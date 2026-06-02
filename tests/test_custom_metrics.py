"""Tests for user-declared custom metrics (airen.tools.custom_metrics)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from airen.config import MetricSpec
from airen.tools.custom_metrics import compute_custom_metrics

NOW = datetime.now(timezone.utc)


class _Cfg:
    def __init__(self, metrics):
        self.metrics = metrics


def _df(values_earlier, values_recent, col="eval.latency_ms"):
    rows = []
    for i, v in enumerate(values_earlier):
        rows.append({"start_time": (NOW - timedelta(minutes=50 - i)).isoformat(), col: v})
    for i, v in enumerate(values_recent):
        rows.append({"start_time": (NOW - timedelta(minutes=10 - i % 10)).isoformat(), col: v})
    return pd.DataFrame(rows)


def test_no_metrics_returns_empty():
    assert compute_custom_metrics(_df([1, 2], [3, 4]), _Cfg([])) == []


def test_explicit_baseline_critical():
    cfg = _Cfg([MetricSpec(name="p95_latency", attribute="eval.latency_ms", agg="p95",
                           baseline=100.0, warn_ratio=1.5, critical_ratio=2.0)])
    # recent + earlier all high → p95 ~ 300 vs baseline 100 → ratio 3 → CRITICAL
    df = _df([300] * 10, [300] * 10)
    sig = compute_custom_metrics(df, cfg)
    assert sig and sig[0].status == "CRITICAL"
    assert sig[0].ratio >= 2.0


def test_intrawindow_baseline_detects_regression():
    # No explicit baseline → earlier half is the baseline. Recent latency 3× earlier.
    cfg = _Cfg([MetricSpec(name="latency", attribute="eval.latency_ms", agg="mean",
                           warn_ratio=1.5, critical_ratio=2.0)])
    df = _df([100] * 10, [300] * 10)
    sig = compute_custom_metrics(df, cfg)
    assert sig and sig[0].status in ("WARNING", "CRITICAL")


def test_higher_is_better_flags_drop():
    # Accuracy dropped from 0.95 to 0.40 → worse for higher_is_better.
    cfg = _Cfg([MetricSpec(name="accuracy", attribute="eval.is_correct", agg="mean",
                           direction="higher_is_better", baseline=0.95,
                           warn_ratio=1.3, critical_ratio=2.0)])
    df = _df([0.4] * 10, [0.4] * 10, col="eval.is_correct")
    sig = compute_custom_metrics(df, cfg)
    assert sig and sig[0].status in ("WARNING", "CRITICAL")


def test_healthy_metric_is_healthy():
    cfg = _Cfg([MetricSpec(name="latency", attribute="eval.latency_ms", agg="mean",
                           baseline=100.0, warn_ratio=1.5, critical_ratio=2.0)])
    df = _df([100] * 10, [100] * 10)
    sig = compute_custom_metrics(df, cfg)
    assert sig and sig[0].status == "HEALTHY"
