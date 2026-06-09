"""Health computation tools — what Sentinel calls.

The agent doesn't compute statistics OR decide status (LLMs are unreliable
at both). These tools do the math AND the status decision; the agent only
writes the narrative summary.

Why: a monitoring agent that occasionally returns wrong status is worse than
no monitoring at all. Deterministic Python beats stochastic LLM for binary
decisions. LLM still adds value writing the human-readable summary.

Returned dicts are JSON-serialisable so ADK can pass them back into Gemini's
context window. Keep them small — a few hundred bytes per call.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from airen.adapters.phoenix import flatten_mlre, get_recent_spans
from airen.tools.reliability import compute_reliability_signals, worst_status


# Baseline MAE for tl-eta-prediction. Derived from healthy-regime synthetic data
# (mean ~120 min, headroom for noise → 150). When we wire BigQuery on Day 6+ this
# becomes dynamic: rolling 7-day MAE from tl_eta_datamart.
DEFAULT_BASELINE_MAE_MINUTES = 150.0

WARNING_RATIO = 1.2
CRITICAL_RATIO = 2.0


def _safe_mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


# ───────────────────────────────────────────────────────────────────────
#  PSI (Population Stability Index)
# ───────────────────────────────────────────────────────────────────────
PSI_WARNING = 0.10
PSI_CRITICAL = 0.25


def compute_psi_against_baseline_value(
    observed_values: list,
    baseline_value: object,
    floor: float = 0.0001,
) -> float:
    """Simplified PSI for the 'training had ONE value, production drifted' case.

    Equivalent to standard PSI where the baseline distribution is
    {baseline_value: 1.0} and the current distribution is the observed
    histogram. Returns a non-negative float; >0.25 = major drift.

    Args:
        observed_values: list of observed values from the current window.
        baseline_value: the value training expected (compared as string).
        floor: minimum probability to avoid log(0) — standard PSI trick.
    """
    if not observed_values:
        return 0.0
    from collections import Counter

    base_str = str(baseline_value)
    counts = Counter(str(v) for v in observed_values)
    total = sum(counts.values())
    psi = 0.0
    for value, count in counts.items():
        cur_pct = count / total
        base_pct = 1.0 if value == base_str else 0.0
        cur_pct = max(cur_pct, floor)
        base_pct = max(base_pct, floor)
        psi += (cur_pct - base_pct) * math.log(cur_pct / base_pct)
    return round(psi, 4)


def _psi_band(psi: float) -> str:
    if psi >= PSI_CRITICAL:
        return "critical"
    if psi >= PSI_WARNING:
        return "warning"
    return "healthy"


def _auto_resolve_baselines(project_name: str) -> dict[str, str]:
    """Look up `attribute_baselines` from any services/<svc>/airen.yaml that
    matches this Phoenix project. Returns {} if no match — PSI is then skipped.
    """
    try:
        from airen.config import list_available_services, load_service_config

        for svc in list_available_services(include_demo=True):
            try:
                cfg = load_service_config(svc)
            except Exception:
                continue
            if cfg.phoenix.project_name == project_name and cfg.sentinel.attribute_baselines:
                return dict(cfg.sentinel.attribute_baselines)
    except Exception:
        pass
    return {}


def _resolve_service_config(project_name: str):
    """Return the AirenServiceConfig matching this Phoenix project, or None.

    Used to look up S3/Redshift settings without forcing every caller of
    get_model_health_snapshot to pass the config in.
    """
    try:
        from airen.config import list_available_services, load_service_config

        for svc in list_available_services(include_demo=True):
            try:
                cfg = load_service_config(svc)
            except Exception:
                continue
            if cfg.phoenix.project_name == project_name:
                return cfg
    except Exception:
        pass
    return None


def _baseline_from_s3(cfg, default: float) -> tuple[float, dict[str, str], str | None]:
    """Try to fetch training-time baseline from S3.

    Returns (mae_baseline, attribute_baselines, source_label).
    Falls back to (default, {}, None) on any error.
    """
    if cfg is None or cfg.s3 is None or not cfg.s3.enable:
        return default, {}, None
    try:
        from airen.adapters.s3 import get_s3_adapter

        adapter = get_s3_adapter(bucket=cfg.s3.bucket, region=cfg.s3.region)
        baseline = adapter.get_training_baseline(
            service=cfg.service.name,
            model_version=cfg.s3.model_version,
        )
        if baseline is None:
            return default, {}, None
        mae = float(baseline.get("metrics", {}).get("mae_minutes", default))
        # Pick the modal value from each attribute distribution as the baseline.
        attr_baselines: dict[str, str] = {}
        for attr, dist in (baseline.get("attribute_distributions") or {}).items():
            if not isinstance(dist, dict) or not dist:
                continue
            modal_value = max(dist.items(), key=lambda kv: kv[1])[0]
            attr_baselines[attr] = modal_value
        version = baseline.get("model_version", "?")
        return mae, attr_baselines, f"s3:training@{version}"
    except Exception as e:
        print(f"⚠️  S3 baseline lookup failed: {type(e).__name__}: {e}")
        return default, {}, None


def _audit_model_governance(cfg) -> list[dict]:
    """For each model_family registered for this service, ask MLflow whether
    its training code is reproducible. Returns a list of dicts, one per
    ungoverned model — empty list = all models healthy.

    Quiet fallbacks: any error returns []. Governance is an additive signal
    over the existing health checks; we never let a flaky MLflow lookup
    crash the main snapshot.
    """
    if cfg is None or not cfg.model_registry:
        return []
    if cfg.mlflow is None or not cfg.mlflow.enable:
        return []
    try:
        from airen.adapters.mlflow_adapter import get_mlflow_adapter

        adapter = get_mlflow_adapter(
            tracking_uri=cfg.mlflow.tracking_uri if cfg.mlflow else None
        )
        warnings: list[dict] = []
        for entry in cfg.model_registry:
            try:
                prov = adapter.get_training_provenance(
                    entry.experiment_id, expected_repo=entry.expected_repo
                )
            except Exception as e:
                warnings.append({
                    "model_family": entry.model_family,
                    "experiment_id": entry.experiment_id,
                    "has_traceability": False,
                    "warnings": [f"MLflow lookup failed: {type(e).__name__}: {e}"],
                    "source_path": None,
                    "git_commit": None,
                })
                continue
            if not prov.get("has_git_traceability") or prov.get("reproducibility_warnings"):
                warnings.append({
                    "model_family": entry.model_family,
                    "experiment_id": entry.experiment_id,
                    "has_traceability": bool(prov.get("has_git_traceability")),
                    "warnings": list(prov.get("reproducibility_warnings") or []),
                    "source_path": prov.get("source_path"),
                    "git_commit": prov.get("git_commit"),
                })
        try:
            adapter.close()
        except Exception:
            pass
        return warnings
    except Exception as e:
        print(f"⚠️  Governance audit skipped: {type(e).__name__}: {e}")
        return []


def _baseline_from_redshift_rolling(
    cfg,
    default: float,
    window_days: int = 7,
) -> tuple[float, str | None]:
    """Compute rolling-window MAE from the actuals table.

    Returns (mae, source_label). Returns (default, None) on any failure so
    Sentinel doesn't crash if the FK VPN is down.
    """
    if cfg is None or cfg.redshift is None or not cfg.redshift.enable:
        return default, None
    actuals_table = cfg.redshift.actuals_table
    pred_table = cfg.redshift.table
    if not actuals_table or not pred_table:
        return default, None
    try:
        from datetime import timedelta

        from airen.adapters.redshift import get_redshift_adapter

        adapter = get_redshift_adapter(config=cfg.redshift)
        since = datetime.now(timezone.utc) - timedelta(days=window_days)
        # Inner-join predictions with actuals on load_id; compute mean abs error.
        # Identifier safety: redshift_real validates table names; mock ignores SQL.
        sql = f"""
            SELECT AVG(ABS(p.predicted_eta_minutes - a.actual_eta_minutes)) AS mae
            FROM {pred_table} p
            INNER JOIN {actuals_table} a ON p.load_id = a.load_id
            WHERE p.timestamp >= %s
        """
        rows = adapter.query(sql, (since,))
        adapter.close()
        if not rows or rows[0].get("mae") is None:
            return default, None
        mae = float(rows[0]["mae"])
        # Sanity floor — a degenerate query shouldn't push baseline absurdly low
        if mae < 10.0:
            return default, None
        return mae, f"redshift:{window_days}d-rolling"
    except Exception as e:
        print(f"⚠️  Redshift rolling baseline failed: {type(e).__name__}: {e}")
        return default, None


def _decide_status(
    n_spans: int,
    overall_ratio: float | None,
    segment_ratios: list[float],
    lookback_minutes: int,
    worst_psi: float = 0.0,
    error_attr: str = "eval.absolute_error_minutes",
    accuracy_expected: bool = True,
) -> dict:
    """Pure-Python status decision. No LLM involved."""
    # No-data branch
    if n_spans == 0:
        # An hour+ of silence from a production model = real outage signal
        is_critical = lookback_minutes >= 60
        return {
            "suggested_status": "CRITICAL" if is_critical else "WARNING",
            "suggested_severity_score": 0.85 if is_critical else 0.5,
            "suggested_action": "ALERT_HUMAN",
            "reason": (
                f"Zero prediction spans in a {lookback_minutes}-min window — "
                "the model appears to be down or not receiving traffic."
            ),
        }

    # No-error-data branch (spans exist but missing MAE attribute)
    if overall_ratio is None:
        # Prediction-only feed (no ground truth by design): a missing error
        # attribute is EXPECTED, not a broken pipeline. Don't cry wolf — let the
        # reliability battery (drift/volume/schema) decide health instead.
        if not accuracy_expected:
            return {
                "suggested_status": "HEALTHY",
                "suggested_severity_score": 0.0,
                "suggested_action": "MONITOR",
                "reason": (
                    f"No `{error_attr}` in this feed — it's prediction-only (no ground "
                    "truth), so accuracy isn't computable. EXPECTED for this service; "
                    "health is judged on drift / volume / schema."
                ),
            }
        return {
            "suggested_status": "WARNING",
            "suggested_severity_score": 0.4,
            "suggested_action": "INVESTIGATE",
            "reason": (
                f"Spans found but no `{error_attr}` attribute — "
                "telemetry pipeline may be broken."
            ),
        }

    # Normal MAE thresholding, AUGMENTED with PSI
    max_ratio = max([overall_ratio] + segment_ratios)
    mae_critical = max_ratio >= CRITICAL_RATIO
    mae_warning = max_ratio >= WARNING_RATIO
    psi_critical = worst_psi >= PSI_CRITICAL
    psi_warning = worst_psi >= PSI_WARNING

    if mae_critical or psi_critical:
        # Choose the dominant signal for the reason text
        if psi_critical and (worst_psi / PSI_CRITICAL) > (max_ratio / CRITICAL_RATIO):
            reason = (
                f"PSI drift {worst_psi:.2f} exceeds critical threshold "
                f"{PSI_CRITICAL} — feature distribution has changed significantly."
            )
        else:
            reason = (
                f"Worst-segment MAE ratio {max_ratio:.2f}x exceeds critical "
                f"threshold {CRITICAL_RATIO}."
            )
        score = max(min(1.0, max_ratio / 5.0), min(1.0, worst_psi / 0.5))
        return {
            "suggested_status": "CRITICAL",
            "suggested_severity_score": round(score, 2),
            "suggested_action": "ALERT_HUMAN",
            "reason": reason,
        }
    if mae_warning or psi_warning:
        score = max(min(0.6, max_ratio / 4.0), min(0.6, worst_psi / 0.4))
        reason = (
            f"PSI drift {worst_psi:.2f} above warning threshold {PSI_WARNING}"
            if psi_warning and worst_psi >= max(max_ratio - 1, 0)
            else f"Worst-segment MAE ratio {max_ratio:.2f}x exceeds warning threshold {WARNING_RATIO}"
        )
        return {
            "suggested_status": "WARNING",
            "suggested_severity_score": round(score, 2),
            "suggested_action": "INVESTIGATE",
            "reason": reason + ".",
        }
    return {
        "suggested_status": "HEALTHY",
        "suggested_severity_score": 0.0,
        "suggested_action": "MONITOR",
        "reason": (
            f"All MAE ratios within tolerance (worst {max_ratio:.2f}x), "
            f"PSI drift {worst_psi:.2f} below warning threshold {PSI_WARNING}."
        ),
    }


def _live_model_version(df) -> str | None:
    """Most-common model_version in the live feed (if the spans carry it)."""
    for col in ("model_version", "mlre.model_version"):
        if col in df.columns:
            vals = df[col].dropna()
            if not vals.empty:
                try:
                    return str(vals.mode().iloc[0])
                except Exception:
                    return str(vals.iloc[0])
    return None


def _baseline_model_version(cfg) -> str | None:
    """The model_version the active baseline represents — calibration artifact
    first (cheap/local), then the S3 training baseline."""
    if cfg is None:
        return None
    try:
        import json
        from pathlib import Path

        p = Path(__file__).resolve().parent.parent.parent / "baselines" / cfg.service.name / "calibration.json"
        if p.is_file():
            v = json.loads(p.read_text()).get("model_version")
            if v:
                return str(v)
    except Exception:
        pass
    try:
        if cfg.s3 is not None and cfg.s3.enable:
            from airen.adapters.s3 import get_s3_adapter

            b = get_s3_adapter(bucket=cfg.s3.bucket, region=cfg.s3.region).get_training_baseline(
                cfg.service.name, cfg.s3.model_version
            )
            if b and b.get("model_version"):
                return str(b["model_version"])
    except Exception:
        pass
    return None


def _baseline_from_calibration(cfg) -> tuple[float | None, dict, str | None]:
    """Read a local calibration baseline (baselines/<service>/calibration.json),
    written by `python -m airen.run_calibrate`. This is the OPERATIONAL baseline
    — 'what normal looked like' from live telemetry — used when no S3 training
    baseline exists. Returns (mae, attribute_baselines, source_label)."""
    if cfg is None:
        return None, {}, None
    try:
        import json
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent.parent / "baselines" / cfg.service.name / "calibration.json"
        if not path.is_file():
            return None, {}, None
        data = json.loads(path.read_text())
        mae = data.get("baseline_mae")
        attrs = data.get("attribute_baselines") or {}
        if mae is None and not attrs:
            return None, {}, None
        return mae, attrs, f"calibration@{data.get('generated_at', '?')}"
    except Exception:
        return None, {}, None


_STATUS_ORDER = {"HEALTHY": 0, "MONITOR": 0, "WARNING": 1, "CRITICAL": 2}


def _merge_reliability(decision: dict, signals: list) -> dict:
    """Escalate the accuracy-based decision with Airen's reliability battery.

    The reliability signals are the safety net: even if the headline metric
    looks fine, a volume collapse or feature drift should still wake the
    Investigator. Status only ever escalates, never downgrades.
    """
    if not signals:
        return decision
    rel_status = worst_status(signals)
    cur = decision.get("suggested_status", "HEALTHY")
    if _STATUS_ORDER.get(rel_status, 0) <= _STATUS_ORDER.get(cur, 0):
        return decision  # accuracy signal already dominates
    worst = max(signals, key=lambda s: s.severity)
    label = getattr(worst, "check", getattr(worst, "name", "signal"))
    return {
        **decision,
        "suggested_status": rel_status,
        "suggested_action": "ALERT_HUMAN" if rel_status == "CRITICAL" else "INVESTIGATE",
        "suggested_severity_score": max(decision.get("suggested_severity_score", 0.0), round(worst.severity, 2)),
        "reason": f"{decision.get('reason', '').rstrip('.')}. Signal[{label}]: {worst.detail}",
    }


def get_model_health_snapshot(
    project_name: str,
    lookback_minutes: int = 60,
    baseline_mae_minutes: float = DEFAULT_BASELINE_MAE_MINUTES,
    attribute_baselines: dict | None = None,
) -> dict:
    """Compute a health snapshot for an ML model project.

    Use this tool to assess whether an ML model's recent predictions are drifting
    or degrading. Returns overall MAE, segment breakdowns, ratios vs baseline,
    AND per-attribute PSI (Population Stability Index) for feature drift.

    Args:
        project_name: Phoenix project, e.g. "tl-eta-prediction".
        lookback_minutes: Time window to analyse. Defaults to 60.
        baseline_mae_minutes: Reference MAE for ratio computation. Defaults to 150.
        attribute_baselines: Map of `attribute_name` → expected baseline value,
            e.g. {"api_fetch_limit": "184"}. PSI is computed for each. Defaults
            to None (no PSI).

    Returns:
        Dict with overall MAE, segment breakdowns by shipper and api_fetch_limit,
        baseline comparison, PSI per attribute, and decision.
    """
    # ── Baseline resolution priority (high → low):
    #    1. Explicit kwargs (caller passed them)
    #    2. S3 training-time baseline (richest — actual training MAE + distributions)
    #    3. Redshift rolling-window baseline (dynamic — adapts as model improves)
    #    4. airen.yaml hand-tuned values
    #    5. DEFAULT_BASELINE_MAE_MINUTES constant
    cfg = _resolve_service_config(project_name)
    baseline_source = "explicit-kwarg" if baseline_mae_minutes != DEFAULT_BASELINE_MAE_MINUTES else None
    if attribute_baselines is None:
        # Try S3 first (richest source)
        s3_mae, s3_attrs, s3_label = _baseline_from_s3(cfg, default=DEFAULT_BASELINE_MAE_MINUTES)
        if s3_label is not None:
            if baseline_source is None:
                baseline_mae_minutes = s3_mae
                baseline_source = s3_label
            attribute_baselines = s3_attrs
        # Calibration (operational baseline from live telemetry) — used when no
        # S3 training baseline resolved. See airen.run_calibrate.
        if baseline_source is None or not attribute_baselines:
            cal_mae, cal_attrs, cal_label = _baseline_from_calibration(cfg)
            if cal_label is not None:
                if baseline_source is None and cal_mae is not None:
                    baseline_mae_minutes = cal_mae
                    baseline_source = cal_label
                if not attribute_baselines:
                    attribute_baselines = cal_attrs
        # Otherwise fall back to yaml
        if not attribute_baselines:
            attribute_baselines = _auto_resolve_baselines(project_name)
    # Redshift rolling baseline (MAE only — distributions don't fit here)
    if baseline_source is None:
        rs_mae, rs_label = _baseline_from_redshift_rolling(cfg, default=DEFAULT_BASELINE_MAE_MINUTES)
        if rs_label is not None:
            baseline_mae_minutes = rs_mae
            baseline_source = rs_label
    # Fall back to yaml sentinel.baseline_mae_minutes if still unresolved
    if baseline_source is None and cfg is not None:
        baseline_mae_minutes = cfg.sentinel.baseline_mae_minutes
        baseline_source = "yaml"
    if baseline_source is None:
        baseline_source = "default"

    # Governance audit runs regardless of data presence — it's independent
    # of Phoenix spans (it asks MLflow about the registered models).
    governance_warnings = _audit_model_governance(cfg)

    # ── Observation schema (which span attribute is the error, which to
    #    segment on, and the metric direction) — config-driven, TL defaults.
    if cfg is not None:
        error_attr = cfg.observation.error_attribute
        segment_attrs = list(cfg.observation.segments)
        direction = cfg.observation.metric_direction
    else:
        error_attr = "eval.error"
        segment_attrs = []
        direction = "lower_is_better"

    def _ratio(value: float) -> float:
        """Ratio where >1 always means 'worse than baseline', regardless of
        whether the metric is an error (lower better) or a score (higher better)."""
        if baseline_mae_minutes == 0 or value == 0 or math.isnan(value):
            return 0.0
        if direction == "higher_is_better":
            return round(baseline_mae_minutes / value, 2)
        return round(value / baseline_mae_minutes, 2)

    def _seg_key(v) -> str:
        try:
            f = float(v)
            return str(int(f)) if f.is_integer() else str(v)
        except (TypeError, ValueError):
            return str(v)

    # Fetch via the prediction-source dispatcher so Sentinel reads the right
    # feed (Phoenix / Kafka / Redshift) per serving.prediction_source. Falls
    # back to direct Phoenix when no service config resolved.
    if cfg is not None:
        from airen.tools.prediction_source import fetch_predictions

        df_fetch = fetch_predictions(cfg, lookback_minutes)
    else:
        df_fetch = flatten_mlre(get_recent_spans(project_name=project_name, lookback_minutes=lookback_minutes))
    if df_fetch.empty:
        decision = _decide_status(0, None, [], lookback_minutes, error_attr=error_attr)
        return {
            "project_name": project_name,
            "lookback_minutes": lookback_minutes,
            "n_spans": 0,
            "overall_mae_minutes": None,
            "baseline_mae_minutes": baseline_mae_minutes,
            "baseline_source": baseline_source,
            "overall_ratio_vs_baseline": None,
            "by_segment": {},
            "by_shipper": {},
            "by_api_fetch_limit": {},
            "reliability_signals": [],
            "governance_warnings": governance_warnings,
            "decision": decision,
        }

    df = df_fetch

    # Airen's always-on reliability battery — model-agnostic, baseline-free.
    # Runs even when the accuracy metric is absent (volume/silence/schema drift
    # are still meaningful), and can escalate the verdict on its own.
    try:
        reliability_signals = compute_reliability_signals(df, lookback_minutes, cfg, error_attr)
    except Exception:
        reliability_signals = []

    # Baseline lifecycle: has the deployed model_version drifted away from the
    # version this baseline was set for? If so, the baseline is stale.
    try:
        from airen.tools.reliability import version_drift_signal

        vd = version_drift_signal(_live_model_version(df), _baseline_model_version(cfg))
        if vd is not None:
            reliability_signals.append(vd)
    except Exception:
        pass

    reliability_dicts = [s.to_dict() for s in reliability_signals]

    # User-declared custom metrics (#2) — domain knowledge on top of the battery.
    try:
        from airen.tools.custom_metrics import compute_custom_metrics

        custom_metric_signals = compute_custom_metrics(df, cfg)
    except Exception:
        custom_metric_signals = []
    custom_metric_dicts = [s.to_dict() for s in custom_metric_signals]

    if error_attr not in df.columns:
        accuracy_expected = bool(getattr(getattr(cfg, "observation", None), "has_ground_truth", True)) if cfg else True
        decision = _decide_status(int(len(df)), None, [], lookback_minutes,
                                  error_attr=error_attr, accuracy_expected=accuracy_expected)
        decision = _merge_reliability(decision, reliability_signals + custom_metric_signals)
        return {
            "project_name": project_name,
            "lookback_minutes": lookback_minutes,
            "n_spans": int(len(df)),
            "overall_mae_minutes": None,
            "baseline_mae_minutes": baseline_mae_minutes,
            "baseline_source": baseline_source,
            "overall_ratio_vs_baseline": None,
            "by_segment": {},
            "by_shipper": {},
            "by_api_fetch_limit": {},
            "reliability_signals": reliability_dicts,
            "custom_metrics": custom_metric_dicts,
            "governance_warnings": governance_warnings,
            "decision": decision,
        }

    errors = df[error_attr].dropna().astype(float)
    overall_mae = _safe_mean(errors.tolist())

    # Generic per-segment breakdown for every configured segment attribute.
    by_segment: dict[str, dict] = {}
    for seg_attr in segment_attrs:
        if seg_attr not in df.columns:
            continue
        seg_map: dict[str, dict] = {}
        for value, grp in df.groupby(seg_attr):
            e = grp[error_attr].dropna().astype(float)
            mae = _safe_mean(e.tolist())
            seg_map[_seg_key(value)] = {
                "n": int(len(e)),
                "mae_minutes": round(mae, 1),    # generic metric value (key kept for back-compat)
                "metric_value": round(mae, 1),
                "ratio_vs_baseline": _ratio(mae),
            }
        by_segment[seg_attr] = seg_map

    # Back-compat aliases — downstream (validator, sentinel prompt, tests) still
    # reference these specific keys when the TL segments are configured.
    by_shipper = by_segment.get("input.shipper", {})
    by_fetch_limit = by_segment.get("input.api_fetch_limit", {})

    overall_ratio = _ratio(overall_mae) if not math.isnan(overall_mae) else None
    segment_ratios = [
        v["ratio_vs_baseline"]
        for seg_map in by_segment.values()
        for v in seg_map.values()
    ]

    # ── PSI drift detection ──
    psi_by_attribute: dict[str, dict] = {}
    if attribute_baselines:
        for attr_name, baseline_value in attribute_baselines.items():
            # Phoenix flattens dotted attrs — `input.api_fetch_limit` etc.
            phoenix_col = f"input.{attr_name}"
            if phoenix_col not in df.columns:
                continue
            observed = df[phoenix_col].dropna().tolist()
            psi = compute_psi_against_baseline_value(
                observed_values=observed, baseline_value=baseline_value
            )
            psi_by_attribute[attr_name] = {
                "n_observed": len(observed),
                "baseline_value": str(baseline_value),
                "observed_distinct_values": sorted({str(v) for v in observed}),
                "psi": psi,
                "band": _psi_band(psi),
            }

    # Surface the worst PSI as a separate signal in the decision
    worst_psi = max((v["psi"] for v in psi_by_attribute.values()), default=0.0)
    decision = _decide_status(
        n_spans=int(len(df)),
        overall_ratio=overall_ratio,
        segment_ratios=segment_ratios,
        lookback_minutes=lookback_minutes,
        worst_psi=worst_psi,
        error_attr=error_attr,
    )
    decision = _merge_reliability(decision, reliability_signals + custom_metric_signals)

    return {
        "project_name": project_name,
        "lookback_minutes": lookback_minutes,
        "n_spans": int(len(df)),
        "overall_mae_minutes": round(overall_mae, 1) if not math.isnan(overall_mae) else None,
        "baseline_mae_minutes": baseline_mae_minutes,
        "baseline_source": baseline_source,
        "overall_ratio_vs_baseline": overall_ratio,
        "by_segment": by_segment,
        "by_shipper": by_shipper,
        "by_api_fetch_limit": by_fetch_limit,
        "reliability_signals": reliability_dicts,
        "custom_metrics": custom_metric_dicts,
        "psi_by_attribute": psi_by_attribute,
        "worst_psi": worst_psi,
        "governance_warnings": governance_warnings,
        "decision": decision,
    }
