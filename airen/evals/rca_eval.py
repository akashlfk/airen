"""Evaluate an InvestigatorVerdict and write the grades into Phoenix.

`evaluate_investigation` runs the deterministic code evaluators (always) plus the
LLM-as-judge evaluators (when a judge backend is available and we're not in mock
mode). `log_to_phoenix` attaches each grade to the agent's span as an annotation,
so in the Phoenix UI the evals sit right next to the trace they grade — the exact
"evals on agent traces" artifact the Arize track is looking for.
"""

from __future__ import annotations

import os
from typing import Any

from airen.evals.evaluators import run_code_evaluators
from airen.evals.types import EvalScore, RcaEvalResult


def _pass_threshold() -> float:
    try:
        return float(os.environ.get("AIREN_EVAL_PASS_THRESHOLD", "0.6"))
    except ValueError:
        return 0.6


def evaluate_investigation(sentinel: Any, inv: Any, *, use_llm: bool = True) -> RcaEvalResult:
    """Grade one Investigator verdict. Code evaluators always run; the LLM judge
    runs only when use_llm and we're not in mock mode (no real LLM there)."""
    scores: list[EvalScore] = run_code_evaluators(sentinel, inv)

    run_llm = use_llm
    try:
        from airen.mocks import is_mock_mode

        if is_mock_mode():
            run_llm = False
    except Exception:
        pass
    if os.environ.get("AIREN_EVAL_LLM_JUDGE", "1").strip().lower() in ("0", "false", "no"):
        run_llm = False

    if run_llm:
        from airen.evals.judge import run_llm_evaluators

        scores.extend(run_llm_evaluators(sentinel, inv))

    return RcaEvalResult(scores=scores, pass_threshold=_pass_threshold())


def log_to_phoenix(result: RcaEvalResult, *, span_id: str | None,
                   project_name: str | None = None) -> bool:
    """Attach each EvalScore to the Investigator span as a Phoenix annotation.
    Best-effort: returns False (never raises) if Phoenix is unreachable or no
    span_id is known."""
    if not span_id or not result.scores:
        return False
    try:
        from phoenix.client import Client

        base_url = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")
        client = Client(base_url=base_url)
        for s in result.scores:
            client.spans.add_span_annotation(
                span_id=span_id,
                annotation_name=f"rca.{s.name}",
                annotator_kind=s.annotator_kind,   # "LLM" or "CODE"
                label=s.label,
                score=s.score,
                explanation=s.explanation,
                metadata={"kind": s.kind, "overall": result.overall},
            )
        # one roll-up annotation for the whole verdict
        client.spans.add_span_annotation(
            span_id=span_id,
            annotation_name="rca.overall",
            annotator_kind="CODE",
            label="pass" if result.passed else "fail",
            score=result.overall,
            explanation=result.critique(),
        )
        return True
    except Exception:
        return False
