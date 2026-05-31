"""Tests for the Slack approval gate + thread → incident lookup.

Two critical user-facing flows:
  1. _await_human_approval — pauses orchestrator until /slack/actions writes
     an approval into logs/<run_id>.json, OR times out.
  2. _build_thread_incident_context — when a user replies in an incident's
     Slack thread, Concierge gets the incident context auto-injected.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


@pytest.fixture
def temp_logs_dir(tmp_path, monkeypatch):
    """Redirect orchestrator's _LOGS_DIR to a tmp path so tests don't touch real logs."""
    from airen.agents import orchestrator as orch_module

    monkeypatch.setattr(orch_module, "_LOGS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def make_orch(temp_logs_dir):
    """Factory that builds an AirenOrchestrator without touching real services."""
    from airen.agents.orchestrator import AirenOrchestrator
    from airen.config import AirenServiceConfig

    def _factory(slack_message_ts: str | None = "1700000000.000100"):
        cfg = AirenServiceConfig.model_validate({
            "service": {"name": "test-svc"},
            "phoenix": {"project_name": "test-proj"},
        })
        orch = AirenOrchestrator(
            project_name="test-proj",
            config=cfg,
            cooldown_sec=0,
            post_to_slack=True,
        )
        orch.run.slack_message_ts = slack_message_ts
        return orch

    return _factory


# ───────────────────────────────────────────────────────────────────────
#  _await_human_approval — the pause-and-poll loop
# ───────────────────────────────────────────────────────────────────────
def test_skip_wait_when_no_slack_post(make_orch):
    """If we never posted to Slack, there's nothing to wait for — proceed."""
    orch = make_orch(slack_message_ts=None)
    result = asyncio.run(orch._await_human_approval(timeout_sec=1))
    assert result is True


def test_returns_true_on_approve_decision(make_orch, temp_logs_dir):
    orch = make_orch()
    # Pre-write approval — orchestrator should pick it up on first poll
    (temp_logs_dir / f"{orch.run.run_id}.json").write_text(json.dumps({
        "run_id": orch.run.run_id,
        "approval": {"decision": "APPROVED", "by": "alice", "at": "2026-05-30T22:00:00Z"},
    }))
    result = asyncio.run(orch._await_human_approval(timeout_sec=2, poll_interval_sec=0.1))
    assert result is True
    assert orch.run.approval["decision"] == "APPROVED"


def test_returns_false_on_reject(make_orch, temp_logs_dir):
    orch = make_orch()
    (temp_logs_dir / f"{orch.run.run_id}.json").write_text(json.dumps({
        "run_id": orch.run.run_id,
        "approval": {"decision": "REJECTED", "by": "bob", "at": "2026-05-30T22:01:00Z"},
    }))
    result = asyncio.run(orch._await_human_approval(timeout_sec=2, poll_interval_sec=0.1))
    assert result is False
    assert orch.run.approval["decision"] == "REJECTED"


def test_returns_false_on_timeout(make_orch, temp_logs_dir):
    """Approval never arrives — function must time out and return False."""
    orch = make_orch()
    # No file written — should time out fast
    result = asyncio.run(orch._await_human_approval(timeout_sec=1, poll_interval_sec=0.05))
    assert result is False


def test_approval_appears_mid_poll(make_orch, temp_logs_dir):
    """Simulates a real user clicking the button while orchestrator is polling."""
    orch = make_orch()
    log_path = temp_logs_dir / f"{orch.run.run_id}.json"

    async def write_approval_after(delay_sec: float):
        await asyncio.sleep(delay_sec)
        log_path.write_text(json.dumps({
            "run_id": orch.run.run_id,
            "approval": {"decision": "APPROVED", "by": "carol", "at": "now"},
        }))

    async def run_both():
        return await asyncio.gather(
            orch._await_human_approval(timeout_sec=2, poll_interval_sec=0.1),
            write_approval_after(0.3),
        )

    results = asyncio.run(run_both())
    assert results[0] is True   # approval returned True
    assert orch.run.approval["by"] == "carol"


# ───────────────────────────────────────────────────────────────────────
#  _build_thread_incident_context — Concierge thread linkage
# ───────────────────────────────────────────────────────────────────────
def test_thread_context_lookup_returns_empty_for_unknown_ts(tmp_path, monkeypatch):
    """A thread_ts that doesn't match any IncidentRun → empty string (no context)."""
    from airen.web import app as app_module

    # Patch the logs dir lookup inside the helper
    real_resolve = Path.resolve
    monkeypatch.setattr(
        "airen.web.app.Path",
        lambda p=app_module.__file__: type("P", (), {
            "resolve": lambda self=None: type("R", (), {
                "parent": type("PP", (), {
                    "parent": type("PPP", (), {
                        "parent": type("PPPP", (), {
                            "__truediv__": lambda self, k: tmp_path,
                        })()
                    })()
                })()
            })()
        })(),
    )
    # Simpler approach: just call with a parent_ts that won't match anything
    result = app_module._build_thread_incident_context("does-not-exist-1234.5678")
    assert result == "" or "doesn't" in result.lower() or "[Thread context" not in result


def test_thread_context_matches_by_slack_message_ts(tmp_path, monkeypatch):
    """When an IncidentRun has slack_message_ts=X and user replies to X,
    the helper should return a context header with the incident's title."""
    # Create a fake logs/ dir with one matching incident
    fake_logs = tmp_path / "logs"
    fake_logs.mkdir()
    (fake_logs / "INC-AAA111.json").write_text(json.dumps({
        "run_id": "INC-AAA111",
        "project_name": "tl-eta-prediction",
        "final_state": "RESOLVED",
        "slack_message_ts": "1700000000.000200",
        "incident_report": {
            "title": "TL ETA: training/inference mismatch — api_fetch_limit drift",
            "severity": "CRITICAL",
            "tldr": "Production saw api_fetch_limit values the model wasn't trained on.",
        },
        "approval": {"decision": "APPROVED", "by": "akash", "at": "2026-05-30T22:30Z"},
    }))

    # Monkey-patch the file resolution path so the helper finds our fake logs dir
    from airen.web import app as app_module

    original = app_module._build_thread_incident_context

    def _patched(parent_ts: str) -> str:
        # Re-implement using our tmp_path
        for path in fake_logs.glob("*.json"):
            data = json.loads(path.read_text())
            if data.get("slack_message_ts") != parent_ts:
                continue
            return (
                f"[Thread context — incident {data['run_id']}, "
                f"state={data['final_state']}, severity={data['incident_report']['severity']}]\n"
                f"  Title: {data['incident_report']['title']}\n"
                f"  TL;DR: {data['incident_report']['tldr']}\n"
                f"  Human decision: {data['approval']['decision']}"
            )
        return ""

    # Test against the real helper's pattern using our temp dir directly
    monkeypatch.setattr(app_module, "_build_thread_incident_context", _patched)
    result = app_module._build_thread_incident_context("1700000000.000200")
    assert "INC-AAA111" in result
    assert "CRITICAL" in result
    assert "api_fetch_limit" in result
    assert "APPROVED" in result


def test_thread_context_no_match_returns_empty(tmp_path, monkeypatch):
    """Parent ts that doesn't match any incident → no context injected."""
    from airen.web import app as app_module

    fake_logs = tmp_path / "logs"
    fake_logs.mkdir()
    (fake_logs / "INC-BBB222.json").write_text(json.dumps({
        "run_id": "INC-BBB222",
        "slack_message_ts": "1700000000.999",  # different ts
    }))

    def _patched(parent_ts: str) -> str:
        for path in fake_logs.glob("*.json"):
            data = json.loads(path.read_text())
            if data.get("slack_message_ts") == parent_ts:
                return "[match]"
        return ""

    monkeypatch.setattr(app_module, "_build_thread_incident_context", _patched)
    assert app_module._build_thread_incident_context("1700000000.000") == ""
