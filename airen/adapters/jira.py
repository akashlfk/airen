"""Factory + facade for the Jira adapter.

Mode controlled by AIREN_JIRA_MODE in env (default 'mock' for safety —
real mode actually creates tickets in your Atlassian instance).
"""

from __future__ import annotations

import os
from typing import Optional

from airen.adapters.jira_base import JiraAdapter


def get_jira_adapter(mode: Optional[str] = None) -> JiraAdapter:
    resolved = (mode or os.environ.get("AIREN_JIRA_MODE", "mock")).strip().lower()
    if resolved == "real":
        from airen.adapters.jira_real import RealJiraAdapter

        return RealJiraAdapter()
    if resolved == "mock":
        from airen.adapters.jira_mock import MockJiraAdapter

        return MockJiraAdapter()
    raise ValueError(f"Unknown AIREN_JIRA_MODE: {mode!r}. Expected 'mock' or 'real'.")
