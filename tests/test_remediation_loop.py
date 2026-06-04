"""Tests for the remediation→merge→validate loop logic (orchestrator).

The merge/CI-CD/PR steps need real GitHub, so here we test the decomposed,
deterministic pieces: the code-fix gate and the validation polling loop.
Run under AIREN_LLM_MODE=mock so the loop never sleeps.
"""

from __future__ import annotations

import asyncio

import pytest

from airen.agents.orchestrator import AirenOrchestrator
from airen.config import load_service_config
from airen.schemas import (
    RemediationActionType,
    RemediationPlan,
    SentinelVerdict,
    ValidatorStatus,
    ValidatorVerdict,
)


def _orch():
    return AirenOrchestrator(config=load_service_config("tl-eta"))


def _plan(action: RemediationActionType) -> RemediationPlan:
    return RemediationPlan(
        action_type=action,
        proposed_branch_name="airen/fix-x",
        proposed_pr_title="x",
        proposed_pr_body="x",
        confidence=0.9,
        rationale="x",
    )


def test_is_code_fix_only_for_pr_actions():
    assert AirenOrchestrator._is_code_fix(_plan(RemediationActionType.REVERT_COMMIT)) is True
    assert AirenOrchestrator._is_code_fix(_plan(RemediationActionType.HOTFIX_PR)) is True
    assert AirenOrchestrator._is_code_fix(_plan(RemediationActionType.RETRAIN_MODEL)) is False
    assert AirenOrchestrator._is_code_fix(_plan(RemediationActionType.ROLLBACK_DEPLOY)) is False
    assert AirenOrchestrator._is_code_fix(_plan(RemediationActionType.NO_ACTION)) is False
    assert AirenOrchestrator._is_code_fix(None) is False


def _verdict(status: ValidatorStatus) -> ValidatorVerdict:
    return ValidatorVerdict(
        phase=1, status=status, summary="t", lookback_minutes=10, n_spans_observed=5,
        overall_mae_before=None, overall_mae_after=None, comparisons=[],
        recommended_next_step="x",
    )


def test_validation_loop_pass_after_recovery_window(monkeypatch):
    orch = _orch()
    orch.run.sentinel_verdict = SentinelVerdict(
        project_name="tl-eta-prediction", status="CRITICAL", severity_score=0.9,
        recommended_action="ALERT_HUMAN", n_spans_analyzed=100, anomalies=[], summary="x",
    )
    orch.config.remediation.recovery_window_polls = 2
    orch.config.remediation.validation_max_attempts = 5

    calls = {"n": 0}
    async def fake_p1(**kwargs):
        calls["n"] += 1
        return _verdict(ValidatorStatus.PASS)
    monkeypatch.setattr(orch._validator, "validate_phase_1", fake_p1)

    result = asyncio.run(orch._validation_loop())
    assert result == "PASS"
    assert calls["n"] == 2  # stopped as soon as 2 consecutive PASS


def test_validation_loop_fails_after_max_attempts(monkeypatch):
    orch = _orch()
    orch.run.sentinel_verdict = SentinelVerdict(
        project_name="tl-eta-prediction", status="CRITICAL", severity_score=0.9,
        recommended_action="ALERT_HUMAN", n_spans_analyzed=100, anomalies=[], summary="x",
    )
    orch.config.remediation.validation_max_attempts = 3
    orch.config.remediation.recovery_window_polls = 2

    async def fake_fail(**kwargs):
        return _verdict(ValidatorStatus.FAIL)
    monkeypatch.setattr(orch._validator, "validate_phase_1", fake_fail)

    result = asyncio.run(orch._validation_loop())
    assert result == "FAIL"


def test_validation_loop_resets_on_intermittent_fail(monkeypatch):
    orch = _orch()
    orch.run.sentinel_verdict = SentinelVerdict(
        project_name="tl-eta-prediction", status="CRITICAL", severity_score=0.9,
        recommended_action="ALERT_HUMAN", n_spans_analyzed=100, anomalies=[], summary="x",
    )
    orch.config.remediation.recovery_window_polls = 2
    orch.config.remediation.validation_max_attempts = 5

    seq = iter([ValidatorStatus.PASS, ValidatorStatus.FAIL, ValidatorStatus.PASS, ValidatorStatus.PASS])
    async def fake_seq(**kwargs):
        return _verdict(next(seq))
    monkeypatch.setattr(orch._validator, "validate_phase_1", fake_seq)

    # PASS, FAIL(reset), PASS, PASS → recovers on the 4th poll
    result = asyncio.run(orch._validation_loop())
    assert result == "PASS"
