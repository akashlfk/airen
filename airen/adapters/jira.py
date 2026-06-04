"""Factory + facade for the Jira adapter.

Mode controlled by AIREN_JIRA_MODE in env (default 'mock' for safety —
real mode actually creates tickets in your Atlassian instance).
"""

from __future__ import annotations

import os
from typing import Optional

from airen.adapters.jira_base import JiraAdapter


def get_jira_adapter(mode: Optional[str] = None, *, base_url=None, user_email=None) -> JiraAdapter:
    """`base_url`/`user_email` come from airen.yaml (jira block); both fall back
    to AIREN_JIRA_BASE_URL / AIREN_JIRA_USER_EMAIL env when None. The API token
    is always env-only (AIREN_JIRA_API_TOKEN)."""
    resolved = (mode or os.environ.get("AIREN_JIRA_MODE", "mock")).strip().lower()
    if resolved == "real":
        from airen.adapters.jira_real import RealJiraAdapter

        return RealJiraAdapter(base_url=base_url, user_email=user_email)
    if resolved == "mock":
        from airen.adapters.jira_mock import MockJiraAdapter

        return MockJiraAdapter()
    raise ValueError(f"Unknown AIREN_JIRA_MODE: {mode!r}. Expected 'mock' or 'real'.")
