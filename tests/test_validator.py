"""Tests for Validator's pure verdict-construction logic."""

from __future__ import annotations

from airen.agents.validator import _verdict_from_snapshot, _lookup_segment_ratio
from airen.schemas import (
    Anomaly,
    HealthStatus,
    RecommendedAction,
    SentinelVerdict,
    ValidatorStatus,
)


def _sentinel_with_anomalies(*anomalies: Anomaly) -> SentinelVerdict:
    return SentinelVerdict(
        project_name="tl-eta-prediction",
        n_spans_analyzed=200,
        status=HealthStatus.CRITICAL,
        severity_score=0.85,
        anomalies=list(anomalies),
        recommended_action=RecommendedAction.ALERT_HUMAN,
        summary="Test",
    )


def test_no_data_after_remediation_is_no_data():
    sentinel = _sentinel_with_anomalies(
        Anomaly(metric="mae", segment="api_fetch_limit=73", current_value=631.5,
                baseline_value=150.0, ratio=4.21, description="x"),
    )
    snapshot = {"n_spans": 0}
    v = _verdict_from_snapshot(phase=1, snapshot=snapshot, sentinel=sentinel, lookback_minutes=10)
    assert v.status == ValidatorStatus.NO_DATA
    assert v.recommended_next_step == "monitor"


def test_full_recovery_is_pass():
    """All flagged segments back below warning ratio."""
    sentinel = _sentinel_with_anomalies(
        Anomaly(metric="mae", segment="api_fetch_limit=73", current_value=631.5,
                baseline_value=150.0, ratio=4.21, description="x"),
    )
    snapshot = {
        "n_spans": 120,
        "overall_mae_minutes": 130.0,
        "by_api_fetch_limit": {"73": {"n": 0, "mae_minutes": 0.0, "ratio_vs_baseline": 0.0}},
        "by_shipper": {},
    }
    v = _verdict_from_snapshot(phase=1, snapshot=snapshot, sentinel=sentinel, lookback_minutes=10)
    assert v.status == ValidatorStatus.PASS
    assert v.recommended_next_step == "monitor"


def test_partial_recovery_is_inconclusive():
    """Worst ratio dropped but still above warning threshold."""
    sentinel = _sentinel_with_anomalies(
        Anomaly(metric="mae", segment="api_fetch_limit=73", current_value=631.5,
                baseline_value=150.0, ratio=4.21, description="x"),
    )
    snapshot = {
        "n_spans": 80,
        "overall_mae_minutes": 250.0,
        "by_api_fetch_limit": {"73": {"n": 12, "mae_minutes": 240.0, "ratio_vs_baseline": 1.6}},
        "by_shipper": {},
    }
    v = _verdict_from_snapshot(phase=1, snapshot=snapshot, sentinel=sentinel, lookback_minutes=10)
    assert v.status == ValidatorStatus.INCONCLUSIVE


def test_still_critical_is_fail():
    sentinel = _sentinel_with_anomalies(
        Anomaly(metric="mae", segment="api_fetch_limit=73", current_value=631.5,
                baseline_value=150.0, ratio=4.21, description="x"),
    )
    snapshot = {
        "n_spans": 80,
        "overall_mae_minutes": 580.0,
        "by_api_fetch_limit": {"73": {"n": 20, "mae_minutes": 590.0, "ratio_vs_baseline": 3.9}},
        "by_shipper": {},
    }
    v = _verdict_from_snapshot(phase=1, snapshot=snapshot, sentinel=sentinel, lookback_minutes=10)
    assert v.status == ValidatorStatus.FAIL
    assert v.recommended_next_step == "re-investigate"


def test_segment_lookup_for_api_fetch_limit():
    snapshot = {"by_api_fetch_limit": {"73": {"ratio_vs_baseline": 1.5}}}
    assert _lookup_segment_ratio(snapshot, "api_fetch_limit=73") == 1.5


def test_segment_lookup_for_shipper():
    snapshot = {"by_shipper": {"Fritolay": {"ratio_vs_baseline": 1.6}}}
    assert _lookup_segment_ratio(snapshot, "shipper=Fritolay") == 1.6


def test_segment_lookup_returns_none_for_unknown():
    assert _lookup_segment_ratio({}, "foo=bar") is None
    assert _lookup_segment_ratio({}, "overall") is None
