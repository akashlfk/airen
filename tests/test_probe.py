"""Tests for the live-source probe's pure functions (airen.onboarding.probe)."""

from __future__ import annotations

import pandas as pd

from airen.onboarding.probe import infer_field_roles, summarize_dataframe


def test_summarize_classifies_field_kinds():
    df = pd.DataFrame({
        "risk_score": [0.1, 0.9, 0.5],
        "merchant": ["a", "b", "a"],
        "is_flagged": [True, False, True],
    })
    obs = summarize_dataframe(df, source="kafka")
    kinds = {f.name: f.kind for f in obs.fields}
    assert kinds["risk_score"] == "numeric"
    assert kinds["merchant"] == "categorical"
    assert kinds["is_flagged"] == "bool"
    assert obs.n_sampled == 3


def test_infer_roles_fraud_schema():
    df = pd.DataFrame({
        "risk_score": [0.1, 0.9, 0.5, 0.2, 0.8],      # prediction
        "label": [0, 1, 0, 0, 1],                       # actual
        "merchant_category": ["grocery", "travel", "grocery", "gas", "travel"],  # segment
        "region": ["us", "eu", "us", "us", "eu"],       # segment
        "model_version": ["v4", "v4", "v4", "v4", "v4"],
        "event_time": ["t1", "t2", "t3", "t4", "t5"],
    })
    prop = infer_field_roles(summarize_dataframe(df, "kafka"))
    fm = prop.tap_field_map
    assert fm["prediction"] == "risk_score"
    assert fm["actual"] == "label"
    assert fm["model_version"] == "model_version"
    assert fm["timestamp"] == "event_time"
    # categorical low-cardinality → segments
    assert "merchant_category" in fm["inputs"] and "region" in fm["inputs"]
    assert prop.observation["error_attribute"] == "eval.error"
    assert "input.merchant_category" in prop.observation["segments"]


def test_infer_roles_flags_missing_prediction():
    df = pd.DataFrame({"merchant": ["a", "b"], "amount": [10, 20]})
    prop = infer_field_roles(summarize_dataframe(df, "kafka"))
    assert any("prediction" in a.lower() for a in prop.ambiguities)


def test_infer_roles_explicit_error_field():
    df = pd.DataFrame({
        "predicted_eta": [600.0, 700.0, 650.0, 620.0],
        "abs_error_minutes": [40.0, 30.0, 35.0, 50.0],
        "shipper": ["A", "B", "A", "C"],
    })
    prop = infer_field_roles(summarize_dataframe(df, "kafka"))
    assert prop.tap_field_map["prediction"] == "predicted_eta"
    assert prop.tap_field_map["error"] == "abs_error_minutes"
