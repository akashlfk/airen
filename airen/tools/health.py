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

        for svc in list_available_services():
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

        for svc in list_available_services():
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

        adapter = get_s3_adapter()
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

        adapter = get_mlflow_adapter()
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

        adapter = get_redshift_adapter()
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
        return {
            "suggested_status": "WARNING",
            "suggested_severity_score": 0.4,
            "suggested_action": "INVESTIGATE",
            "reason": (
                "Spans found but no `eval.absolute_error_minutes` attribute — "
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

    spans = get_recent_spans(project_name=project_name, lookback_minutes=lookback_minutes)
    if spans.empty:
        decision = _decide_status(0, None, [], lookback_minutes)
        return {
            "project_name": project_name,
            "lookback_minutes": lookback_minutes,
            "n_spans": 0,
            "overall_mae_minutes": None,
            "baseline_mae_minutes": baseline_mae_minutes,
            "baseline_source": baseline_source,
            "overall_ratio_vs_baseline": None,
            "by_shipper": {},
            "by_api_fetch_limit": {},
            "governance_warnings": governance_warnings,
            "decision": decision,
        }

    df = flatten_mlre(spans)
    if "eval.absolute_error_minutes" not in df.columns:
        decision = _decide_status(int(len(df)), None, [], lookback_minutes)
        return {
            "project_name": project_name,
            "lookback_minutes": lookback_minutes,
            "n_spans": int(len(df)),
            "overall_mae_minutes": None,
            "baseline_mae_minutes": baseline_mae_minutes,
            "baseline_source": baseline_source,
            "overall_ratio_vs_baseline": None,
            "by_shipper": {},
            "by_api_fetch_limit": {},
            "governance_warnings": governance_warnings,
            "decision": decision,
        }

    errors = df["eval.absolute_error_minutes"].dropna().astype(float)
    overall_mae = _safe_mean(errors.tolist())

    by_shipper = {}
    if "input.shipper" in df.columns:
        for shipper, grp in df.groupby("input.shipper"):
            e = grp["eval.absolute_error_minutes"].dropna().astype(float)
            mae = _safe_mean(e.tolist())
            by_shipper[str(shipper)] = {
                "n": int(len(e)),
                "mae_minutes": round(mae, 1),
                "ratio_vs_baseline": round(mae / baseline_mae_minutes, 2),
            }

    by_fetch_limit = {}
    if "input.api_fetch_limit" in df.columns:
        for limit, grp in df.groupby("input.api_fetch_limit"):
            e = grp["eval.absolute_error_minutes"].dropna().astype(float)
            mae = _safe_mean(e.tolist())
            by_fetch_limit[str(int(limit))] = {
                "n": int(len(e)),
                "mae_minutes": round(mae, 1),
                "ratio_vs_baseline": round(mae / baseline_mae_minutes, 2),
            }

    overall_ratio = (
        round(overall_mae / baseline_mae_minutes, 2)
        if not math.isnan(overall_mae)
        else None
    )
    segment_ratios = (
        [v["ratio_vs_baseline"] for v in by_shipper.values()]
        + [v["ratio_vs_baseline"] for v in by_fetch_limit.values()]
    )

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
    )

    return {
        "project_name": project_name,
        "lookback_minutes": lookback_minutes,
        "n_spans": int(len(df)),
        "overall_mae_minutes": round(overall_mae, 1) if not math.isnan(overall_mae) else None,
        "baseline_mae_minutes": baseline_mae_minutes,
        "baseline_source": baseline_source,
        "overall_ratio_vs_baseline": overall_ratio,
        "by_shipper": by_shipper,
        "by_api_fetch_limit": by_fetch_limit,
        "psi_by_attribute": psi_by_attribute,
        "worst_psi": worst_psi,
        "governance_warnings": governance_warnings,
        "decision": decision,
    }
