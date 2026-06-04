"""Tests for Airen's always-on reliability battery (airen.tools.reliability).

The orchestrator's mock path never calls health.py, so these synthetic-frame
tests are the only offline exercise this safety net gets. Each builds a spans
DataFrame with a start_time column and asserts the right signal fires.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from airen.config import ReliabilityConfig
from airen.tools.reliability import compute_reliability_signals

NOW = datetime.now(timezone.utc)


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _span(mins_ago: float, **attrs) -> dict:
    d = {"start_time": (NOW - timedelta(minutes=mins_ago)).isoformat()}
    d.update(attrs)
    return d


def _checks(signals) -> dict[str, str]:
    return {s.check: s.status for s in signals}


def test_healthy_window_emits_no_signals():
    # Balanced volume across the window, recent activity, stable feature.
    rows = [_span(m, **{"input.region": "us", "eval.absolute_error_minutes": 5.0})
            for m in range(0, 20)]  # spans every minute up to ~now
    signals = compute_reliability_signals(_df(rows), lookback_minutes=60, error_attribute="eval.absolute_error_minutes")
    assert signals == []


def test_volume_drop_is_critical():
    # 20 spans in the earlier half, 1 in the recent half.
    rows = [_span(m) for m in range(40, 60)] + [_span(2)]
    signals = compute_reliability_signals(_df(rows), lookback_minutes=60)
    assert _checks(signals).get("volume_drop") == "CRITICAL"


def test_silence_warns_when_no_recent_spans():
    # All spans are >40 min old; window is 60 min → gap exceeds 50%.
    rows = [_span(m) for m in (45, 50, 55, 58)]
    signals = compute_reliability_signals(_df(rows), lookback_minutes=60)
    assert "silence" in _checks(signals)


def test_error_rate_critical():
    rows = ([_span(m, status_code="OK") for m in range(30, 45)]
            + [_span(m, status_code="ERROR") for m in range(0, 15)])
    signals = compute_reliability_signals(_df(rows), lookback_minutes=60)
    assert _checks(signals).get("error_rate") == "CRITICAL"


def test_null_rate_warns_on_partial_telemetry():
    rows = ([_span(m, **{"eval.absolute_error_minutes": 5.0}) for m in range(30, 45)]
            + [_span(m) for m in range(0, 15)])  # recent spans missing the metric
    signals = compute_reliability_signals(_df(rows), lookback_minutes=60,
                                          error_attribute="eval.absolute_error_minutes")
    assert "null_rate" in _checks(signals)


def test_input_drift_auto_flags_feature_shift():
    # Earlier half all 'us', recent half all 'eu' → large categorical PSI.
    rows = ([_span(m, **{"input.region": "us"}) for m in range(40, 60)]
            + [_span(m, **{"input.region": "eu"}) for m in range(0, 20)])
    signals = compute_reliability_signals(_df(rows), lookback_minutes=60)
    assert _checks(signals).get("input_drift_auto") in ("WARNING", "CRITICAL")


def test_opt_out_disables_a_check():
    rows = [_span(m) for m in range(40, 60)] + [_span(2)]  # would trip volume_drop
    cfg = type("C", (), {"reliability": ReliabilityConfig(disabled_checks=["volume_drop"])})()
    signals = compute_reliability_signals(_df(rows), lookback_minutes=60, config=cfg)
    assert "volume_drop" not in _checks(signals)


def test_disable_all_via_enable_flag():
    rows = [_span(m) for m in range(40, 60)] + [_span(2)]
    cfg = type("C", (), {"reliability": ReliabilityConfig(enable=False)})()
    assert compute_reliability_signals(_df(rows), lookback_minutes=60, config=cfg) == []


def test_version_drift_signal():
    from airen.tools.reliability import version_drift_signal
    # matching versions → no signal
    assert version_drift_signal("v4", "v4") is None
    # missing either side → no signal
    assert version_drift_signal("v4", None) is None
    assert version_drift_signal(None, "v3") is None
    # mismatch → baseline_stale WARNING
    sig = version_drift_signal("v4", "v3")
    assert sig is not None and sig.check == "baseline_stale" and sig.status == "WARNING"
