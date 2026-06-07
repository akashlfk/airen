"""The self-improvement loop — how Airen gets better from its own observability.

Two mechanisms, both feeding off Airen's own eval scores + resolved-incident
history (the Arize "agent improves from its own observability data" bonus):

  1. Reflection (in-run): when the evals grade a verdict below the bar, Airen
     feeds the verdict its OWN critique and re-investigates. The second attempt is
     traced + re-graded, so the score lift is visible in Phoenix.

  2. Cross-run learning: every Validator-CONFIRMED incident is appended to a small
     local store keyed by anomaly class. On a future incident of the same class,
     those confirmed root causes are recalled and injected into the Investigator
     prompt as worked examples — so the agent compounds what it has learned.

The store is intentionally a plain JSONL file (auditable, diffable, no DB).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from airen.evals.types import RcaEvalResult

_STORE = Path(__file__).resolve().parent.parent.parent / "baselines" / "learning" / "incidents.jsonl"


# ───────────────────────────────────────────────────────────────────────
#  Reflection (in-run self-improvement)
# ───────────────────────────────────────────────────────────────────────
def should_reflect(result: RcaEvalResult) -> bool:
    """Reflect only when the verdict failed the eval bar AND reflection is enabled."""
    if os.environ.get("AIREN_EVAL_REFLECT", "1").strip().lower() in ("0", "false", "no"):
        return False
    return not result.passed


def max_reflections() -> int:
    try:
        return max(0, int(os.environ.get("AIREN_EVAL_MAX_REFLECTIONS", "1")))
    except ValueError:
        return 1


def reflection_feedback(result: RcaEvalResult) -> str:
    """Turn the eval critique into an instruction the Investigator reads on retry."""
    return (
        "\n\n--- SELF-REVIEW (your previous verdict was auto-graded and FELL SHORT) ---\n"
        f"Overall eval score: {result.overall:.2f} (threshold {result.pass_threshold:.2f}).\n"
        f"What was weak: {result.critique()}\n"
        "Re-investigate addressing these gaps specifically: gather the evidence you "
        "were missing using your tools, name the cause CLASS explicitly, set a "
        "confidence that matches the evidence you actually have, and give a concrete, "
        "actionable recommendation. Do NOT repeat an unsupported claim."
    )


# ───────────────────────────────────────────────────────────────────────
#  Cross-run learning store
# ───────────────────────────────────────────────────────────────────────
def _worst_metric(sentinel: Any) -> str:
    anomalies = list(getattr(sentinel, "anomalies", []) or [])
    if not anomalies:
        return ""
    return (getattr(anomalies[0], "metric", "") or "").lower()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_incident(run: Any) -> bool:
    """Append a resolved incident to the learning store. We keep both confirmed and
    unconfirmed outcomes (with a `validated` flag) but only recall confirmed ones.
    Best-effort; never raises."""
    try:
        sentinel = getattr(run, "sentinel_verdict", None)
        inv = getattr(run, "investigator_verdict", None)
        if sentinel is None or inv is None:
            return False
        phase1 = getattr(run, "validator_phase1", None)
        validated = False
        try:
            from airen.schemas import ValidatorStatus

            validated = phase1 is not None and phase1.status == ValidatorStatus.PASS
        except Exception:
            pass
        rec = {
            "ts": _now(),
            "run_id": getattr(run, "run_id", None),
            "service": getattr(run, "project_name", None),
            "metric": _worst_metric(sentinel),
            "root_cause": (getattr(inv, "root_cause_hypothesis", "") or "").strip(),
            "recommended_fix": (getattr(inv, "recommended_fix", "") or "").strip(),
            "confidence": float(getattr(inv, "confidence", 0.0) or 0.0),
            "validated": validated,
        }
        if not rec["metric"] or not rec["root_cause"]:
            return False
        _STORE.parent.mkdir(parents=True, exist_ok=True)
        with _STORE.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        return True
    except Exception:
        return False


def _load_store() -> list[dict]:
    if not _STORE.exists():
        return []
    out: list[dict] = []
    try:
        for line in _STORE.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []
    return out


def recall_similar(sentinel: Any, *, k: int = 3, validated_only: bool = True) -> list[dict]:
    """Return up to k past incidents of the same anomaly CLASS (most recent first),
    to seed the Investigator with what worked before."""
    metric = _worst_metric(sentinel)
    if not metric:
        return []
    rows = [r for r in _load_store() if r.get("metric") == metric]
    if validated_only:
        rows = [r for r in rows if r.get("validated")]
    rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return rows[:k]


def exemplars_block(similar: list[dict]) -> str:
    """Format recalled incidents as a worked-examples block for the prompt."""
    if not similar:
        return ""
    lines = [
        "\n\n--- PRIOR CONFIRMED INCIDENTS OF THIS CLASS (your own validated history; "
        "use as precedent, do not copy blindly) ---"
    ]
    for r in similar:
        lines.append(
            f"• metric={r.get('metric')} → root cause: {r.get('root_cause')} "
            f"| fix that was confirmed: {r.get('recommended_fix')}"
        )
    return "\n".join(lines)
