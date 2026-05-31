"""
Inject an ML incident into Phoenix — chronologically.

Unlike `synthetic_data.py` which emits spans "all at once" with current
timestamps, this script lays spans across a TIME WINDOW so Airen sees the
incident unfold the way it would in production:

    [t=0] ─────── healthy traffic only ─────── [t=incident_at] ─── bugged spans start ──── [t=duration]

Two modes:
  --mode replay   Emit historical spans backdated across the window. Finishes
                  in seconds — Airen can immediately query the "last hour".
                  This is the demo workhorse.
  --mode live     Emit in real time across the window. Use when you want to
                  show "watch it happen" on the dashboard while it streams in.

Scenarios (the [[feedback-generalize-not-fritolay]] memory: this must not be
locked to one bug):
  fritolay   The Feb 2026 api_fetch_limit=73 regression. Step-function rollout.
  healthy    No bug — pure baseline traffic. Useful for "before" demos.
  gradual    Rollout-style: bugged % ramps from 0 → 20% across the window.
             Models the realistic case where a buggy commit canaries slowly.

Usage:
    python -m demo.inject_incident --scenario fritolay
    python -m demo.inject_incident --scenario gradual --duration-min 120 --total 600
    python -m demo.inject_incident --mode live --rate 5

Run while `phoenix serve` is up on localhost:6006.
"""

from __future__ import annotations

import argparse
import json
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from phoenix.otel import register

# ─────────────────────────────────────────────────────────────────────
#  Service profiles — each defines how to emit one realistic span for
#  a service. Adding a new ML service to the demo = adding a profile
#  here and dropping a `services/<name>/airen.yaml`.
# ─────────────────────────────────────────────────────────────────────
@dataclass
class ServiceProfile:
    """Everything the emitter needs to know about a service to inject realistic
    spans for it. Per [[feedback-generalize-not-fritolay]]: the architecture
    must support arbitrary services, not just TL ETA."""

    name: str
    project_name: str
    model_name: str
    model_version: str
    healthy_mae_base: float
    bugged_mae_base: float
    # The attribute that flips between healthy/bugged. Phoenix sees both values
    # in the input dict; Sentinel's PSI calc compares production distribution
    # to the healthy value from `airen.yaml::sentinel.attribute_baselines`.
    drift_attribute: str
    healthy_value: object
    bugged_value: object
    # A callable that returns a fresh feature dict given the drift attribute's
    # current value. Lets each profile model its own input schema.
    feature_builder: callable


def _tl_eta_features(drift_value: object) -> dict:
    n_pings = min(int(drift_value), random.randint(50, 250))
    return {
        "load_id": f"LD{random.randint(100000, 999999)}",
        "shipper": random.choices(
            ["Fritolay", "Shipper A", "Shipper B"], weights=[0.5, 0.3, 0.2]
        )[0],
        "origin_state": random.choice(["IL", "TX", "CA", "OH"]),
        "dest_state": random.choice(["GA", "FL", "NY", "WA"]),
        "miles_remaining": random.randint(50, 2000),
        "n_pings_used": n_pings,
        "api_fetch_limit": int(drift_value),
        "seq_len": 48,
    }


def _ocean_eta_features(drift_value: object) -> dict:
    return {
        "shipment_id": f"OC{random.randint(100000, 999999)}",
        "vessel_class": str(drift_value),
        "port_pair": random.choice(
            ["SHA-LAX", "SHA-LGB", "NSA-RTM", "SIN-HAM", "NSA-LAX"]
        ),
        "miles_remaining": random.randint(1000, 12000),
        "n_route_segments": random.randint(2, 6),
        "weather_api_version": "v3",
        "is_express": random.random() < 0.15,
    }


PROFILES: dict[str, ServiceProfile] = {
    "tl-eta": ServiceProfile(
        name="tl-eta",
        project_name="tl-eta-prediction",
        model_name="tl_eta_lstm_v3",
        model_version="d9de2cc6",
        healthy_mae_base=610.0,
        bugged_mae_base=1277.0,
        drift_attribute="api_fetch_limit",
        healthy_value=184,
        bugged_value=73,
        feature_builder=_tl_eta_features,
    ),
    "ocean-eta": ServiceProfile(
        name="ocean-eta",
        project_name="ocean-eta-prediction",
        model_name="ocean_eta_xgb_v2",
        model_version="a3f2b791",
        # MAE in minutes, but represents hours-scale errors (4h baseline → 18h drift)
        healthy_mae_base=240.0,
        bugged_mae_base=1080.0,
        drift_attribute="vessel_class",
        healthy_value="container",
        bugged_value="tanker",   # new tanker route launched without retraining
        feature_builder=_ocean_eta_features,
    ),
}


# ─────────────────────────────────────────────────────────────────────
#  Scenario abstraction — what fraction of spans are bugged at time t?
# ─────────────────────────────────────────────────────────────────────
@dataclass
class Scenario:
    name: str
    description: str

    def bugged_fraction(self, t_minutes: float, duration: float, incident_at: float) -> float:
        raise NotImplementedError


class FritolayScenario(Scenario):
    """Step-function: 0% bugged before incident_at, 20% after."""

    def __init__(self) -> None:
        super().__init__(
            name="fritolay",
            description="Feb 2026 api_fetch_limit=73 regression — step function rollout",
        )

    def bugged_fraction(self, t: float, duration: float, incident_at: float) -> float:
        return 0.20 if t >= incident_at else 0.0


class HealthyScenario(Scenario):
    """Pure baseline traffic — no bug ever fires."""

    def __init__(self) -> None:
        super().__init__(name="healthy", description="No bug — pure baseline traffic")

    def bugged_fraction(self, t: float, duration: float, incident_at: float) -> float:
        return 0.0


class GradualScenario(Scenario):
    """Linear ramp from 0% → 20% bugged starting at incident_at."""

    def __init__(self) -> None:
        super().__init__(
            name="gradual",
            description="Bugged fraction ramps linearly 0 → 20% — models a canary rollout",
        )

    def bugged_fraction(self, t: float, duration: float, incident_at: float) -> float:
        if t < incident_at:
            return 0.0
        ramp_window = max(duration - incident_at, 1.0)
        progress = (t - incident_at) / ramp_window
        return min(0.20, progress * 0.20)


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in [FritolayScenario(), HealthyScenario(), GradualScenario()]
}


# ─────────────────────────────────────────────────────────────────────
#  Span generation — profile-driven so it works for any ServiceProfile
# ─────────────────────────────────────────────────────────────────────
def _predict(features: dict, profile: ServiceProfile, bugged: bool) -> tuple[float, float]:
    """Return (predicted, actual). The bugged regime adds the profile's bugged
    MAE base as Gaussian noise; healthy uses a quarter of the healthy base."""
    miles = features.get("miles_remaining", 1000)
    actual = miles * 1.2 + random.gauss(0, 60)
    if bugged:
        predicted = actual + random.gauss(0, profile.bugged_mae_base)
    else:
        predicted = actual + random.gauss(0, profile.healthy_mae_base / 4)
    return max(0.0, predicted), max(0.0, actual)


def _emit_at(
    tracer: trace.Tracer,
    profile: ServiceProfile,
    bugged: bool,
    when: datetime,
    fail: bool = False,
) -> None:
    """Emit one span anchored at `when` (UTC), shaped by `profile`. Uses the
    OTel low-level API so Phoenix sees the historical timestamp rather than
    wall-clock now."""
    drift_value = profile.bugged_value if bugged else profile.healthy_value
    features = profile.feature_builder(drift_value)
    request_id = str(uuid.uuid4())

    start_ns = int(when.timestamp() * 1e9)
    duration_ms = random.uniform(20, 120)
    end_ns = start_ns + int(duration_ms * 1e6)

    span = tracer.start_span("predict_eta", start_time=start_ns)
    try:
        span.set_attribute(
            SpanAttributes.OPENINFERENCE_SPAN_KIND,
            OpenInferenceSpanKindValues.LLM.value,
        )
        span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps(features, default=str))
        span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")
        span.set_attribute(SpanAttributes.LLM_MODEL_NAME, profile.model_name)

        span.set_attribute("mlre.request_id", request_id)
        span.set_attribute("mlre.model.version", profile.model_version)
        span.set_attribute("mlre.service", profile.name)
        span.set_attribute("mlre.timestamp", when.isoformat())
        # Flatten every feature into mlre.input.* so Phoenix's flatten_mlre
        # picks them up as columns the Sentinel can groupby + run PSI on.
        for k, v in features.items():
            # OTel can't serialize bools/None directly; coerce
            if v is None:
                continue
            span.set_attribute(f"mlre.input.{k}", v if isinstance(v, (int, float, str, bool)) else str(v))

        if fail:
            span.set_attribute("mlre.error.type", "FeatureFetchTimeout")
            span.set_status(Status(StatusCode.ERROR, "upstream feature fetch timed out"))
            return

        predicted, actual = _predict(features, profile, bugged)
        ae = abs(predicted - actual)
        span.set_attribute(
            SpanAttributes.OUTPUT_VALUE, json.dumps({"eta_minutes": predicted})
        )
        span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")
        span.set_attribute("mlre.prediction.eta_minutes", predicted)
        span.set_attribute("mlre.ground_truth.actual_minutes", actual)
        span.set_attribute("mlre.eval.absolute_error_minutes", ae)
        span.set_attribute("mlre.eval.regime", "bugged" if bugged else "healthy")
    finally:
        span.end(end_time=end_ns)


# ─────────────────────────────────────────────────────────────────────
#  Mode runners
# ─────────────────────────────────────────────────────────────────────
def _build_schedule(
    total: int, duration_min: float
) -> list[float]:
    """Pick `total` timestamps (in minutes-from-window-start) uniformly across
    the window. Sorted so we can replay/live-stream them in order."""
    return sorted(random.uniform(0, duration_min) for _ in range(total))


def _run_replay(
    tracer: trace.Tracer,
    profile: ServiceProfile,
    scenario: Scenario,
    total: int,
    duration_min: float,
    incident_at_min: float,
    fail_rate: float,
    end_time: datetime,
) -> dict[str, int]:
    """Lay all spans down with historical timestamps. Finishes in seconds."""
    counts = {"healthy": 0, "bugged": 0, "failed": 0}
    window_start = end_time - timedelta(minutes=duration_min)
    times = _build_schedule(total, duration_min)

    print(
        f"📼 REPLAY mode — laying {total} spans across "
        f"{window_start.strftime('%H:%M')} → {end_time.strftime('%H:%M')} UTC"
    )
    print(f"   Service:  {profile.name} → Phoenix project {profile.project_name!r}")
    print(f"   Scenario: {scenario.name} — {scenario.description}")
    print(f"   Drift:    {profile.drift_attribute} {profile.healthy_value!r} → {profile.bugged_value!r}")
    print(f"   Bug onset at t+{incident_at_min:.0f} min "
          f"({(window_start + timedelta(minutes=incident_at_min)).strftime('%H:%M')} UTC)")
    print()

    for i, t in enumerate(times):
        when = window_start + timedelta(minutes=t)
        roll = random.random()
        if roll < fail_rate:
            _emit_at(tracer, profile, bugged=False, when=when, fail=True)
            counts["failed"] += 1
        elif roll < (fail_rate + scenario.bugged_fraction(t, duration_min, incident_at_min)):
            _emit_at(tracer, profile, bugged=True, when=when)
            counts["bugged"] += 1
        else:
            _emit_at(tracer, profile, bugged=False, when=when)
            counts["healthy"] += 1
        if (i + 1) % 50 == 0:
            print(f"   …{i + 1}/{total} emitted")

    return counts


def _run_live(
    tracer: trace.Tracer,
    profile: ServiceProfile,
    scenario: Scenario,
    rate_per_sec: float,
    duration_min: float,
    incident_at_min: float,
    fail_rate: float,
) -> dict[str, int]:
    """Real-time emission across the window. Stays running until duration elapsed."""
    counts = {"healthy": 0, "bugged": 0, "failed": 0}
    start = datetime.now(timezone.utc)
    incident_marker_shown = False

    print(
        f"🔴 LIVE mode — emitting ~{rate_per_sec:.1f} spans/sec for {duration_min:.0f} min"
    )
    print(f"   Service:  {profile.name} → Phoenix project {profile.project_name!r}")
    print(f"   Scenario: {scenario.name} — {scenario.description}")
    print(f"   Drift:    {profile.drift_attribute} {profile.healthy_value!r} → {profile.bugged_value!r}")
    print(f"   Bug onset at t+{incident_at_min:.0f} min — watch the Sentinel light flip.")
    print()

    interval = 1.0 / rate_per_sec
    duration_sec = duration_min * 60
    next_log_at = 5
    while True:
        elapsed_sec = (datetime.now(timezone.utc) - start).total_seconds()
        if elapsed_sec >= duration_sec:
            break
        t_min = elapsed_sec / 60.0

        if not incident_marker_shown and t_min >= incident_at_min:
            print(f"\n   💥 t+{t_min:.1f} min — bug rollout begins\n")
            incident_marker_shown = True

        when = datetime.now(timezone.utc)
        roll = random.random()
        bugged_pct = scenario.bugged_fraction(t_min, duration_min, incident_at_min)
        if roll < fail_rate:
            _emit_at(tracer, profile, bugged=False, when=when, fail=True)
            counts["failed"] += 1
        elif roll < (fail_rate + bugged_pct):
            _emit_at(tracer, profile, bugged=True, when=when)
            counts["bugged"] += 1
        else:
            _emit_at(tracer, profile, bugged=False, when=when)
            counts["healthy"] += 1

        if elapsed_sec >= next_log_at:
            total = sum(counts.values())
            print(
                f"   t+{t_min:5.1f}m  total={total:4d}  "
                f"healthy={counts['healthy']:4d}  "
                f"bugged={counts['bugged']:3d}  "
                f"failed={counts['failed']:2d}  "
                f"bugged%={bugged_pct * 100:4.1f}"
            )
            next_log_at += 10

        time.sleep(interval)

    return counts


# ─────────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────────
def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--service", choices=list(PROFILES), default="tl-eta",
                   help="Which service to emit spans for. Each has its own feature schema + bug class.")
    p.add_argument("--mode", choices=["replay", "live"], default="replay")
    p.add_argument("--scenario", choices=list(SCENARIOS), default="fritolay")
    p.add_argument("--duration-min", type=float, default=60.0,
                   help="Window length in minutes (default 60)")
    p.add_argument("--incident-at", type=float, default=30.0,
                   help="Minute into window when bug starts (default 30)")
    p.add_argument("--total", type=int, default=400,
                   help="[replay mode] total spans to emit (default 400)")
    p.add_argument("--rate", type=float, default=3.0,
                   help="[live mode] spans per second (default 3)")
    p.add_argument("--fail-rate", type=float, default=0.01,
                   help="Fraction of spans that are infrastructure failures (default 0.01)")
    p.add_argument("--project", type=str, default=None,
                   help="Override the profile's Phoenix project name (rarely needed)")
    p.add_argument("--end-at", type=str, default=None,
                   help="[replay mode] end of replay window, ISO format. Defaults to now.")
    args = p.parse_args()

    profile = PROFILES[args.service]
    project_name = args.project or profile.project_name
    scenario = SCENARIOS[args.scenario]
    tracer_provider = register(project_name=project_name, auto_instrument=False)
    tracer = tracer_provider.get_tracer("mlre.demo.inject_incident")

    print(f"\n🚦 Phoenix project: {project_name}")
    if args.mode == "replay":
        end_time = datetime.fromisoformat(args.end_at) if args.end_at else datetime.now(timezone.utc)
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)
        counts = _run_replay(
            tracer=tracer,
            profile=profile,
            scenario=scenario,
            total=args.total,
            duration_min=args.duration_min,
            incident_at_min=args.incident_at,
            fail_rate=args.fail_rate,
            end_time=end_time,
        )
    else:
        counts = _run_live(
            tracer=tracer,
            profile=profile,
            scenario=scenario,
            rate_per_sec=args.rate,
            duration_min=args.duration_min,
            incident_at_min=args.incident_at,
            fail_rate=args.fail_rate,
        )

    tracer_provider.force_flush()
    total = sum(counts.values())
    print(f"\n✅ Done. {total} spans emitted — {counts}")
    bug_pct = counts["bugged"] / max(total, 1) * 100
    print(f"   Bugged share: {bug_pct:.1f}%")
    print(f"   Phoenix UI: http://localhost:6006  →  project '{project_name}'")
    print(f"   Now run:    AIREN_LLM_MODE=mock python -m airen.run_orchestrator {profile.name}")


if __name__ == "__main__":
    main()
