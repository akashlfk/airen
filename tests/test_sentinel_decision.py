"""Tests for Sentinel's deterministic status decision logic.

The whole architecture pattern rests on these decisions being deterministic
Python (not LLM-driven). This test suite is the contract.
"""

from __future__ import annotations

from airen.tools.health import _decide_status


def test_zero_spans_short_window_is_warning():
    r = _decide_status(n_spans=0, overall_ratio=None, segment_ratios=[], lookback_minutes=5)
    assert r["suggested_status"] == "WARNING"
    assert r["suggested_action"] == "ALERT_HUMAN"
    assert "zero" in r["reason"].lower() or "no" in r["reason"].lower()


def test_zero_spans_long_window_is_critical():
    """An hour+ of zero traffic from production = real outage signal."""
    r = _decide_status(n_spans=0, overall_ratio=None, segment_ratios=[], lookback_minutes=60)
    assert r["suggested_status"] == "CRITICAL"
    assert r["suggested_severity_score"] >= 0.8


def test_no_error_data_is_warning():
    """Spans flowing but no MAE attribute = telemetry break, not health break."""
    r = _decide_status(n_spans=100, overall_ratio=None, segment_ratios=[], lookback_minutes=60)
    assert r["suggested_status"] == "WARNING"
    assert r["suggested_action"] == "INVESTIGATE"


def test_healthy_when_all_ratios_below_warning():
    r = _decide_status(
        n_spans=200, overall_ratio=1.05, segment_ratios=[0.95, 1.10], lookback_minutes=60
    )
    assert r["suggested_status"] == "HEALTHY"
    assert r["suggested_severity_score"] == 0.0
    assert r["suggested_action"] == "MONITOR"


def test_warning_when_segment_above_warning_threshold():
    r = _decide_status(
        n_spans=200, overall_ratio=1.05, segment_ratios=[1.5], lookback_minutes=60
    )
    assert r["suggested_status"] == "WARNING"
    assert r["suggested_action"] == "INVESTIGATE"


def test_critical_when_segment_above_critical_threshold():
    """The Fritolay scenario — one segment hits 4.21x baseline."""
    r = _decide_status(
        n_spans=200, overall_ratio=1.42, segment_ratios=[4.21, 1.58], lookback_minutes=60
    )
    assert r["suggested_status"] == "CRITICAL"
    assert r["suggested_action"] == "ALERT_HUMAN"
    assert 0.5 < r["suggested_severity_score"] <= 1.0


def test_critical_when_overall_ratio_alone_is_high():
    r = _decide_status(
        n_spans=200, overall_ratio=2.5, segment_ratios=[1.0], lookback_minutes=60
    )
    assert r["suggested_status"] == "CRITICAL"


def test_severity_score_is_bounded():
    """Severity_score must stay in [0, 1] no matter how extreme the ratio."""
    r = _decide_status(
        n_spans=10, overall_ratio=100.0, segment_ratios=[100.0], lookback_minutes=60
    )
    assert 0.0 <= r["suggested_severity_score"] <= 1.0
