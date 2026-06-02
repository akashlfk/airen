"""Tests for calibration analysis + the prediction-source dispatcher."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pandas as pd

from airen.config import load_service_config
from airen.onboarding.calibrate import analyze_spans
from airen.tools.prediction_source import fetch_predictions

NOW = datetime.now(timezone.utc)


def test_analyze_spans_validates_schema_and_learns_norms():
    cfg = load_service_config("tl-eta")  # error_attr=eval.absolute_error_minutes, segs include input.shipper
    rows = []
    for i in range(20):
        rows.append({
            "start_time": (NOW - timedelta(minutes=60 - i * 2)).isoformat(),
            "eval.absolute_error_minutes": 120.0 + i,
            "input.shipper": "Fritolay" if i % 2 else "ShipperA",
            "input.api_fetch_limit": 184,
        })
    res = analyze_spans(pd.DataFrame(rows), cfg, window_minutes=60)
    assert res.n_spans == 20
    assert res.schema.error_present is True
    assert res.schema.segments_present["input.shipper"] is True
    assert res.overall_metric is not None and res.overall_metric > 0
    # modal api_fetch_limit should be learned as a PSI baseline candidate
    assert res.attribute_modes.get("api_fetch_limit") == "184"


def test_analyze_spans_flags_missing_schema():
    cfg = load_service_config("tl-eta")
    # spans that DON'T carry the configured error attribute
    rows = [{"start_time": NOW.isoformat(), "eval.something_else": 5}]
    res = analyze_spans(pd.DataFrame(rows), cfg, window_minutes=60)
    assert res.schema.error_present is False


def test_fetch_predictions_kafka_mock_returns_rows():
    os.environ["AIREN_KAFKA_MODE"] = "mock"
    cfg = load_service_config("ocean-eta")  # prediction_source=kafka
    df = fetch_predictions(cfg, window_minutes=60, max_rows=50)
    assert not df.empty
    assert "start_time" in df.columns  # normalized from message 'timestamp'


def test_fetch_predictions_empty_on_bad_source():
    cfg = load_service_config("tl-eta")
    # phoenix source but no Phoenix running in tests → empty frame, no raise
    df = fetch_predictions(cfg, window_minutes=60)
    assert isinstance(df, pd.DataFrame)


def _inmem_config(name: str):
    from airen.config import AirenServiceConfig, PhoenixConfig, ServiceInfo
    return AirenServiceConfig(
        service=ServiceInfo(name=name),
        phoenix=PhoenixConfig(project_name=f"{name}-prediction"),
    )


def test_ensure_baseline_auto_computes_from_healthy_window(monkeypatch):
    import airen.onboarding.calibrate as cal

    cfg = _inmem_config("unit-autocal")  # no s3/redshift → no existing baseline
    # in-mem config uses the generic default observation.error_attribute = eval.error
    rows = [{
        "start_time": (NOW - timedelta(minutes=120 - i)).isoformat(),
        "eval.error": 100.0 + (i % 5),
        "region": "A" if i % 2 else "B",
        "model_version": "v1",
    } for i in range(60)]
    monkeypatch.setattr("airen.tools.prediction_source.fetch_predictions",
                        lambda *a, **k: pd.DataFrame(rows))

    status = cal.ensure_baseline(cfg, write=False)  # write=False → no disk artifact
    assert status is not None and status.startswith("established")
    assert "v1" in status


def test_ensure_baseline_refuses_unhealthy_window(monkeypatch):
    import airen.onboarding.calibrate as cal

    cfg = _inmem_config("unit-autocal-bad")
    # volume collapse: 50 spans early, 1 recent → CRITICAL volume_drop
    rows = [{"start_time": (NOW - timedelta(minutes=120 - i)).isoformat(),
             "eval.absolute_error_minutes": 100.0, "model_version": "v1"} for i in range(55)]
    rows.append({"start_time": (NOW - timedelta(minutes=1)).isoformat(),
                 "eval.absolute_error_minutes": 100.0, "model_version": "v1"})
    monkeypatch.setattr("airen.tools.prediction_source.fetch_predictions",
                        lambda *a, **k: pd.DataFrame(rows))

    status = cal.ensure_baseline(cfg, write=False)
    assert status is not None and "deferred baseline" in status
