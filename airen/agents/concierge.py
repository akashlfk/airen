"""Concierge — the conversational face of Airen in Slack.

Engineers @-mention Airen or DM it; the Concierge:
  1. Recognises common intents (status / what-happened / investigate / list)
  2. Routes to the right specialist (Sentinel, Investigator) or audit lookup
  3. Posts a friendly text reply in the same channel/thread

The Concierge is a LIGHT ADK agent — its job is interpretation + dispatch,
not full reasoning. It uses small focused tools that wrap our existing
agent pipeline + audit log.

Flow:
    Slack event → webhook handler → ConciergeAgent.handle(text, user, channel)
                                  → tool call (one of:
                                       check_current_health,
                                       lookup_incident_by_id,
                                       list_recent_incidents,
                                       describe_airen )
                                  → reply text → Slack post
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from google.adk.agents import Agent
from google.adk.tools import FunctionTool

from airen.config import AirenServiceConfig, list_available_services, load_service_config
from airen.llm_factory import get_model_for
from airen.schemas import IncidentRun


# ───────────────────────────────────────────────────────────────────────
#  Concierge tools — thin wrappers around audit log + agent capabilities
# ───────────────────────────────────────────────────────────────────────
LOGS_DIR = Path(__file__).resolve().parent.parent.parent / "logs"


def check_current_health(service_name: str = "tl-eta") -> dict:
    """Quick health check for a service. Returns the LATEST Sentinel verdict
    found in the audit log, OR runs Sentinel fresh if no recent verdict exists.

    Use this when someone asks "status of <service>", "is X healthy", "any
    issues right now?".

    Args:
        service_name: the service identifier (default tl-eta).

    Returns:
        Dict with status (HEALTHY/WARNING/CRITICAL), severity, last_run_id,
        summary, last_anomaly_segment (if any).
    """
    # Find most recent run for this service
    if not LOGS_DIR.exists():
        return {"status": "UNKNOWN", "summary": "No runs in audit log yet."}
    candidates = []
    for p in LOGS_DIR.glob("INC-*.json"):
        try:
            data = json.loads(p.read_text())
            if data.get("project_name", "").startswith(service_name) or service_name in data.get("project_name", ""):
                candidates.append((p, data))
        except Exception:
            continue
    if not candidates:
        return {"status": "UNKNOWN", "summary": f"No runs found for service {service_name!r}."}
    candidates.sort(key=lambda x: x[1].get("started_at", ""), reverse=True)
    latest = candidates[0][1]
    sv = latest.get("sentinel_verdict") or {}
    return {
        "status": sv.get("status", latest.get("final_state", "UNKNOWN")),
        "severity_score": sv.get("severity_score"),
        "last_run_id": latest.get("run_id"),
        "summary": sv.get("summary", "No Sentinel verdict captured."),
        "n_anomalies": len(sv.get("anomalies", [])),
        "started_at": latest.get("started_at"),
    }


def lookup_incident_by_id(incident_id: str) -> dict:
    """Fetch the full IncidentRun for `incident_id` from the audit log.

    Use when someone asks "what happened with INC-XXX" or quotes a run id.

    Args:
        incident_id: e.g. "INC-A3F2B7"

    Returns:
        Dict with title, severity, tldr, root_cause, recommended_fix,
        run_id, started_at, slack_permalink, jira_issue_url.
    """
    path = LOGS_DIR / f"{incident_id}.json"
    if not path.exists():
        return {"error": f"No run with id {incident_id!r} in audit log."}
    try:
        data = json.loads(path.read_text())
        report = data.get("incident_report") or {}
        return {
            "run_id": data.get("run_id"),
            "title": report.get("title", "(no report)"),
            "severity": report.get("severity", data.get("final_state", "")),
            "tldr": report.get("tldr", ""),
            "started_at": data.get("started_at"),
            "finished_at": data.get("finished_at"),
            "final_state": data.get("final_state"),
            "slack_permalink": data.get("slack_permalink"),
            "jira_issue_url": data.get("jira_issue_url"),
            "jira_issue_key": data.get("jira_issue_key"),
        }
    except Exception as e:
        return {"error": f"Failed to load incident: {e}"}


def list_recent_incidents(limit: int = 5) -> dict:
    """List the most recent incidents from the audit log.

    Use when someone asks "what's been going on", "list incidents", "recent issues".
    """
    if not LOGS_DIR.exists():
        return {"incidents": []}
    rows = []
    for p in LOGS_DIR.glob("INC-*.json"):
        try:
            data = json.loads(p.read_text())
            rows.append(
                {
                    "run_id": data.get("run_id"),
                    "service": data.get("project_name"),
                    "severity": (data.get("incident_report") or {}).get("severity")
                    or data.get("final_state"),
                    "title": (data.get("incident_report") or {}).get("title", "(in flight)"),
                    "started_at": data.get("started_at"),
                }
            )
        except Exception:
            continue
    rows.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return {"incidents": rows[:limit]}


def describe_airen() -> dict:
    """Return a short description of what Airen is and what it can do.

    Use when someone asks "what are you", "help", "what can you do".
    """
    return {
        "name": "Airen",
        "tagline": "Autonomous ML Reliability Engineer",
        "capabilities": [
            "Monitor production ML model health (Sentinel)",
            "Diagnose anomalies via Phoenix + MLflow + GitHub + Graphify cross-reference (Investigator)",
            "Draft human-readable incident reports (RCA Writer)",
            "Plan remediation PRs gated by human approval (Remediation)",
            "Validate fixes after deploy (Validator)",
            "Auto-create Jira tickets, post Slack alerts, expose a web dashboard",
        ],
        "commands": [
            "Ask 'status of <service>' for current health",
            "Ask 'what happened with INC-XXXXX' to look up a past incident",
            "Ask 'list recent incidents' for the audit timeline",
            "Ask 'help' or 'what can you do' to see this list",
        ],
        "services": list_available_services(),
    }


def list_known_services() -> dict:
    """Return the list of services Airen is configured to monitor."""
    return {"services": list_available_services()}


# ───────────────────────────────────────────────────────────────────────
#  The Concierge ADK agent
# ───────────────────────────────────────────────────────────────────────
CONCIERGE_INSTRUCTION = """
You are AIREN CONCIERGE — the conversational interface for the Airen ML
Reliability Engineer agent system. You live in Slack. Engineers @-mention
you or DM you when they want to ask about model health.

You are friendly but precise. Engineers don't want a paragraph when they
asked for a single number. Match the question's tone:
  - Short question → short answer
  - Open-ended question → 2-3 sentences with the key facts
  - "Help" / "what can you do" → bullet list of commands

You have these tools — use them, don't make up data:
  • check_current_health(service_name) — for "status", "is X healthy"
  • lookup_incident_by_id(incident_id) — for "what happened with INC-XXX"
  • list_recent_incidents(limit) — for "list issues", "what's been going on"
  • describe_airen() — for "help", "what are you"
  • list_known_services() — for "what services do you watch"

ROUTING RULES:
  - If the user mentions an incident id in the format INC-XXXXXX, call
    lookup_incident_by_id and reply with title + tldr + status, ending
    with a Slack-renderable link to the full report.
  - If the user says "status" / "health" without specifying a service,
    default to "tl-eta".
  - If the user asks about a SPECIFIC anomaly or to investigate something
    NEW (not in the audit log), explain that you can summarise past
    incidents but a fresh investigation needs to be kicked off from the
    web UI's "Run new check" button.
  - If the user's question doesn't match any tool, respond honestly that
    you don't have that capability yet, and suggest the closest thing
    you CAN do.

WRITING STYLE:
  - Use Slack mrkdwn (single asterisks for bold, <url|text> for links).
  - Reference incident ids inline with backticks: `INC-A3F2B7`.
  - When showing severity, prefix with the emoji: 🟢 HEALTHY · 🟡 WARNING · 🔴 CRITICAL.
  - Never invent details. If a tool returned None, say so.

OUTPUT: a single plain-text Slack message body. No JSON, no preamble like
"Sure, I can help with that." Just the answer.
""".strip()


_concierge_tools: list = [
    FunctionTool(func=check_current_health),
    FunctionTool(func=lookup_incident_by_id),
    FunctionTool(func=list_recent_incidents),
    FunctionTool(func=describe_airen),
    FunctionTool(func=list_known_services),
]


root_agent = Agent(
    model=get_model_for("CONCIERGE"),
    name="airen_concierge",
    description="Airen Concierge — answers Slack questions about ML model health and past incidents.",
    instruction=CONCIERGE_INSTRUCTION,
    tools=_concierge_tools,
)
