"""Abstract Graphify adapter — the contract.

Graphify is Airen's code knowledge graph. Given a symbol (function name,
variable, constant), it answers:
  - Where is this defined?
  - Who calls this?
  - Where is this constant value set?

This is the cross-reference layer above plain GitHub search. GitHub search
returns "files that contain the text"; Graphify returns "symbols + their
relationships" — which is what Investigator needs to bisect call sites.

The strongest investigation pattern uses Graphify like this:
  1. Sentinel finds anomaly segment "api_fetch_limit=73"
  2. Investigator asks Graphify: "where is api_fetch_limit set to 73?"
  3. Graphify returns: "predict/main.py:89 passes limit=73 in call to
     fetch_historical_checkcalls"
  4. Investigator now has the exact code line, not just a suspect commit.
"""

from __future__ import annotations

from typing import Any, Protocol


class GraphifyAdapter(Protocol):
    """Symbol-level code-graph query interface."""

    def find_definition(self, repo: str, symbol: str) -> dict[str, Any]:
        """Where is `symbol` defined? Returns file/line/snippet."""
        ...

    def find_callers(self, repo: str, symbol: str, max_results: int = 20) -> list[dict[str, Any]]:
        """Who calls `symbol`? Returns list of file/line/snippet."""
        ...

    def find_constant_assignments(
        self, repo: str, symbol: str, max_results: int = 20
    ) -> list[dict[str, Any]]:
        """Find lines that ASSIGN `symbol` (e.g., `api_fetch_limit = 73`).

        Distinguished from find_callers because this catches `limit=73` in
        function-call kwargs as well as top-level constant definitions.
        """
        ...

    def graph_summary(self, repo: str) -> dict[str, Any]:
        """High-level repo summary: n_files, n_functions, n_classes, languages."""
        ...

    def close(self) -> None:
        ...
