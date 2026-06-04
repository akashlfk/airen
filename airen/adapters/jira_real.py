"""Real Atlassian Cloud Jira adapter.

Auth: Basic auth with email + API token (works for both legacy and the newer
"scoped" API tokens). URL routing depends on which kind of token:

  - Legacy tokens (no scopes) call the workspace URL directly:
      https://<your-site>.atlassian.net/rest/api/3/...
  - Scoped tokens MUST go through the Atlassian API gateway with cloud_id:
      https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/...

This adapter auto-discovers the cloud_id via the public /_edge/tenant_info
endpoint on first use and always uses the gateway pattern (works for both
token types and is the path Atlassian is migrating to).

Descriptions and comments must be in Atlassian Document Format (ADF) for the
v3 API; we wrap plain strings into a minimal ADF doc transparently.

Generate a scoped token at https://id.atlassian.com/manage-profile/security/api-tokens
with these scopes: read:me, read:project:jira, read:issue:jira,
write:issue:jira, write:comment:jira.

Reads from env (canonical AIREN_JIRA_* names; ATLASSIAN_* names accepted as fallback):
    AIREN_JIRA_BASE_URL     e.g. https://fourkites.atlassian.net
    AIREN_JIRA_USER_EMAIL   the email of the API user
    AIREN_JIRA_API_TOKEN    ATATT... — from id.atlassian.com
"""

from __future__ import annotations

import os
from base64 import b64encode
from typing import Any

import requests


_GATEWAY_BASE = "https://api.atlassian.com/ex/jira"


def _adf_text(text: str) -> dict:
    """Wrap a plain-text string as a minimal valid Atlassian Document Format
    document. v3 API rejects raw strings in description/comment bodies.

    Newlines split into separate paragraph nodes; empty lines become empty
    paragraphs (visual blank line). Bullet/heading markdown isn't expanded —
    if richer rendering is needed, build the ADF directly.
    """
    paragraphs: list[dict] = []
    for line in text.split("\n"):
        if line.strip():
            paragraphs.append({"type": "paragraph", "content": [{"type": "text", "text": line}]})
        else:
            paragraphs.append({"type": "paragraph"})
    if not paragraphs:
        paragraphs = [{"type": "paragraph"}]
    return {"type": "doc", "version": 1, "content": paragraphs}


def _auth_header(email: str, token: str) -> str:
    raw = f"{email}:{token}".encode()
    return f"Basic {b64encode(raw).decode()}"


def _truncate_for_jira(text: str, max_chars: int = 32000) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 50] + "\n\n... (truncated by Airen)"


class RealJiraAdapter:
    """Hits the real Atlassian Cloud REST API v2."""

    def __init__(self, *, base_url: str | None = None, user_email: str | None = None) -> None:
        # Non-secret topology (base_url, user_email) comes from airen.yaml when
        # provided; otherwise fall back to the AIREN_JIRA_* / ATLASSIAN_* env
        # vars so existing setups don't break. The API token is always env-only.
        self.base_url = (
            (base_url or "").strip()
            or os.environ.get("AIREN_JIRA_BASE_URL", "").strip()
            or os.environ.get("ATLASSIAN_BASE_URL", "").strip()
        ).rstrip("/")
        self.email = (
            (user_email or "").strip()
            or os.environ.get("AIREN_JIRA_USER_EMAIL", "").strip()
            or os.environ.get("ATLASSIAN_USER_EMAIL", "").strip()
        )
        self.token = (
            os.environ.get("AIREN_JIRA_API_KEY", "").strip()
            or os.environ.get("AIREN_JIRA_API_TOKEN", "").strip()
            or os.environ.get("ATLASSIAN_API_TOKEN", "").strip()
        )
        missing = [
            n for n, v in [
                ("AIREN_JIRA_BASE_URL", self.base_url),
                ("AIREN_JIRA_USER_EMAIL", self.email),
                ("AIREN_JIRA_API_KEY", self.token),
            ] if not v
        ]
        if missing:
            raise RuntimeError(
                f"Jira config missing: {', '.join(missing)}. Add to .env "
                f"(token from id.atlassian.com/manage-profile/security/api-tokens)."
            )
        self._headers = {
            "Authorization": _auth_header(self.email, self.token),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        # cloud_id discovery is lazy — happens on first API call. The gateway
        # URL (https://api.atlassian.com/ex/jira/<cloud_id>) works for BOTH
        # legacy and scoped tokens, so we always go through it.
        self._cloud_id: str | None = None
        self._api_root: str | None = None

    def _resolve_api_root(self) -> str:
        """Discover the cloud_id once via the unauth tenant_info endpoint, then
        cache the gateway base URL. Falls back to the workspace URL if discovery
        fails (legacy token path — still works for old API tokens).
        """
        if self._api_root is not None:
            return self._api_root
        try:
            r = requests.get(f"{self.base_url}/_edge/tenant_info", timeout=10)
            r.raise_for_status()
            cid = r.json().get("cloudId")
            if cid:
                self._cloud_id = cid
                self._api_root = f"{_GATEWAY_BASE}/{cid}"
                return self._api_root
        except Exception:
            pass
        # Fallback — direct workspace URL (works for legacy tokens only)
        self._api_root = self.base_url
        return self._api_root

    def create_issue(
        self,
        project_key: str,
        summary: str,
        description: str,
        issue_type: str = "Task",
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        root = self._resolve_api_root()
        payload: dict[str, Any] = {
            "fields": {
                "project": {"key": project_key},
                "issuetype": {"name": issue_type},
                "summary": summary[:255],
                "description": _adf_text(_truncate_for_jira(description)),
            }
        }
        if labels:
            payload["fields"]["labels"] = labels
        try:
            r = requests.post(
                f"{root}/rest/api/3/issue",
                headers=self._headers,
                json=payload,
                timeout=15,
            )
        except requests.RequestException as e:
            return {"error": f"network: {type(e).__name__}: {e}"}
        if r.status_code not in (200, 201):
            return {
                "error": f"jira returned {r.status_code}",
                "details": (r.text or "")[:500],
            }
        data = r.json()
        key = data.get("key")
        # Always return the human-clickable workspace URL for the browse link,
        # even though API calls go through the gateway.
        return {
            "key": key,
            "id": data.get("id"),
            "url": f"{self.base_url}/browse/{key}" if key else None,
            "self": data.get("self"),
        }

    def add_comment(self, issue_key: str, body: str) -> dict[str, Any]:
        root = self._resolve_api_root()
        try:
            r = requests.post(
                f"{root}/rest/api/3/issue/{issue_key}/comment",
                headers=self._headers,
                json={"body": _adf_text(_truncate_for_jira(body))},
                timeout=15,
            )
        except requests.RequestException as e:
            return {"error": f"network: {type(e).__name__}: {e}"}
        if r.status_code not in (200, 201):
            return {"error": f"jira returned {r.status_code}", "details": (r.text or "")[:500]}
        return r.json()

    def transition_issue(self, issue_key: str, transition_name: str) -> dict[str, Any]:
        root = self._resolve_api_root()
        try:
            r = requests.get(
                f"{root}/rest/api/3/issue/{issue_key}/transitions",
                headers=self._headers,
                timeout=15,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            return {"error": f"list transitions failed: {e}"}
        transitions = r.json().get("transitions", [])
        target = next(
            (t for t in transitions if t["name"].lower() == transition_name.lower()),
            None,
        )
        if target is None:
            return {
                "error": f"transition {transition_name!r} not found",
                "available": [t["name"] for t in transitions],
            }
        try:
            r2 = requests.post(
                f"{root}/rest/api/3/issue/{issue_key}/transitions",
                headers=self._headers,
                json={"transition": {"id": target["id"]}},
                timeout=15,
            )
        except requests.RequestException as e:
            return {"error": f"transition POST failed: {e}"}
        if r2.status_code not in (200, 204):
            return {"error": f"jira returned {r2.status_code}", "details": (r2.text or "")[:500]}
        return {"transitioned_to": transition_name}
