"""Tests for the incident-injection scenarios.

The bugged_fraction logic determines what fraction of spans go bad at each
moment of the window. Test boundary cases since this drives demo behavior
that's hard to spot-check by eye.
"""

from __future__ import annotations

from demo.inject_incident import (
    FritolayScenario,
    GradualScenario,
    HealthyScenario,
    PROFILES,
    SCENARIOS,
)


def test_fritolay_is_step_function():
    s = FritolayScenario()
    duration, incident_at = 60.0, 30.0
    # Before incident: zero bugged
    assert s.bugged_fraction(0.0, duration, incident_at) == 0.0
    assert s.bugged_fraction(29.9, duration, incident_at) == 0.0
    # At and after incident: 20%
    assert s.bugged_fraction(30.0, duration, incident_at) == 0.20
    assert s.bugged_fraction(45.0, duration, incident_at) == 0.20
    assert s.bugged_fraction(60.0, duration, incident_at) == 0.20


def test_healthy_never_bugs():
    s = HealthyScenario()
    for t in [0.0, 10.0, 30.0, 60.0, 120.0]:
        assert s.bugged_fraction(t, 60.0, 30.0) == 0.0


def test_gradual_ramps_linearly():
    s = GradualScenario()
    duration, incident_at = 60.0, 30.0
    # Before incident: zero
    assert s.bugged_fraction(0.0, duration, incident_at) == 0.0
    # At incident: zero
    assert s.bugged_fraction(30.0, duration, incident_at) == 0.0
    # Midway through ramp window (45 min = halfway between 30 and 60): 10%
    assert abs(s.bugged_fraction(45.0, duration, incident_at) - 0.10) < 1e-6
    # At end: 20%
    assert abs(s.bugged_fraction(60.0, duration, incident_at) - 0.20) < 1e-6


def test_scenarios_registry_has_all_three():
    assert set(SCENARIOS.keys()) == {"fritolay", "healthy", "gradual"}
    for name, scen in SCENARIOS.items():
        assert scen.name == name
        assert scen.description  # non-empty


def test_profiles_registry_has_both_services():
    assert set(PROFILES.keys()) == {"tl-eta", "ocean-eta"}
    for name, profile in PROFILES.items():
        assert profile.name == name
        assert profile.project_name
        assert profile.healthy_value != profile.bugged_value
        # Each profile's feature builder must produce a dict including the
        # drift attribute keyed by its name.
        features = profile.feature_builder(profile.healthy_value)
        assert isinstance(features, dict)
        assert profile.drift_attribute in features


def test_profile_drift_attributes_are_distinct():
    """tl-eta and ocean-eta must drift on different attributes — that's the
    whole point of having two profiles (proves generalization)."""
    tl = PROFILES["tl-eta"]
    ocean = PROFILES["ocean-eta"]
    assert tl.drift_attribute != ocean.drift_attribute
    assert tl.project_name != ocean.project_name


def test_profile_yamls_match_drift_baselines():
    """Each profile's healthy_value must match the airen.yaml attribute_baselines
    entry, or PSI computation will incorrectly flag the healthy regime."""
    from airen.config import load_service_config

    for name, profile in PROFILES.items():
        cfg = load_service_config(name)
        baseline = cfg.sentinel.attribute_baselines.get(profile.drift_attribute)
        assert baseline is not None, (
            f"{name}: airen.yaml is missing attribute_baselines[{profile.drift_attribute!r}]"
        )
        assert str(profile.healthy_value) == str(baseline), (
            f"{name}: profile.healthy_value={profile.healthy_value!r} but "
            f"yaml attribute_baselines[{profile.drift_attribute!r}]={baseline!r}"
        )
