"""Graph-backed Graphify adapter (AIREN_GRAPHIFY_MODE=graph).

Implements the GraphifyAdapter Protocol on top of a real AST-built code
knowledge graph (airen.onboarding.code_graph). Same queries as the ripgrep
adapter, but answered from the graph — plus the graph enables richer reasoning
(call paths, importers) and an optional Neo4j export for the browser viz.

Clones/refreshes the repo via the existing Graphify cache, builds the graph
once per repo (cached in-process), and saves the artifact under graphs/.
"""

from __future__ import annotations

from typing import Any

from airen.onboarding.code_graph import CodeGraph, build_code_graph


class GraphGraphifyAdapter:
    def __init__(self) -> None:
        self._graphs: dict[str, CodeGraph] = {}

    def _graph(self, repo: str) -> CodeGraph:
        if repo not in self._graphs:
            from airen.adapters.graphify_real import _ensure_repo
            from airen.onboarding.manifest import RepoManifest

            root = _ensure_repo(repo)
            g = build_code_graph(root, source=repo)
            try:
                g.save(RepoManifest.slug_for(repo, is_remote=True))
            except Exception:
                pass
            self._graphs[repo] = g
        return self._graphs[repo]

    # ── Protocol ─────────────────────────────────────────────────────
    def find_definition(self, repo: str, symbol: str) -> dict[str, Any]:
        return self._graph(repo).find_definition(symbol)

    def find_callers(self, repo: str, symbol: str, max_results: int = 20) -> list[dict[str, Any]]:
        return self._graph(repo).find_callers(symbol, max_results)

    def find_constant_assignments(self, repo: str, symbol: str, max_results: int = 20) -> list[dict[str, Any]]:
        return self._graph(repo).find_constant_assignments(symbol, max_results)

    def graph_summary(self, repo: str) -> dict[str, Any]:
        return self._graph(repo).summary()

    # ── graph-only extras ────────────────────────────────────────────
    def call_path(self, repo: str, from_name: str, to_name: str) -> list[str] | None:
        return self._graph(repo).call_path(from_name, to_name)

    def importers(self, repo: str, module_name: str) -> list[str]:
        return self._graph(repo).importers(module_name)

    def export_neo4j(self, repo: str) -> str:
        from airen.onboarding.code_graph import export_to_neo4j

        return export_to_neo4j(self._graph(repo))

    def close(self) -> None:
        return
