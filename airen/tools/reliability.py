"""Airen's default reliability battery — the always-on safety net under the
user's custom metrics.

These checks are model-agnostic: they don't know or care what the model
predicts. They catch the production failures users routinely forget to write a
metric for — traffic collapse, silent telemetry breakage, feature drift on an
attribute nobody baselined, latency regressions, schema changes.

Design choices that matter:
  • Baseline-free. Every check compares the RECENT half of the window to the
    EARLIER half. No pre-computed baseline needed, so this works the moment a
    service is onboarded — before any calibration run exists.
  • Defensive. A column that isn't present just means that check is skipped,
    never an error. Different apps emit different span shapes.
  • Pure. Input is the flattened spans DataFrame; output is a list of signals.
    Easy to unit-test with synthetic frames (the orchestrator's mock path never
    calls this, so tests are the only exercise it gets offline).
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass

import pandas as pd

DEFAULT_CHECKS = [
    "volume_drop", "silence", "error_rate", "latency_drift",
    "null_rate", "input_drift_auto", "output_drift", "schema_drift",
]


@dataclass
class ReliabilitySignal:
    check: str
    status: str                       # HEALTHY | WARNING | CRITICAL
    severity: float                   # 0–1
    detail: str
    current_value: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ───────────────────────────────────────────────────────────────────────
#  helpers
# ───────────────────────────────────────────────────────────────────────
def _split_halves(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """Split spans into (earlier, recent) by start_time at the window midpoint."""
    if "start_time" not in df.columns or len(df) < 4:
        return None
    ts = pd.to_datetime(df["start_time"], utc=True, errors="coerce")
    df = df.assign(_ts=ts).dropna(subset=["_ts"])
    if len(df) < 4:
        return None
    span = df["_ts"].max() - df["_ts"].min()
    # No real time axis → "recent vs earlier" is meaningless. A one-shot tap/peek
    # snapshot stamps every span at emission time, bunching the whole window into
    # a few seconds; splitting it just compares two arbitrary halves of one
    # instant and invents drift. Skip the temporal checks in that case. (A looped
    # tap or a live service spreads spans across real time, so this won't fire.)
    if span < pd.Timedelta(minutes=2):
        return None
    mid = df["_ts"].min() + span / 2
    earlier = df[df["_ts"] <= mid]
    recent = df[df["_ts"] > mid]
    if earlier.empty or recent.empty:
        return None
    return earlier, recent


def _psi_two(expected: list, actual: list, bins: int = 10) -> float:
    """Population Stability Index between two samples. Works for numeric
    (quantile bins) and categorical (value frequencies)."""
    if not expected or not actual:
        return 0.0
    exp_num = pd.to_numeric(pd.Series(expected), errors="coerce")
    act_num = pd.to_numeric(pd.Series(actual), errors="coerce")
    numeric = exp_num.notna().mean() > 0.8 and act_num.notna().mean() > 0.8

    if numeric:
        e, a = exp_num.dropna(), act_num.dropna()
        if e.nunique() < 2:
            return 0.0
        try:
            edges = pd.qcut(e, q=min(bins, e.nunique()), retbins=True, duplicates="drop")[1]
        except (ValueError, IndexError):
            return 0.0
        edges[0], edges[-1] = -math.inf, math.inf
        e_pct = pd.cut(e, edges).value_counts(normalize=True, sort=False)
        a_pct = pd.cut(a, edges).value_counts(normalize=True, sort=False)
        cats = e_pct.index
    else:
        e_pct = pd.Series(expected, dtype="object").astype(str).value_counts(normalize=True)
        a_pct = pd.Series(actual, dtype="object").astype(str).value_counts(normalize=True)
        cats = e_pct.index.union(a_pct.index)
        e_pct = e_pct.reindex(cats).fillna(0.0)
        a_pct = a_pct.reindex(cats).fillna(0.0)

    eps = 1e-4
    psi = 0.0
    for c in cats:
        ep = max(float(e_pct.get(c, 0.0)), eps)
        ap = max(float(a_pct.get(c, 0.0)), eps)
        psi += (ap - ep) * math.log(ap / ep)
    return round(abs(psi), 3)


_RESERVED_COLS = {
    "start_time", "end_time", "timestamp", "ts", "event_time", "created_at",
    "status_code", "status", "span_status", "request_id", "load_id", "id",
    "model_name", "model_version", "_topic", "_regime", "name", "context.span_id",
}


_ID_NAME = re.compile(
    r"(?:^|[._])(?:id|ids|uuid|guid|key|keys|hash|geohash|seq|token|loadid)(?:$|[._])",
    re.I,
)


def _drift_eligible(s: pd.Series) -> bool:
    """Is this column worth a PSI drift check? Numeric columns are fine (PSI bins
    them) unless they're effectively a unique key; categorical/text columns only
    if low-cardinality. High-cardinality categoricals, IDs, hashes and free text
    make PSI explode on noise — the recent/earlier halves just carry different
    label sets — so we skip them."""
    s = s.dropna()
    n = len(s)
    if n == 0:
        return False
    nu = _safe_nunique(s)
    if pd.api.types.is_numeric_dtype(s):
        return not (nu > 50 and nu / n > 0.9)   # not a row-id-like numeric
    return nu <= 50                              # low-cardinality categorical only


def _safe_nunique(s: pd.Series) -> int:
    try:
        return int(s.nunique())
    except TypeError:  # unhashable (nested dict/list) values
        return int(len({str(v) for v in s}))


def _feature_columns(df: pd.DataFrame, error_attribute: str) -> list[str]:
    """The attributes to watch for drift. Prefers Phoenix's `input.*` convention;
    falls back to 'every column that isn't plumbing' for raw kafka/redshift rows.
    Identifier-like and high-cardinality columns are excluded — PSI is noise there."""
    input_cols = [c for c in df.columns if c.startswith("input.")]
    if input_cols:
        candidates = input_cols
    else:
        candidates = [
            c for c in df.columns
            if c not in _RESERVED_COLS and c != error_attribute
            and not c.startswith(("eval.", "output."))
            and "predicted" not in c and "actual" not in c
        ]
    out: list[str] = []
    for c in candidates:
        if _ID_NAME.search(c):          # identifier-like by name → skip
            continue
        if _drift_eligible(df[c]):
            out.append(c)
    return out


def _band(psi: float, warn: float, crit: float) -> tuple[str, float]:
    if psi >= crit:
        return "CRITICAL", min(1.0, psi / (crit * 2))
    if psi >= warn:
        return "WARNING", min(0.6, psi / (crit * 2))
    return "HEALTHY", 0.0


# ───────────────────────────────────────────────────────────────────────
#  the battery
# ───────────────────────────────────────────────────────────────────────
def compute_reliability_signals(
    df: pd.DataFrame,
    lookback_minutes: int,
    config=None,
    error_attribute: str = "eval.absolute_error_minutes",
) -> list[ReliabilitySignal]:
    """Run every enabled default check. Returns one signal per check that ran."""
    rc = getattr(config, "reliability", None)
    if rc is not None and not rc.enable:
        return []
    disabled = set(getattr(rc, "disabled_checks", []) or [])

    def on(name: str) -> bool:
        return name not in disabled

    # tunables with fallbacks
    def t(attr, default):
        return getattr(rc, attr, default) if rc is not None else default

    out: list[ReliabilitySignal] = []
    halves = _split_halves(df)

    # ── volume drop (recent rate vs earlier rate) ──
    if on("volume_drop") and halves is not None:
        earlier, recent = halves
        e_n, r_n = len(earlier), len(recent)
        if e_n >= 2:
            ratio = r_n / e_n
            warn, crit = t("volume_drop_warn", 0.4), t("volume_drop_critical", 0.7)
            if ratio <= (1 - crit):
                out.append(ReliabilitySignal("volume_drop", "CRITICAL", 0.85,
                    f"Prediction volume fell {round((1-ratio)*100)}% (recent {r_n} vs earlier {e_n}).", round(ratio, 2)))
            elif ratio <= (1 - warn):
                out.append(ReliabilitySignal("volume_drop", "WARNING", 0.5,
                    f"Prediction volume down {round((1-ratio)*100)}% (recent {r_n} vs earlier {e_n}).", round(ratio, 2)))

    # ── silence / staleness (gap since last span) ──
    if on("silence") and "start_time" in df.columns and not df.empty:
        ts = pd.to_datetime(df["start_time"], utc=True, errors="coerce").dropna()
        if not ts.empty:
            now = pd.Timestamp.now(tz="UTC")
            gap_min = (now - ts.max()).total_seconds() / 60.0
            frac = t("silence_warn_frac", 0.5)
            if gap_min >= lookback_minutes * frac:
                out.append(ReliabilitySignal("silence", "WARNING", 0.55,
                    f"No predictions for {round(gap_min)} min (>{round(frac*100)}% of the {lookback_minutes}-min window).",
                    round(gap_min, 1)))

    # ── error-span rate (if status available) ──
    if on("error_rate"):
        status_col = next((c for c in ("status_code", "status", "span_status") if c in df.columns), None)
        if status_col and len(df) > 0:
            is_err = df[status_col].astype(str).str.upper().str.contains("ERROR", na=False)
            rate = float(is_err.mean())
            warn, crit = t("error_rate_warn", 0.05), t("error_rate_critical", 0.2)
            if rate >= crit:
                out.append(ReliabilitySignal("error_rate", "CRITICAL", min(1.0, rate * 2),
                    f"{round(rate*100,1)}% of spans are errored.", round(rate, 3)))
            elif rate >= warn:
                out.append(ReliabilitySignal("error_rate", "WARNING", 0.5,
                    f"{round(rate*100,1)}% of spans are errored.", round(rate, 3)))

    # ── latency drift (recent p95 vs earlier p95) ──
    if on("latency_drift") and halves is not None:
        lat_col = next((c for c in ("latency_ms", "eval.latency_ms", "duration_ms") if c in df.columns), None)
        if lat_col:
            earlier, recent = halves
            try:
                e_p95 = pd.to_numeric(earlier[lat_col], errors="coerce").dropna().quantile(0.95)
                r_p95 = pd.to_numeric(recent[lat_col], errors="coerce").dropna().quantile(0.95)
                if e_p95 and not math.isnan(e_p95) and e_p95 > 0:
                    ratio = r_p95 / e_p95
                    warn, crit = t("latency_warn_ratio", 1.5), t("latency_critical_ratio", 2.0)
                    if ratio >= crit:
                        out.append(ReliabilitySignal("latency_drift", "CRITICAL", min(1.0, ratio / 4),
                            f"p95 latency {round(ratio,1)}× worse (recent {round(r_p95)} vs earlier {round(e_p95)}).", round(ratio, 2)))
                    elif ratio >= warn:
                        out.append(ReliabilitySignal("latency_drift", "WARNING", 0.5,
                            f"p95 latency {round(ratio,1)}× worse.", round(ratio, 2)))
            except Exception:
                pass

    # ── null/missing rate of the error attribute (telemetry health) ──
    if on("null_rate") and error_attribute in df.columns and len(df) > 0:
        null_frac = float(df[error_attribute].isna().mean())
        warn = t("null_rate_warn", 0.2)
        if 0 < null_frac < 1.0 and null_frac >= warn:
            out.append(ReliabilitySignal("null_rate", "WARNING", 0.45,
                f"{round(null_frac*100)}% of spans are missing `{error_attribute}` — telemetry may be degrading.",
                round(null_frac, 3)))

    # ── auto input drift (PSI recent vs earlier on EVERY input.* attr) ──
    if on("input_drift_auto") and halves is not None:
        earlier, recent = halves
        warn, crit = t("drift_psi_warn", 0.10), t("drift_psi_critical", 0.25)
        worst_attr, worst_psi = None, 0.0
        for col in _feature_columns(df, error_attribute):
            psi = _psi_two(earlier[col].dropna().tolist(), recent[col].dropna().tolist())
            if psi > worst_psi:
                worst_attr, worst_psi = col, psi
        if worst_attr is not None:
            status, sev = _band(worst_psi, warn, crit)
            if status != "HEALTHY":
                out.append(ReliabilitySignal("input_drift_auto", status, sev,
                    f"Auto-drift: `{worst_attr}` shifted within the window (PSI {worst_psi}).", worst_psi))

    # ── output distribution drift ──
    if on("output_drift") and halves is not None:
        earlier, recent = halves
        out_col = next((c for c in df.columns if c.startswith("output.") or c == "eval.prediction"), None)
        if out_col:
            psi = _psi_two(earlier[out_col].dropna().tolist(), recent[out_col].dropna().tolist())
            status, sev = _band(psi, t("drift_psi_warn", 0.10), t("drift_psi_critical", 0.25))
            if status != "HEALTHY":
                out.append(ReliabilitySignal("output_drift", status, sev,
                    f"Output distribution `{out_col}` shifted within the window (PSI {psi}).", psi))

    # ── schema drift (attr populated earlier but vanished recently) ──
    if on("schema_drift") and halves is not None:
        earlier, recent = halves
        for col in [c for c in df.columns if c.startswith(("input.", "eval.", "output."))]:
            e_pop = earlier[col].notna().mean() if col in earlier else 0
            r_pop = recent[col].notna().mean() if col in recent else 0
            if e_pop > 0.5 and r_pop == 0:
                out.append(ReliabilitySignal("schema_drift", "WARNING", 0.5,
                    f"Attribute `{col}` stopped being emitted (present earlier, absent recently).", 0.0))
                break

    return out


def version_drift_signal(prod_version, baseline_version) -> ReliabilitySignal | None:
    """Flag when the deployed model_version no longer matches the baseline's.

    A new model live against an old baseline means drift checks compare apples
    to oranges — either missing real regressions or firing false alarms. This is
    the 'baseline is stale because the model changed' guardrail.
    """
    if not prod_version or not baseline_version:
        return None
    if str(prod_version).strip() == str(baseline_version).strip():
        return None
    return ReliabilitySignal(
        check="baseline_stale",
        status="WARNING",
        severity=0.5,
        detail=(f"Production model_version='{prod_version}' but the baseline was set for "
                f"'{baseline_version}'. A new model is live — recalibrate "
                f"(`python -m airen.run_calibrate`) so drift checks reference the right model."),
    )


def worst_status(signals: list[ReliabilitySignal]) -> str:
    order = {"HEALTHY": 0, "WARNING": 1, "CRITICAL": 2}
    return max((s.status for s in signals), key=lambda s: order.get(s, 0), default="HEALTHY")
