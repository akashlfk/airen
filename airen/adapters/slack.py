"""Slack adapter — posts Airen IncidentReports to a Slack channel.

Renders the report as Block Kit with a colored side-bar attachment
(red for CRITICAL, yellow for WARNING, green for HEALTHY) so the
incident card looks like a proper monitoring alert in Slack.

Auth via .env:
    SLACK_BOT_TOKEN     xoxb-... (Bot User OAuth Token from your Slack App)
    SLACK_CHANNEL       e.g. "#airen-test" or a channel ID like "C0123456"

Required Bot Token Scopes (set in api.slack.com/apps → OAuth & Permissions):
    chat:write              — post messages
    chat:write.public       — post to public channels without joining
    chat:write.customize    — override bot username/icon per message (optional)
"""

from __future__ import annotations

import os
import re
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from airen.schemas import HealthStatus, IncidentReport


# ───────────────────────────────────────────────────────────────────────
#  Visual style — match the web UI's traffic-light palette
# ───────────────────────────────────────────────────────────────────────
SEVERITY_COLORS = {
    HealthStatus.HEALTHY: "#10b981",
    HealthStatus.WARNING: "#f59e0b",
    HealthStatus.CRITICAL: "#ef4444",
}
SEVERITY_EMOJI = {
    HealthStatus.HEALTHY: ":large_green_circle:",
    HealthStatus.WARNING: ":large_yellow_circle:",
    HealthStatus.CRITICAL: ":red_circle:",
}


# ───────────────────────────────────────────────────────────────────────
#  Markdown conversion — our schemas use standard MD, Slack uses mrkdwn
# ───────────────────────────────────────────────────────────────────────
def _md_to_slack(text: str) -> str:
    """Convert common Markdown → Slack's mrkdwn dialect."""
    if not text:
        return text
    # **bold** → *bold*  (do BEFORE single-asterisk handling)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    # [text](url) → <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)
    # Inline code `foo` survives as-is (Slack supports it)
    # Slack section text limit is 3000 chars per block
    return text


def _truncate(text: str, max_len: int = 2900) -> str:
    if not text or len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


# ───────────────────────────────────────────────────────────────────────
#  Block Kit builder
# ───────────────────────────────────────────────────────────────────────
def build_blocks(
    report: IncidentReport,
    incident_id: str | None = None,
    jira_url: str | None = None,
    jira_key: str | None = None,
) -> list[dict]:
    """Render an IncidentReport as a list of Slack Block Kit blocks."""
    emoji = SEVERITY_EMOJI[report.severity]
    blocks: list[dict] = []

    # Header — severity + title
    header_text = f"{emoji} {report.severity.value} · {report.title}"
    blocks.append(
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header_text[:150], "emoji": True},
        }
    )

    # Context — incident id, jira link, service (small grey text)
    ctx_parts = []
    if incident_id:
        ctx_parts.append(f"`{incident_id}`")
    if jira_url and jira_key:
        ctx_parts.append(f"🎫 <{jira_url}|{jira_key}>")
    ctx_parts.append("posted by *Airen* — autonomous ML reliability")
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "  ·  ".join(ctx_parts)}],
        }
    )

    # TL;DR — what an on-call engineer sees first
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _truncate(f"*TL;DR*\n{_md_to_slack(report.tldr)}"),
            },
        }
    )

    blocks.append({"type": "divider"})

    # What happened
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _truncate(f"*What happened*\n{_md_to_slack(report.what_happened)}"),
            },
        }
    )

    # Root cause
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _truncate(f"*Root cause*\n{_md_to_slack(report.root_cause)}"),
            },
        }
    )

    # Evidence
    if report.evidence_bullets:
        ev_text = "\n".join(f"• {_md_to_slack(b.lstrip('•').lstrip())}" for b in report.evidence_bullets)
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _truncate(f"*Evidence*\n{ev_text}")},
            }
        )

    # Recommended fix
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _truncate(f"*Recommended fix*\n{_md_to_slack(report.recommended_fix)}"),
            },
        }
    )

    # Open questions
    if report.open_questions:
        q_text = "\n".join(f"• {_md_to_slack(q)}" for q in report.open_questions)
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _truncate(f"*Open questions*\n{q_text}")},
            }
        )

    # Interactive Approve / Reject buttons — only on CRITICAL/WARNING incidents.
    # The handler at /slack/actions records the decision into the IncidentRun.
    if incident_id and report.severity in (HealthStatus.CRITICAL, HealthStatus.WARNING):
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ Approve & Execute"},
                        "style": "primary",
                        "action_id": "approve_remediation",
                        "value": incident_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "❌ Reject"},
                        "style": "danger",
                        "action_id": "reject_remediation",
                        "value": incident_id,
                    },
                ],
            }
        )

    return blocks


# ───────────────────────────────────────────────────────────────────────
#  Public API
# ───────────────────────────────────────────────────────────────────────
def _client() -> WebClient:
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "SLACK_BOT_TOKEN is not set in .env. Add it (xoxb-...) — never paste in chat."
        )
    return WebClient(token=token)


def post_incident_report(
    report: IncidentReport,
    channel: str | None = None,
    incident_id: str | None = None,
    jira_url: str | None = None,
    jira_key: str | None = None,
) -> dict[str, Any]:
    """Post an IncidentReport to the configured Slack channel.

    Returns a dict with ok/channel/ts (timestamp = message ID, used for threading
    follow-ups later). If a Jira ticket was created, its key + URL are surfaced
    in the message context line.
    """
    channel = (channel or os.environ.get("SLACK_CHANNEL", "")).strip()
    if not channel:
        raise RuntimeError("SLACK_CHANNEL not set in .env (e.g. #airen-test)")

    blocks = build_blocks(report, incident_id=incident_id, jira_url=jira_url, jira_key=jira_key)
    color = SEVERITY_COLORS[report.severity]

    # Fallback text for OS-level notifications (appears in mobile push, etc.)
    fallback = f"[{report.severity.value}] {report.title}"

    client = _client()
    try:
        resp = client.chat_postMessage(
            channel=channel,
            text=fallback,
            attachments=[
                {
                    "color": color,
                    "blocks": blocks,
                }
            ],
        )
    except SlackApiError as e:
        return {
            "ok": False,
            "error": e.response.get("error"),
            "details": str(e),
        }

    return {
        "ok": resp.get("ok"),
        "channel": resp.get("channel"),
        "ts": resp.get("ts"),
        "permalink": _safe_permalink(client, resp.get("channel"), resp.get("ts")),
    }


def _safe_permalink(client: WebClient, channel: str | None, ts: str | None) -> str | None:
    if not channel or not ts:
        return None
    try:
        r = client.chat_getPermalink(channel=channel, message_ts=ts)
        return r.get("permalink")
    except Exception:
        return None


def post_text(channel: str | None, text: str, thread_ts: str | None = None) -> dict[str, Any]:
    """Send plain text. Pass `thread_ts` to reply in a thread (used by Concierge)."""
    channel = (channel or os.environ.get("SLACK_CHANNEL", "")).strip()
    if not channel:
        raise RuntimeError("SLACK_CHANNEL not set")
    kwargs: dict[str, Any] = {"channel": channel, "text": text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    try:
        resp = _client().chat_postMessage(**kwargs)
        return {"ok": resp.get("ok"), "ts": resp.get("ts")}
    except SlackApiError as e:
        return {"ok": False, "error": e.response.get("error")}


# ───────────────────────────────────────────────────────────────────────
#  Slack request signature verification (for incoming webhooks)
#  https://api.slack.com/authentication/verifying-requests-from-slack
# ───────────────────────────────────────────────────────────────────────
def verify_slack_signature(
    body: bytes,
    timestamp: str,
    signature: str,
    signing_secret: str | None = None,
    max_age_seconds: int = 300,
) -> bool:
    """Verify a Slack request signature. Returns True on success.

    Fails closed: if SLACK_SIGNING_SECRET is not set, this returns False.
    Caller should set AIREN_SLACK_SKIP_VERIFY=1 to bypass during ngrok dev only.
    """
    import hashlib
    import hmac
    import time

    secret = (signing_secret or os.environ.get("SLACK_SIGNING_SECRET", "")).strip()
    if not secret:
        return False
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts_int) > max_age_seconds:
        return False

    basestring = f"v0:{timestamp}:".encode() + body
    digest = hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)
