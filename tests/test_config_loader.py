"""Tests for the airen.yaml config loader + service discovery."""

from __future__ import annotations

import pytest

from airen.config import (
    AirenServiceConfig,
    list_available_services,
    load_service_config,
)


def test_lists_at_least_tl_eta_and_ocean_eta():
    services = list_available_services()
    assert "tl-eta" in services
    assert "ocean-eta" in services


def test_loads_tl_eta_config_successfully():
    cfg = load_service_config("tl-eta")
    assert isinstance(cfg, AirenServiceConfig)
    assert cfg.service.name == "tl-eta"
    assert cfg.phoenix.project_name == "tl-eta-prediction"
    assert cfg.github is not None
    assert cfg.github.repo == "cloudqwest/dynamic_eta_prediction"


def test_unknown_service_raises_filenotfound():
    with pytest.raises(FileNotFoundError):
        load_service_config("nonexistent-service")


def test_sentinel_thresholds_have_sane_defaults():
    cfg = load_service_config("tl-eta")
    assert cfg.sentinel.warning_ratio < cfg.sentinel.critical_ratio
    assert cfg.sentinel.baseline_mae_minutes > 0
    assert cfg.sentinel.default_lookback_minutes > 0


def test_ocean_eta_has_different_baseline():
    """Multi-service: per-service thresholds work independently."""
    tl = load_service_config("tl-eta")
    ocean = load_service_config("ocean-eta")
    assert tl.sentinel.baseline_mae_minutes != ocean.sentinel.baseline_mae_minutes
    assert tl.phoenix.project_name != ocean.phoenix.project_name
