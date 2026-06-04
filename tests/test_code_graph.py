"""Tests for the code knowledge graph (airen.onboarding.code_graph)."""

from __future__ import annotations

from pathlib import Path

from airen.onboarding.code_graph import build_code_graph


def _fixture(tmp_path: Path) -> Path:
    (tmp_path / "model.py").write_text(
        "API_FETCH_LIMIT = 73\n"
        "SEQ_LEN = 48\n"
        "\n"
        "def fetch_history(limit):\n"
        "    return limit\n"
        "\n"
        "class Net:\n"
        "    def predict(self, x):\n"
        "        return fetch_history(API_FETCH_LIMIT)\n"
    )
    (tmp_path / "serve.py").write_text(
        "from model import Net\n"
        "def handler():\n"
        "    return Net().predict(1)\n"
    )
    return tmp_path


def test_builds_nodes_and_edges(tmp_path):
    g = build_code_graph(_fixture(tmp_path), source="fix")
    s = g.summary()
    assert s["nodes_by_kind"].get("constant", 0) >= 2     # API_FETCH_LIMIT, SEQ_LEN
    assert s["nodes_by_kind"].get("class", 0) >= 1        # Net
    assert s["nodes_by_kind"].get("function", 0) >= 3     # fetch_history, predict, handler
    assert s["edges_by_type"].get("IMPORTS", 0) >= 1      # serve imports model


def test_find_definition_and_constant(tmp_path):
    g = build_code_graph(_fixture(tmp_path), source="fix")
    d = g.find_definition("fetch_history")
    assert d.get("kind") == "function" and d.get("file") == "model.py"
    consts = g.find_constant_assignments("API_FETCH_LIMIT")
    assert consts and consts[0]["value"] == 73


def test_find_callers(tmp_path):
    g = build_code_graph(_fixture(tmp_path), source="fix")
    callers = g.find_callers("fetch_history")
    assert any(c["caller"] == "predict" for c in callers)


def test_call_path(tmp_path):
    g = build_code_graph(_fixture(tmp_path), source="fix")
    # handler → predict → fetch_history
    path = g.call_path("handler", "fetch_history")
    assert path is not None and path[0] == "handler" and path[-1] == "fetch_history"


def test_save_roundtrip(tmp_path, monkeypatch):
    import airen.onboarding.code_graph as cg
    monkeypatch.setattr(cg, "GRAPHS_DIR", tmp_path / "graphs")
    g = build_code_graph(_fixture(tmp_path), source="fix")
    p = g.save("fix")
    assert p.is_file()
