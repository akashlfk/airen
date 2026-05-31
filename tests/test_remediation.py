"""Tests for Remediation's action-decision logic."""

from __future__ import annotations

from airen.agents.remediation import Remediator
from airen.schemas import (
    Anomaly,
    CodeEvidence,
    HealthStatus,
    InvestigatorVerdict,
    RecommendedAction,
    RemediationActionType,
    SentinelVerdict,
)


def _sentinel_critical() -> SentinelVerdict:
    return SentinelVerdict(
        project_name="tl-eta-prediction",
        n_spans_analyzed=200,
        status=HealthStatus.CRITICAL,
        severity_score=0.85,
        anomalies=[
            Anomaly(metric="mae", segment="api_fetch_limit=73", current_value=631.5,
                    baseline_value=150.0, ratio=4.21, description="x"),
        ],
        recommended_action=RecommendedAction.ALERT_HUMAN,
        summary="test",
    )


def _investigator(confidence: float, with_evidence: bool = True) -> InvestigatorVerdict:
    code = []
    if with_evidence:
        code.append(
            CodeEvidence(
                commit_sha="6270ca55abcdef1234567890",
                commit_author="akashlfk",
                commit_date="2026-05-27T10:07:23+00:00",
                file_path="predict/processing/location_service/LocationService.py",
                pr_number=741,
                pr_url="https://github.com/cloudqwest/dynamic_eta_prediction/pull/741",
            )
        )
    return InvestigatorVerdict(
        triggering_anomaly=Anomaly(metric="mae", segment="api_fetch_limit=73",
                                   current_value=631.5, baseline_value=150.0,
                                   ratio=4.21, description="x"),
        root_cause_hypothesis="suspect commit",
        confidence=confidence,
        code_evidence=code,
        data_evidence=["bla"],
        recommended_fix="revert",
        further_investigation_needed=[],
    )


def test_no_action_when_sentinel_healthy():
    sentinel = SentinelVerdict(
        project_name="tl-eta-prediction",
        n_spans_analyzed=200,
        status=HealthStatus.HEALTHY,
        severity_score=0.0,
        anomalies=[],
        recommended_action=RecommendedAction.MONITOR,
        summary="all good",
    )
    r = Remediator()
    action, target = r._decide_action(sentinel, _investigator(0.95))
    assert action == RemediationActionType.NO_ACTION


def test_no_action_when_no_code_evidence():
    sentinel = _sentinel_critical()
    r = Remediator()
    action, _ = r._decide_action(sentinel, _investigator(0.95, with_evidence=False))
    assert action == RemediationActionType.NO_ACTION


def test_no_action_when_confidence_too_low():
    sentinel = _sentinel_critical()
    r = Remediator()
    action, _ = r._decide_action(sentinel, _investigator(confidence=0.4))
    assert action == RemediationActionType.NO_ACTION


def test_revert_when_high_confidence_with_evidence():
    sentinel = _sentinel_critical()
    r = Remediator()
    action, target = r._decide_action(sentinel, _investigator(confidence=0.9))
    assert action == RemediationActionType.REVERT_COMMIT
    assert target.commit_sha.startswith("6270ca55")
    assert target.pr_number == 741


def test_branch_name_contains_short_sha_and_incident_id():
    name = Remediator._propose_branch_name("6270ca55abcdef", "INC-A3F2B7")
    assert name.startswith("airen/revert-")
    assert "6270ca55" in name
    assert "inc-a3f2b7" in name  # lowercase
    assert " " not in name


def test_branch_name_handles_missing_sha():
    name = Remediator._propose_branch_name(None, "INC-FOO")
    assert "unknown" in name


# ───────────────────────────────────────────────────────────────────────
#  _git_run — token scrubbing in error messages
# ───────────────────────────────────────────────────────────────────────
def test_git_run_scrubs_token_from_error(monkeypatch):
    """If git stderr contains the auth token, the raised RuntimeError must not."""
    from airen.agents.remediation import _git_run

    fake_token = "ghp_LeakyTokenShouldNotAppear1234567890"
    # Force git to fail on a guaranteed-bad URL that embeds the token.
    bad_url = f"https://x-access-token:{fake_token}@github.com/nonexistent/nope-{__name__}.git"
    try:
        _git_run(["clone", "--depth", "1", bad_url, "/tmp/airen-test-clone-target"],
                 token=fake_token, timeout=30)
    except RuntimeError as e:
        assert fake_token not in str(e), "Token leaked in error message!"
        assert "<TOKEN_REDACTED>" in str(e) or "git" in str(e)
        return
    raise AssertionError("Expected git clone to fail on nonexistent repo")
