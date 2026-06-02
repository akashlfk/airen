"""Calibration run — the one-time "learn what normal looks like" step.

After onboarding writes the config from the *code*, calibration connects to the
*live telemetry* and answers three questions:

  1. Does reality match the config?  (is `error_attribute` actually in the spans?
     do the `segments` exist? what attributes is the service really emitting?)
  2. What does normal look like right now?  (current metric, volume, per-segment
     values, modal feature values)
  3. Should we write those norms back as the baselines Sentinel compares against?

It is honest about its central assumption: calibration treats the current
window as "healthy". If the model is already broken, say so before writing.

`analyze_spans()` is pure (testable with synthetic frames). `calibrate()` does
the live fetch + interactive report + optional write-back.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from airen.config import AirenServiceConfig

CALIBRATION_DIR = Path(__file__).resolve().parent.parent.parent / "baselines"


@dataclass
class SchemaCheck:
    error_attribute: str
    error_present: bool
    segments_present: dict[str, bool] = field(default_factory=dict)
    available_eval: list[str] = field(default_factory=list)
    available_input: list[str] = field(default_factory=list)


@dataclass
class CalibrationResult:
    project: str
    window_minutes: int
    n_spans: int
    model_version: str | None = None
    schema: SchemaCheck | None = None
    overall_metric: float | None = None
    volume_per_hour: float = 0.0
    segment_counts: dict[str, dict] = field(default_factory=dict)
    attribute_modes: dict[str, str] = field(default_factory=dict)
    reliability_signals: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "service": self.project,
            "generated_at": date.today().isoformat(),
            "window_minutes": self.window_minutes,
            "n_spans": self.n_spans,
            "model_version": self.model_version,
            "baseline_mae": self.overall_metric,
            "attribute_baselines": self.attribute_modes,
            "source": "calibration",
        }


# ───────────────────────────────────────────────────────────────────────
#  pure analysis
# ───────────────────────────────────────────────────────────────────────
def analyze_spans(df: pd.DataFrame, config: AirenServiceConfig, window_minutes: int) -> CalibrationResult:
    obs = config.observation
    error_attr = obs.error_attribute
    segments = list(obs.segments)
    res = CalibrationResult(
        project=config.phoenix.project_name, window_minutes=window_minutes, n_spans=int(len(df))
    )
    if df.empty:
        return res

    # stamp the model_version this baseline represents (drives version-drift checks)
    for col in ("model_version", "mlre.model_version"):
        if col in df.columns:
            vals = df[col].dropna()
            if not vals.empty:
                res.model_version = str(vals.mode().iloc[0])
                break

    eval_cols = sorted(c for c in df.columns if c.startswith("eval."))
    input_cols = sorted(c for c in df.columns if c.startswith("input."))
    res.schema = SchemaCheck(
        error_attribute=error_attr,
        error_present=error_attr in df.columns,
        segments_present={s: (s in df.columns) for s in segments},
        available_eval=eval_cols,
        available_input=input_cols,
    )

    # current headline metric
    if error_attr in df.columns:
        vals = pd.to_numeric(df[error_attr], errors="coerce").dropna()
        if not vals.empty:
            res.overall_metric = round(float(vals.mean()), 2)

    res.volume_per_hour = round(len(df) / max(window_minutes / 60.0, 1e-9), 1)

    # per-segment counts
    for seg in segments:
        if seg in df.columns:
            res.segment_counts[seg] = {
                str(k): int(v) for k, v in df[seg].value_counts().head(10).items()
            }

    # modal value per attribute_of_interest → PSI baseline candidates
    attrs = config.github.attributes_of_interest if config.github else []
    for name in attrs:
        col = name if name in df.columns else f"input.{name}"
        if col in df.columns:
            non_null = df[col].dropna()
            if not non_null.empty:
                mode = Counter(str(v) for v in non_null).most_common(1)[0][0]
                res.attribute_modes[name] = mode

    # run the reliability battery so calibration also reports current safety state
    try:
        from airen.tools.reliability import compute_reliability_signals

        res.reliability_signals = compute_reliability_signals(df, window_minutes, config, error_attr)
    except Exception:
        res.reliability_signals = []

    return res


# ───────────────────────────────────────────────────────────────────────
#  live fetch + interactive driver
# ───────────────────────────────────────────────────────────────────────
def calibrate(
    config: AirenServiceConfig,
    *,
    window_minutes: int = 1440,
    interactive: bool = True,
    write_back: bool = True,
) -> CalibrationResult | None:
    from airen.onboarding import io

    project = config.phoenix.project_name
    print()
    print("═" * 78)
    print(f"  CALIBRATION  ·  service={config.service.name}  ·  phoenix={project}")
    print(f"  Window: last {window_minutes} min  ·  endpoint: {config.phoenix.collector_endpoint}")
    print("═" * 78)

    src = config.serving.prediction_source or "phoenix"
    print(f"\n  ⏳ Fetching live predictions (source: {src})…")
    try:
        from airen.tools.prediction_source import fetch_predictions

        df = fetch_predictions(config, window_minutes)
    except Exception as e:  # noqa: BLE001
        print(f"\n  ❌ Could not read the {src} feed: {type(e).__name__}: {e}")
        if src == "phoenix":
            print("     Check PHOENIX_COLLECTOR_ENDPOINT and that Phoenix is running,")
            print(f"     and that '{project}' has spans.")
        return None

    res = analyze_spans(df, config, window_minutes)
    _print_report(res, config)

    if res.n_spans == 0:
        print("\n  ✗ No spans found — nothing to calibrate.")
        print(f"     Is your service emitting to Phoenix project '{project}'?")
        return res

    if write_back and (not interactive or io.ask_bool(
        "\n  Write these as the baselines Sentinel compares against?", default=True
    )):
        _write_artifact(config.service.name, res)
    return res


def _print_report(res: CalibrationResult, config: AirenServiceConfig) -> None:
    print(f"\n  Spans analyzed : {res.n_spans}  (~{res.volume_per_hour}/hour)")
    if res.schema is not None:
        sc = res.schema
        mark = "✓" if sc.error_present else "✗ MISSING"
        print(f"\n  Schema check (does the config match the live data?):")
        print(f"    error_attribute  {sc.error_attribute:<32} [{mark}]")
        for seg, present in sc.segments_present.items():
            print(f"    segment          {seg:<32} [{'✓' if present else '✗ MISSING'}]")
        if not sc.error_present:
            print(f"    ↳ live eval.* attributes actually present: {sc.available_eval or '—'}")
            print( "      Fix observation.error_attribute in the yaml to one of these.")
        missing_segs = [s for s, p in sc.segments_present.items() if not p]
        if missing_segs:
            print(f"    ↳ live input.* attributes present: {sc.available_input or '—'}")

    if res.overall_metric is not None:
        unit = config.observation.metric_unit
        print(f"\n  Current metric : {res.overall_metric} {unit}  ({config.observation.error_attribute})")
        print(f"    → proposed sentinel.baseline ≈ {res.overall_metric} {unit}")
    if res.attribute_modes:
        print("\n  Modal feature values (proposed PSI baselines):")
        for k, v in res.attribute_modes.items():
            print(f"    {k} = {v}")
    if res.reliability_signals:
        print("\n  ⚠️  Reliability signals in the calibration window (is 'now' actually healthy?):")
        for s in res.reliability_signals:
            print(f"    [{s.status}] {s.check}: {s.detail}")
        print("    NOTE: calibration assumes the current window is healthy. If these")
        print("          look like real problems, fix them BEFORE writing baselines.")


def _any_baseline_exists(config: AirenServiceConfig) -> bool:
    """True if SOME baseline is already available, so we don't re-establish every
    cycle. (Staleness/version drift is handled separately by health.py.)"""
    cal = CALIBRATION_DIR / config.service.name / "calibration.json"
    if cal.is_file():
        return True
    if config.s3 is not None and config.s3.enable:
        return True
    if config.redshift is not None and config.redshift.enable:
        return True
    return False


def ensure_baseline(
    config: AirenServiceConfig,
    *,
    window_minutes: int = 1440,
    min_spans: int = 50,
    write: bool = True,
) -> str | None:
    """Auto-establish a baseline from live telemetry when none exists — Airen
    computes it ITSELF, no manual extract/upload. Returns a status line or None.

    Guardrails:
      • Only runs when no baseline exists yet (cheap no-op otherwise).
      • Needs >= min_spans of data — won't baseline on a trickle.
      • Refuses to baseline an UNHEALTHY window (won't bake an incident in as
        'normal'); reports that instead and leaves the baseline unset.
    """
    if _any_baseline_exists(config):
        return None

    from airen.tools.prediction_source import fetch_predictions

    df = fetch_predictions(config, window_minutes)
    if df.empty or len(df) < min_spans:
        return None  # not enough live data yet — try again next cycle

    res = analyze_spans(df, config, window_minutes)
    crit = [s for s in res.reliability_signals if getattr(s, "status", "") == "CRITICAL"]
    if crit:
        return (f"deferred baseline — the current window looks unhealthy "
                f"({crit[0].detail}). Not baselining an incident as 'normal'.")
    if res.overall_metric is None:
        return None
    if write:
        _write_artifact(config.service.name, res)
    return (f"established baseline from {res.n_spans} spans "
            f"(model {res.model_version or '?'}, metric ≈ {res.overall_metric}).")


def _write_artifact(service: str, res: CalibrationResult) -> None:
    out_dir = CALIBRATION_DIR / service
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "calibration.json"
    path.write_text(json.dumps(res.to_dict(), indent=2))
    print(f"\n  ✅ Wrote calibration baseline → {path}")
    print("     Sentinel will use this as its baseline (below S3 training baselines,")
    print("     above the hand-tuned yaml values).")
