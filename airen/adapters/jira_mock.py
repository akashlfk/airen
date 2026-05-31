"""Mock Jira adapter — returns canned ticket data, no network."""

from __future__ import annotations

import random
import string
from typing import Any


class MockJiraAdapter:
    """In-memory Jira simulator. Records every call so tests can introspect."""

    def __init__(self, base_url: str = "https://airen-demo.atlassian.net") -> None:
        self.base_url = base_url
        # call log — useful for tests AND for the demo UI to show "what would have happened"
        self.comments: list[dict[str, Any]] = []
        self.transitions: list[dict[str, Any]] = []

    def create_issue(
        self,
        project_key: str,
        summary: str,
        description: str,
        issue_type: str = "Task",
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        # Generate a plausible-looking issue key like ETAI-1842
        key = f"{project_key}-{random.randint(1000, 9999)}"
        return {
            "key": key,
            "id": "".join(random.choices(string.digits, k=7)),
            "url": f"{self.base_url}/browse/{key}",
            "self": f"{self.base_url}/rest/api/2/issue/{key}",
        }

    def add_comment(self, issue_key: str, body: str) -> dict[str, Any]:
        self.comments.append({"issue": issue_key, "body": body})
        return {"id": "".join(random.choices(string.digits, k=7)), "issue": issue_key}

    def transition_issue(self, issue_key: str, transition_name: str) -> dict[str, Any]:
        self.transitions.append({"issue": issue_key, "transition": transition_name})
        return {"transitioned_to": transition_name, "issue": issue_key}
