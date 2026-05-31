"""Tests for the training/inference mismatch detector — the smoking-gun cross-ref."""

from __future__ import annotations

import os

import pytest

from airen.tools.investigation import detect_training_inference_mismatch


@pytest.fixture(autouse=True)
def force_mock_mlflow():
    """All these tests use the mock MLflow adapter (training run has NO api_fetch_limit)."""
    prev = os.environ.get("AIREN_MLFLOW_MODE")
    os.environ["AIREN_MLFLOW_MODE"] = "mock"
    yield
    if prev is None:
        os.environ.pop("AIREN_MLFLOW_MODE", None)
    else:
        os.environ["AIREN_MLFLOW_MODE"] = prev


def test_fritolay_scenario_returns_high_severity_mismatch():
    """The whole reason this tool exists — training has no `api_fetch_limit`,
    production has it set to 73 → high-severity mismatch."""
    r = detect_training_inference_mismatch("tl-eta-prod", "api_fetch_limit", "73")
    assert r["mismatch"] is True
    assert r["severity"] == "high"
    assert r["training_value"] is None
    assert r["observed_value"] == "73"
    assert "training/inference mismatch" in r["description"].lower()


def test_matching_value_is_no_mismatch():
    """seq_len=48 is in training params — no mismatch when production agrees."""
    r = detect_training_inference_mismatch("tl-eta-prod", "seq_len", "48")
    assert r["mismatch"] is False
    assert r["severity"] == "low"
    assert r["training_value"] == "48"


def test_differing_value_is_medium_severity():
    """Training set seq_len=48, production using 72 → mismatch but not silent."""
    r = detect_training_inference_mismatch("tl-eta-prod", "seq_len", "72")
    assert r["mismatch"] is True
    assert r["severity"] == "medium"
    assert r["training_value"] == "48"
    assert r["observed_value"] == "72"
