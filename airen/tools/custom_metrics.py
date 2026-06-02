"""User-declared custom metrics (#2).

Sentinel's default battery (reliability.py) is model-agnostic; this is the
domain-knowledge layer the user controls. Each `MetricSpec` in the config is
computed over the window and compared to a baseline (explicit, or the earlier
half of the window when none is given). A ratio >1 always means "worse".

Pure function over the flattened DataFrame — same testing story as the battery.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import pandas as pd


@dataclass
class CustomMetricSignal:
    name: str
    status: str                  # HEALTHY | WARNING | CRITICAL
    severity: float
    detail: str
    current_value: float | None = None
    baseline_value: float | None = None
    ratio: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _aggregate(series: pd.Series, agg: str) -> float | None:
    if agg == "count":
        return float(series.shape[0])
    if agg == "null_rate":
        return float(series.isna().mean()) if len(series) else None
    num = pd.to_numeric(series, errors="coerce").dropna()
    if agg == "rate":
        # fraction of truthy/non-zero values
        raw = series.dropna()
        if raw.empty:
            return None
        return float((pd.to_numeric(raw, errors="coerce").fillna(0) != 0).mean())
    if num.empty:
        return None
    if agg == "mean":
        return float(num.mean())
    if agg == "sum":
        return float(num.sum())
    if agg in ("p50", "p95", "p99"):
        q = {"p50": 0.50, "p95": 0.95, "p99": 0.99}[agg]
        return float(num.quantile(q))
    return float(num.mean())  # default


def _ratio(value: float, baseline: float, direction: str) -> float:
    if baseline == 0 or value == 0 or math.isnan(value) or math.isnan(baseline):
        return 0.0
    if direction == "higher_is_better":
        return round(baseline / value, 2)
    return round(value / baseline, 2)


def _split_halves(df: pd.DataFrame):
    if "start_time" not in df.columns or len(df) < 4:
        return None
    ts = pd.to_datetime(df["start_time"], utc=True, errors="coerce")
    df = df.assign(_ts=ts).dropna(subset=["_ts"])
    if len(df) < 4:
        return None
    mid = df["_ts"].min() + (df["_ts"].max() - df["_ts"].min()) / 2
    earlier, recent = df[df["_ts"] <= mid], df[df["_ts"] > mid]
    return (earlier, recent) if not earlier.empty and not recent.empty else None


def compute_custom_metrics(df: pd.DataFrame, config) -> list[CustomMetricSignal]:
    specs = getattr(config, "metrics", None) or []
    if df.empty or not specs:
        return []

    halves = _split_halves(df)
    out: list[CustomMetricSignal] = []

    for spec in specs:
        if spec.attribute not in df.columns and spec.agg != "count":
            continue
        col = df[spec.attribute] if spec.attribute in df.columns else df.iloc[:, 0]
        value = _aggregate(col, spec.agg)
        if value is None:
            continue

        # baseline: explicit, else the earlier half of the window
        baseline = spec.baseline
        if baseline is None and halves is not None:
            earlier, _ = halves
            if spec.attribute in earlier.columns or spec.agg == "count":
                ecol = earlier[spec.attribute] if spec.attribute in earlier.columns else earlier.iloc[:, 0]
                baseline = _aggregate(ecol, spec.agg)
        if baseline is None:
            # nothing to compare against — report the value, no verdict
            out.append(CustomMetricSignal(spec.name, "HEALTHY", 0.0,
                f"{spec.name} = {round(value, 3)} ({spec.agg} of {spec.attribute}); no baseline yet.",
                round(value, 3)))
            continue

        ratio = _ratio(value, baseline, spec.direction)
        if ratio >= spec.critical_ratio:
            status, sev = "CRITICAL", min(1.0, ratio / (spec.critical_ratio * 2))
        elif ratio >= spec.warn_ratio:
            status, sev = "WARNING", min(0.6, ratio / (spec.critical_ratio * 2))
        else:
            status, sev = "HEALTHY", 0.0
        out.append(CustomMetricSignal(
            spec.name, status, round(sev, 2),
            f"{spec.name} = {round(value, 3)} vs baseline {round(baseline, 3)} ({ratio}× — {spec.direction}).",
            round(value, 3), round(baseline, 3), ratio,
        ))
    return out


def worst_status(signals: list[CustomMetricSignal]) -> str:
    order = {"HEALTHY": 0, "WARNING": 1, "CRITICAL": 2}
    return max((s.status for s in signals), key=lambda s: order.get(s, 0), default="HEALTHY")
