"""Tests for the Sentinel baseline-resolution chain: S3 → Redshift → yaml → default.

These are critical because they determine what "drift" means for every
service. The wrong baseline source = wrong verdict.
"""

from __future__ import annotations

import pytest

from airen.adapters.s3 import get_s3_adapter
from airen.adapters.s3_mock import MOCK_BASELINES, MockS3Adapter
from airen.tools.health import (
    DEFAULT_BASELINE_MAE_MINUTES,
    _baseline_from_redshift_rolling,
    _baseline_from_s3,
    _resolve_service_config,
)


@pytest.fixture(autouse=True)
def isolate_local_baselines(monkeypatch, tmp_path):
    """The MockS3Adapter prefers ./baselines/<service>/*.json when present.
    Tests must be deterministic regardless of what the user has extracted
    locally — point the adapter at an empty tempdir so it falls back to
    the canned MOCK_BASELINES dict.
    """
    import airen.adapters.s3_mock as s3_mock_module
    monkeypatch.setattr(s3_mock_module, "LOCAL_BASELINES_DIR", tmp_path / "baselines")


# ───────────────────────────────────────────────────────────────────────
#  S3 mock adapter
# ───────────────────────────────────────────────────────────────────────
def test_s3_mock_returns_known_tl_eta_baseline():
    a = MockS3Adapter()
    b = a.get_training_baseline("tl-eta", "d9de2cc6")
    assert b is not None
    assert b["metrics"]["mae_minutes"] == 118.4
    # Training distribution should reveal the healthy api_fetch_limit
    assert b["attribute_distributions"]["api_fetch_limit"] == {"184": 1.0}


def test_s3_mock_latest_returns_newest_by_trained_at():
    a = MockS3Adapter()
    latest = a.get_training_baseline("tl-eta")
    # Only one tl-eta baseline exists; latest must equal that one
    assert latest["model_version"] == "d9de2cc6"


def test_s3_mock_get_object_for_latest_json():
    a = MockS3Adapter()
    raw = a.get_object("baselines/ocean-eta/latest.json")
    assert raw is not None
    import json
    parsed = json.loads(raw)
    assert parsed["service"] == "ocean-eta"


def test_s3_mock_returns_none_for_unknown_service():
    a = MockS3Adapter()
    assert a.get_training_baseline("nonexistent-svc") is None
    assert a.get_object("baselines/nonexistent-svc/v1.json") is None


def test_s3_mock_list_baselines():
    a = MockS3Adapter()
    keys = a.list_baselines("tl-eta")
    assert "baselines/tl-eta/d9de2cc6.json" in keys


def test_s3_factory_defaults_to_mock(monkeypatch):
    monkeypatch.delenv("AIREN_S3_MODE", raising=False)
    a = get_s3_adapter()
    assert type(a).__name__ == "MockS3Adapter"


# ───────────────────────────────────────────────────────────────────────
#  _baseline_from_s3 — Sentinel's S3 entrypoint
# ───────────────────────────────────────────────────────────────────────
def test_baseline_from_s3_returns_training_mae_and_modal_attrs():
    cfg = _resolve_service_config("tl-eta-prediction")
    assert cfg is not None
    assert cfg.s3.enable is True   # tl-eta yaml has s3.enable=true
    mae, attrs, label = _baseline_from_s3(cfg, default=DEFAULT_BASELINE_MAE_MINUTES)
    assert mae == 118.4
    assert attrs["api_fetch_limit"] == "184"
    assert label and label.startswith("s3:training@")


def test_baseline_from_s3_disabled_returns_default():
    """When s3.enable=false, no S3 call at all."""
    cfg = _resolve_service_config("tl-eta-prediction")
    cfg.s3.enable = False
    mae, attrs, label = _baseline_from_s3(cfg, default=999.0)
    assert mae == 999.0
    assert attrs == {}
    assert label is None


def test_baseline_from_s3_returns_modal_value_for_skewed_distribution():
    """ocean-eta has vessel_class {'container': 0.94, 'bulker': 0.06}.
    Modal value 'container' should be picked as the PSI baseline."""
    cfg = _resolve_service_config("ocean-eta-prediction")
    mae, attrs, label = _baseline_from_s3(cfg, default=DEFAULT_BASELINE_MAE_MINUTES)
    assert attrs["vessel_class"] == "container"


# ───────────────────────────────────────────────────────────────────────
#  _baseline_from_redshift_rolling — gated by redshift.enable
# ───────────────────────────────────────────────────────────────────────
def test_redshift_rolling_disabled_returns_default():
    cfg = _resolve_service_config("tl-eta-prediction")
    assert cfg.redshift.enable is False   # default
    mae, label = _baseline_from_redshift_rolling(cfg, default=150.0)
    assert mae == 150.0
    assert label is None


def test_redshift_rolling_no_config_returns_default():
    mae, label = _baseline_from_redshift_rolling(None, default=42.0)
    assert mae == 42.0
    assert label is None
