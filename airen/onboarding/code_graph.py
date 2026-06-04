"""Code knowledge graph — the 'advanced graphify' layer.

Builds a queryable graph of the repo's code entities + relationships via Python
AST. Nodes: modules, classes, functions, module-level constants. Edges:
DEFINES, IMPORTS, CALLS, ASSIGNS. Persisted as node-link JSON under graphs/.

Two backends:
  • in-process (default) — stdlib only, no server. Powers the GraphifyAdapter
    queries (find_definition / find_callers / find_constant_assignments) plus
    richer graph queries (call_path, callees, importers).
  • Neo4j (optional) — `export_to_neo4j()` ships the same graph to a Neo4j
    instance (Cypher MERGE) for the browser viz + Cypher queries. Requires the
    `neo4j` package + NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD; best-effort.

Call edges are resolved by bare name (Python dispatch is dynamic), so CALLS is
approximate — good enough to localize suspects, not a type-checker.
"""

from __future__ import annotations

import ast
import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from airen.onboarding.detectors import _parse, _py_files, _rel

GRAPHS_DIR = Path(__file__).resolve().parent.parent.parent / "graphs"


@dataclass
class CodeGraph:
    source: str
    nodes: dict[str, dict] = field(default_factory=dict)      # id -> {kind,name,file,line,value?}
    edges: list[dict] = field(default_factory=list)           # {src,dst,type}

    # ── build ────────────────────────────────────────────────────────
    def _add_node(self, nid: str, **attrs) -> str:
        if nid not in self.nodes:
            self.nodes[nid] = {"id": nid, **attrs}
        return nid

    def _add_edge(self, src: str, dst: str, etype: str) -> None:
        self.edges.append({"src": src, "dst": dst, "type": etype})

    # ── queries (back the GraphifyAdapter Protocol) ──────────────────
    def find_definition(self, symbol: str) -> dict[str, Any]:
        for n in self.nodes.values():
            if n.get("name") == symbol and n["kind"] in ("function", "class", "constant"):
                return {"file": n.get("file"), "line": n.get("line"), "kind": n["kind"], "id": n["id"]}
        return {"error": f"definition of {symbol!r} not found"}

    def find_callers(self, symbol: str, max_results: int = 20) -> list[dict[str, Any]]:
        out = []
        for e in self.edges:
            if e["type"] == "CALLS" and self.nodes.get(e["dst"], {}).get("name") == symbol:
                src = self.nodes.get(e["src"], {})
                out.append({"caller": src.get("name"), "file": src.get("file"), "line": src.get("line")})
                if len(out) >= max_results:
                    break
        return out

    def find_constant_assignments(self, symbol: str, max_results: int = 20) -> list[dict[str, Any]]:
        out = []
        for n in self.nodes.values():
            if n["kind"] == "constant" and n.get("name") == symbol:
                out.append({"file": n.get("file"), "line": n.get("line"), "value": n.get("value")})
                if len(out) >= max_results:
                    break
        return out

    def callees(self, func_name: str) -> list[str]:
        ids = {n["id"] for n in self.nodes.values() if n.get("name") == func_name and n["kind"] == "function"}
        return sorted({self.nodes[e["dst"]].get("name") for e in self.edges
                       if e["type"] == "CALLS" and e["src"] in ids and e["dst"] in self.nodes})

    def importers(self, module_name: str) -> list[str]:
        return sorted({self.nodes[e["src"]].get("name") for e in self.edges
                       if e["type"] == "IMPORTS" and self.nodes.get(e["dst"], {}).get("name") == module_name})

    def call_path(self, from_name: str, to_name: str, max_depth: int = 8) -> list[str] | None:
        """BFS over CALLS edges from any function named from_name to to_name."""
        adj: dict[str, list[str]] = {}
        for e in self.edges:
            if e["type"] == "CALLS":
                adj.setdefault(e["src"], []).append(e["dst"])
        starts = [n["id"] for n in self.nodes.values() if n.get("name") == from_name]
        q = deque((s, [self.nodes[s]["name"]]) for s in starts)
        seen = set(starts)
        while q:
            nid, path = q.popleft()
            if len(path) > max_depth:
                continue
            if self.nodes.get(nid, {}).get("name") == to_name and len(path) > 1:
                return path
            for dst in adj.get(nid, []):
                if dst not in seen and dst in self.nodes:
                    seen.add(dst)
                    if self.nodes[dst].get("name") == to_name:
                        return path + [self.nodes[dst]["name"]]
                    q.append((dst, path + [self.nodes[dst]["name"]]))
        return None

    def summary(self) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        for n in self.nodes.values():
            by_kind[n["kind"]] = by_kind.get(n["kind"], 0) + 1
        by_edge: dict[str, int] = {}
        for e in self.edges:
            by_edge[e["type"]] = by_edge.get(e["type"], 0) + 1
        return {"n_nodes": len(self.nodes), "n_edges": len(self.edges),
                "nodes_by_kind": by_kind, "edges_by_type": by_edge}

    # ── persistence ──────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {"source": self.source, "nodes": list(self.nodes.values()), "edges": self.edges}

    def save(self, slug: str) -> Path:
        GRAPHS_DIR.mkdir(parents=True, exist_ok=True)
        p = GRAPHS_DIR / f"{slug}.codegraph.json"
        p.write_text(json.dumps(self.to_dict(), indent=2))
        return p


# ───────────────────────────────────────────────────────────────────────
#  build from AST
# ───────────────────────────────────────────────────────────────────────
def build_code_graph(root: str | Path, source: str = "?") -> CodeGraph:
    root = Path(root)
    g = CodeGraph(source=source)

    for p in _py_files(root):
        mod = _parse(p)
        if not mod:
            continue
        rel = _rel(root, p)
        mod_id = f"module:{rel}"
        g._add_node(mod_id, kind="module", name=rel, file=rel, line=1)

        # imports
        for node in ast.walk(mod):
            if isinstance(node, ast.Import):
                for a in node.names:
                    tid = f"module:{a.name}"
                    g._add_node(tid, kind="module", name=a.name)
                    g._add_edge(mod_id, tid, "IMPORTS")
            elif isinstance(node, ast.ImportFrom) and node.module:
                tid = f"module:{node.module}"
                g._add_node(tid, kind="module", name=node.module)
                g._add_edge(mod_id, tid, "IMPORTS")

        # top-level constants
        for node in mod.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        cid = f"const:{rel}:{t.id}"
                        g._add_node(cid, kind="constant", name=t.id, file=rel,
                                    line=node.lineno, value=node.value.value)
                        g._add_edge(mod_id, cid, "ASSIGNS")

        # classes, functions, calls
        _walk_defs(g, mod, mod_id, rel, parent_id=mod_id)

    _resolve_calls(g)
    return g


def _resolve_calls(g: CodeGraph) -> None:
    """Rewire CALLS edges from `callname:X` placeholders to the real function
    node(s) named X, so call paths/callers traverse function→function. Calls
    with no in-repo target stay as callname placeholders (external)."""
    name_to_funcs: dict[str, list[str]] = {}
    for n in g.nodes.values():
        if n["kind"] == "function":
            name_to_funcs.setdefault(n["name"], []).append(n["id"])
    resolved: list[dict] = []
    for e in g.edges:
        if e["type"] == "CALLS":
            dst = g.nodes.get(e["dst"], {})
            if dst.get("kind") == "callname":
                targets = name_to_funcs.get(dst.get("name"))
                if targets:
                    for t in targets:
                        resolved.append({"src": e["src"], "dst": t, "type": "CALLS"})
                    continue
        resolved.append(e)
    g.edges = resolved


def _walk_defs(g: CodeGraph, scope: ast.AST, mod_id: str, rel: str, parent_id: str) -> None:
    for node in getattr(scope, "body", []):
        if isinstance(node, ast.ClassDef):
            cid = f"class:{rel}:{node.name}"
            g._add_node(cid, kind="class", name=node.name, file=rel, line=node.lineno)
            g._add_edge(parent_id, cid, "DEFINES")
            _walk_defs(g, node, mod_id, rel, parent_id=cid)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fid = f"func:{rel}:{node.name}:{node.lineno}"
            g._add_node(fid, kind="function", name=node.name, file=rel, line=node.lineno)
            g._add_edge(parent_id, fid, "DEFINES")
            # calls inside this function
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    fn = sub.func
                    callee = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
                    if callee:
                        # link to a callee placeholder by name (resolved at query time)
                        nid = f"callname:{callee}"
                        g._add_node(nid, kind="callname", name=callee)
                        g._add_edge(fid, nid, "CALLS")
            _walk_defs(g, node, mod_id, rel, parent_id=fid)


# ───────────────────────────────────────────────────────────────────────
#  optional Neo4j export
# ───────────────────────────────────────────────────────────────────────
def export_to_neo4j(g: CodeGraph) -> str:
    """Push the graph to Neo4j via Cypher MERGE. Best-effort; needs the `neo4j`
    package + NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD. Returns a status string."""
    import os

    uri = os.environ.get("NEO4J_URI")
    if not uri:
        return "skipped: NEO4J_URI not set"
    try:
        from neo4j import GraphDatabase  # type: ignore
    except ImportError:
        return "skipped: `neo4j` package not installed (pip install neo4j)"
    try:
        driver = GraphDatabase.driver(
            uri, auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")),
        )
        with driver.session() as s:
            for n in g.nodes.values():
                s.run("MERGE (x:Code {id:$id}) SET x.kind=$kind, x.name=$name, x.file=$file, x.line=$line",
                      id=n["id"], kind=n.get("kind"), name=n.get("name"), file=n.get("file"), line=n.get("line"))
            for e in g.edges:
                s.run("MATCH (a:Code {id:$s}),(b:Code {id:$d}) MERGE (a)-[r:REL {type:$t}]->(b)",
                      s=e["src"], d=e["dst"], t=e["type"])
        driver.close()
        return f"exported {len(g.nodes)} nodes / {len(g.edges)} edges to {uri}"
    except Exception as ex:  # noqa: BLE001
        return f"neo4j export failed: {type(ex).__name__}: {ex}"
