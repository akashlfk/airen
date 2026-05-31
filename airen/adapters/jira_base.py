"""Abstract Jira (Atlassian Cloud) adapter — the contract.

Same Protocol pattern as other adapters. The Orchestrator calls this to
create + transition incident tickets. Real adapter hits Atlassian REST API
v2; mock adapter returns canned ticket data so laptop dev works offline.
"""

from __future__ import annotations

from typing import Any, Protocol


class JiraAdapter(Protocol):
    def create_issue(
        self,
        project_key: str,
        summary: str,
        description: str,
        issue_type: str = "Task",
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a Jira issue. Returns {key, id, url, self} or {error: ...}."""
        ...

    def add_comment(self, issue_key: str, body: str) -> dict[str, Any]:
        ...

    def transition_issue(self, issue_key: str, transition_name: str) -> dict[str, Any]:
        ...
