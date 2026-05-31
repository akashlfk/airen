"""Mock Graphify adapter — canned call-graph data for the Fritolay scenario.

The mock is opinionated: it knows about the api_fetch_limit story so that
Investigator's demo run lands the "I found the exact call site" beat. In
real mode (when we have a checked-out repo on the FK VM) this gets replaced
with actual tree-sitter / ripgrep parsing.
"""

from __future__ import annotations

from typing import Any


# Canned graph data for cloudqwest/dynamic_eta_prediction.
# Each entry mirrors what a real code-graph tool would return.
_CANNED_GRAPH: dict[str, dict[str, Any]] = {
    "cloudqwest/dynamic_eta_prediction": {
        "definitions": {
            "fetch_historical_checkcalls": {
                "file": "predict/processing/location_service/LocationService.py",
                "line": 42,
                "kind": "method",
                "snippet": (
                    "def fetch_historical_checkcalls(self, one_tracking_entry: Dict, "
                    "limit: Optional[int] = None) -> Optional[List[Dict]]:"
                ),
                "introduced_in_commit": "abc1234e",  # original definition, much older
            },
            "API_FETCH_LIMIT": {
                "file": "predict/main.py",
                "line": 12,
                "kind": "constant",
                "snippet": "API_FETCH_LIMIT = 73   # added in PR #741",
                "introduced_in_commit": "6270ca55",
            },
        },
        "callers": {
            "fetch_historical_checkcalls": [
                {
                    "file": "predict/main.py",
                    "line": 89,
                    "function": "predict_eta",
                    "snippet": (
                        "checkcalls = self.location_service.fetch_historical_checkcalls("
                        "entry, limit=API_FETCH_LIMIT)"
                    ),
                    "kwargs_passed": {"limit": "API_FETCH_LIMIT"},
                },
                {
                    "file": "predict/processing/feature_engineering.py",
                    "line": 187,
                    "function": "build_feature_vector",
                    "snippet": (
                        "history = location_service.fetch_historical_checkcalls("
                        "tracking)"
                    ),
                    "kwargs_passed": {},
                },
            ],
        },
        "constant_assignments": {
            "api_fetch_limit": [
                {
                    "file": "predict/main.py",
                    "line": 12,
                    "kind": "module-constant",
                    "snippet": "API_FETCH_LIMIT = 73",
                    "value": "73",
                    "commit": "6270ca55",
                    "commit_author": "akashlfk",
                },
                {
                    "file": "predict/main.py",
                    "line": 89,
                    "kind": "kwarg-in-call",
                    "snippet": (
                        "checkcalls = self.location_service.fetch_historical_checkcalls("
                        "entry, limit=API_FETCH_LIMIT)"
                    ),
                    "value": "API_FETCH_LIMIT (= 73)",
                    "commit": "6270ca55",
                    "commit_author": "akashlfk",
                },
            ],
        },
        "summary": {
            "n_python_files": 47,
            "n_functions": 184,
            "n_classes": 23,
            "languages": ["python"],
            "default_branch": "production",
        },
    },
}


class MockGraphifyAdapter:
    """Returns canned graph data for the Fritolay scenario."""

    def find_definition(self, repo: str, symbol: str) -> dict[str, Any]:
        repo_graph = _CANNED_GRAPH.get(repo, {})
        # Try direct lookup, then case-insensitive
        defs = repo_graph.get("definitions", {})
        if symbol in defs:
            return defs[symbol]
        for k, v in defs.items():
            if k.lower() == symbol.lower():
                return v
        return {"error": f"symbol {symbol!r} not found in {repo}"}

    def find_callers(
        self, repo: str, symbol: str, max_results: int = 20
    ) -> list[dict[str, Any]]:
        repo_graph = _CANNED_GRAPH.get(repo, {})
        callers = repo_graph.get("callers", {})
        # Case-insensitive lookup
        result = []
        for k, v in callers.items():
            if k.lower() == symbol.lower():
                result = v
                break
        return result[:max_results]

    def find_constant_assignments(
        self, repo: str, symbol: str, max_results: int = 20
    ) -> list[dict[str, Any]]:
        repo_graph = _CANNED_GRAPH.get(repo, {})
        assignments = repo_graph.get("constant_assignments", {})
        for k, v in assignments.items():
            if k.lower() == symbol.lower():
                return v[:max_results]
        return []

    def graph_summary(self, repo: str) -> dict[str, Any]:
        return _CANNED_GRAPH.get(repo, {}).get(
            "summary",
            {"n_python_files": 0, "n_functions": 0, "n_classes": 0, "languages": []},
        )

    def close(self) -> None:
        return
