"""Code-based (deterministic) evaluators for an InvestigatorVerdict.

These need no LLM — they encode what a good root-cause analysis must structurally
satisfy, so they run everywhere (mock, offline, CI) and are fully unit-tested.
The LLM-as-judge evaluators (groundedness / correctness) live in judge.py and
layer on top of these.
"""

from __future__ import annotations

from typing import Any

from airen.evals.types import EvalScore

# Cause classes the Investigator's router is supposed to name (see investigator.py).
_CAUSE_CLASSES = (
    "code regression", "training-inference", "training/inference", "mismatch",
    "data drift", "concept drift", "distribution", "retrain",
    "infra", "serving", "outage", "latency", "rollback",
    "pipeline", "schema", "null", "feature",
    "version drift", "stale baseline", "recalibrat",
)

# Verbs that make a recommendation actionable rather than vague.
_ACTION_VERBS = (
    "revert", "retrain", "rollback", "roll back", "recalibrat", "recalibrate",
    "fix", "update", "scale", "page", "escalate", "investigate", "patch",
    "redeploy", "deploy", "disable", "restart", "hotfix", "merge",
)


def _evidence_count(inv: Any) -> tuple[int, int]:
    code = len(getattr(inv, "code_evidence", []) or [])
    data = len(getattr(inv, "data_evidence", []) or [])
    return code, data


def eval_has_evidence(sentinel: Any, inv: Any) -> EvalScore:
    """A verdict should cite SOMETHING — a commit, or a factual data observation.
    (Drift/infra causes legitimately have no code_evidence, but should still carry
    data_evidence, so we accept either.)"""
    code, data = _evidence_count(inv)
    if code or data:
        return EvalScore("has_evidence", 1.0, "grounded",
                         f"Cites {code} code + {data} data evidence item(s).")
    return EvalScore("has_evidence", 0.0, "unsupported",
                     "Verdict cites no code or data evidence at all.")


def eval_confidence_calibration(sentinel: Any, inv: Any) -> EvalScore:
    """Confidence should track evidence: high confidence with zero evidence is the
    classic hallucination tell; appropriate humility when evidence is thin is good."""
    conf = float(getattr(inv, "confidence", 0.0) or 0.0)
    code, data = _evidence_count(inv)
    has_ev = bool(code or data)
    if has_ev:
        if conf < 0.4:
            return EvalScore("confidence_calibration", 0.6, "under_confident",
                             f"Has evidence but confidence is only {conf:.2f}.")
        return EvalScore("confidence_calibration", 1.0, "calibrated",
                         f"Confidence {conf:.2f} is reasonable given the evidence.")
    # no evidence
    if conf >= 0.85:
        return EvalScore("confidence_calibration", 0.0, "overconfident",
                         f"Confidence {conf:.2f} with NO evidence — likely hallucinated.")
    if conf >= 0.6:
        return EvalScore("confidence_calibration", 0.5, "borderline",
                         f"Confidence {conf:.2f} with no evidence — should be lower.")
    return EvalScore("confidence_calibration", 1.0, "calibrated",
                     f"Appropriately low confidence ({conf:.2f}) given no evidence.")


def eval_recommendation_present(sentinel: Any, inv: Any) -> EvalScore:
    """A reliability verdict must end in an action a human/automation can take."""
    rec = (getattr(inv, "recommended_fix", "") or "").strip()
    if len(rec) < 12:
        return EvalScore("recommendation_actionable", 0.0, "missing",
                         "No concrete recommended fix.")
    low = rec.lower()
    if any(v in low for v in _ACTION_VERBS):
        return EvalScore("recommendation_actionable", 1.0, "actionable",
                         "Recommendation names a concrete action.")
    return EvalScore("recommendation_actionable", 0.5, "vague",
                     "Recommendation present but names no concrete action verb.")


def eval_addresses_anomaly(sentinel: Any, inv: Any) -> EvalScore:
    """The verdict should be about the anomaly Sentinel actually flagged, not a
    different metric the model wandered onto."""
    anomalies = list(getattr(sentinel, "anomalies", []) or [])
    if not anomalies:
        return EvalScore("addresses_anomaly", 1.0, "n/a",
                         "Sentinel reported no anomalies to match against.")
    sent_metrics = {(getattr(a, "metric", "") or "").lower() for a in anomalies}
    trig = getattr(inv, "triggering_anomaly", None)
    trig_metric = (getattr(trig, "metric", "") or "").lower() if trig else ""
    worst_metric = (getattr(anomalies[0], "metric", "") or "").lower()
    if trig_metric and trig_metric == worst_metric:
        return EvalScore("addresses_anomaly", 1.0, "on_target",
                         f"Targets the worst anomaly metric ({worst_metric}).")
    if trig_metric and trig_metric in sent_metrics:
        return EvalScore("addresses_anomaly", 0.7, "secondary",
                         f"Targets a real but non-dominant anomaly ({trig_metric}).")
    return EvalScore("addresses_anomaly", 0.0, "off_target",
                     f"Triggering anomaly ({trig_metric or 'none'}) isn't one Sentinel flagged.")


def eval_hypothesis_named(sentinel: Any, inv: Any) -> EvalScore:
    """The root cause should name a recognizable cause CLASS, not just restate the
    symptom."""
    hypo = (getattr(inv, "root_cause_hypothesis", "") or "").strip().lower()
    if len(hypo) < 10:
        return EvalScore("hypothesis_named", 0.0, "empty",
                         "No root-cause hypothesis stated.")
    if any(k in hypo for k in _CAUSE_CLASSES):
        return EvalScore("hypothesis_named", 1.0, "classified",
                         "Names a recognizable cause class.")
    return EvalScore("hypothesis_named", 0.5, "unclassified",
                     "States a hypothesis but doesn't name a known cause class.")


CODE_EVALUATORS = (
    eval_has_evidence,
    eval_confidence_calibration,
    eval_recommendation_present,
    eval_addresses_anomaly,
    eval_hypothesis_named,
)


def run_code_evaluators(sentinel: Any, inv: Any) -> list[EvalScore]:
    """Run all deterministic evaluators. Never raises — a broken evaluator yields
    a 0-score note rather than killing the eval pass."""
    out: list[EvalScore] = []
    for fn in CODE_EVALUATORS:
        try:
            out.append(fn(sentinel, inv))
        except Exception as e:  # noqa: BLE001
            out.append(EvalScore(fn.__name__.replace("eval_", ""), 0.0, "error",
                                 f"evaluator raised {type(e).__name__}: {e}"))
    return out
