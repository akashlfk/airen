"""Tests for the Jira lifecycle logic on Orchestrator.

Focus is on the decision branches Airen owns:
  - close-on-pass → use cfg.jira.transition_on_pass
  - leave-open-on-fail → use cfg.jira.transition_on_fail (which may be None)
  - no-op when Jira disabled or no ticket key
"""

from __future__ import annotations

import asyncio

import pytest

from airen.adapters.jira_mock import MockJiraAdapter
from airen.config import AirenServiceConfig
from airen.schemas import ValidatorStatus, ValidatorVerdict


def _make_config(
    *,
    enable: bool = True,
    transition_on_alert: str | None = "In Progress",
    transition_on_pass: str | None = "Done",
    transition_on_fail: str | None = None,
) -> AirenServiceConfig:
    return AirenServiceConfig.model_validate(
        {
            "service": {"name": "test-svc"},
            "phoenix": {"project_name": "test-proj"},
            "jira": {
                "project_key": "TEST",
                "enable": enable,
                "transition_on_alert": transition_on_alert,
                "transition_on_pass": transition_on_pass,
                "transition_on_fail": transition_on_fail,
            },
        }
    )


def _make_orchestrator(config: AirenServiceConfig, jira_key: str | None = "TEST-42"):
    """Build an Orchestrator without touching real services."""
    from airen.agents.orchestrator import AirenOrchestrator

    orch = AirenOrchestrator(
        project_name="test-proj",
        config=config,
        cooldown_sec=0,
        post_to_slack=False,
    )
    if jira_key:
        orch.run.jira_issue_key = jira_key
    return orch


def test_resolved_pass_runs_close_transition(monkeypatch):
    """Validator PASS → orchestrator should call transition_on_pass."""
    cfg = _make_config(transition_on_pass="Done", transition_on_fail=None)
    orch = _make_orchestrator(cfg)
    orch.run.validator_phase1 = ValidatorVerdict(
        phase=1, status=ValidatorStatus.PASS, summary="all good",
        lookback_minutes=10, recommended_next_step="monitor",
    )

    fake = MockJiraAdapter()
    monkeypatch.setattr("airen.adapters.jira.get_jira_adapter", lambda *a, **k: fake)

    asyncio.run(orch._jira_on_resolved())
    assert fake.transitions == [{"issue": "TEST-42", "transition": "Done"}]


def test_resolved_fail_skips_when_no_fail_transition(monkeypatch):
    """Validator FAIL + transition_on_fail=None → no transition call."""
    cfg = _make_config(transition_on_pass="Done", transition_on_fail=None)
    orch = _make_orchestrator(cfg)
    orch.run.validator_phase1 = ValidatorVerdict(
        phase=1, status=ValidatorStatus.FAIL, summary="still broken",
        lookback_minutes=10, recommended_next_step="escalate",
    )

    fake = MockJiraAdapter()
    monkeypatch.setattr("airen.adapters.jira.get_jira_adapter", lambda *a, **k: fake)

    asyncio.run(orch._jira_on_resolved())
    assert fake.transitions == []


def test_resolved_inconclusive_uses_fail_transition_when_set(monkeypatch):
    """Validator INCONCLUSIVE + transition_on_fail='Needs Review' → call that one."""
    cfg = _make_config(transition_on_pass="Done", transition_on_fail="Needs Review")
    orch = _make_orchestrator(cfg)
    orch.run.validator_phase1 = ValidatorVerdict(
        phase=1, status=ValidatorStatus.INCONCLUSIVE, summary="??",
        lookback_minutes=10, recommended_next_step="rerun",
    )

    fake = MockJiraAdapter()
    monkeypatch.setattr("airen.adapters.jira.get_jira_adapter", lambda *a, **k: fake)

    asyncio.run(orch._jira_on_resolved())
    assert fake.transitions == [{"issue": "TEST-42", "transition": "Needs Review"}]


def test_lifecycle_noop_when_jira_disabled(monkeypatch):
    """If jira.enable=False, lifecycle helpers must not touch the adapter."""
    cfg = _make_config(enable=False)
    orch = _make_orchestrator(cfg)

    fake = MockJiraAdapter()
    monkeypatch.setattr("airen.adapters.jira.get_jira_adapter", lambda *a, **k: fake)

    asyncio.run(orch._jira_on_alert_sent("https://slack/perma"))
    asyncio.run(orch._jira_on_validation(1, ValidatorVerdict(
        phase=1, status=ValidatorStatus.PASS, summary="ok",
        lookback_minutes=10, recommended_next_step="monitor",
    )))
    assert fake.comments == []
    assert fake.transitions == []


def test_lifecycle_noop_when_no_ticket(monkeypatch):
    """If create_issue never ran (no jira_issue_key), lifecycle helpers are no-ops."""
    cfg = _make_config(enable=True)
    orch = _make_orchestrator(cfg, jira_key=None)

    fake = MockJiraAdapter()
    monkeypatch.setattr("airen.adapters.jira.get_jira_adapter", lambda *a, **k: fake)

    asyncio.run(orch._jira_on_alert_sent("https://slack/perma"))
    assert fake.comments == []
    assert fake.transitions == []


def test_alert_sent_writes_comment_and_transitions(monkeypatch):
    """After Slack post: one comment + one transition (the 'In Progress' default)."""
    cfg = _make_config()  # defaults: transition_on_alert='In Progress'
    orch = _make_orchestrator(cfg)

    fake = MockJiraAdapter()
    monkeypatch.setattr("airen.adapters.jira.get_jira_adapter", lambda *a, **k: fake)

    asyncio.run(orch._jira_on_alert_sent("https://slack/perma/abc"))
    assert len(fake.comments) == 1
    assert "https://slack/perma/abc" in fake.comments[0]["body"]
    assert fake.transitions == [{"issue": "TEST-42", "transition": "In Progress"}]
