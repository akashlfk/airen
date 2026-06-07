"""LLM-as-judge evaluators for the Investigator's verdict, built on
`phoenix.evals` (the Arize evals library the track expects us to use).

The judge LLM follows Airen's own backend switch (AIREN_LLM_BACKEND): it runs on
Azure today and on Gemini for the submission, with no code change — same as the
agents. If the provider/creds aren't available, the LLM evaluators simply yield
nothing and the deterministic code evaluators carry the eval pass.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from airen.evals.types import EvalScore


def _backend_to_provider_model() -> tuple[str, str]:
    """Map Airen's AIREN_LLM_BACKEND onto a phoenix.evals provider+model.

    azure  → litellm client with an `azure/<deployment>` model (reads the same
             AZURE_API_* env vars llm_factory uses).
    gemini → google-genai provider.
    """
    backend = os.environ.get("AIREN_LLM_BACKEND", "gemini").strip().lower()
    if backend == "azure":
        deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
        return "litellm", f"azure/{deployment}"
    model = os.environ.get("JUDGE_MODEL") or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    return "google", model


@lru_cache(maxsize=1)
def get_judge_llm() -> Any:
    """The phoenix.evals.LLM used to grade RCAs. Cached — one per process."""
    from phoenix.evals import LLM

    provider, model = _backend_to_provider_model()
    return LLM(provider=provider, model=model)


_GROUNDEDNESS_TEMPLATE = """\
You are auditing an ML-incident root-cause analysis for hallucination.

DETECTED ANOMALY:
{anomaly}

ANALYST'S ROOT-CAUSE HYPOTHESIS:
{hypothesis}

EVIDENCE THE ANALYST CITED:
{evidence}

Is the hypothesis GROUNDED in the cited evidence — i.e. does the evidence actually
support the conclusion, with nothing fabricated or assumed beyond it? If the
hypothesis is a confident claim with no supporting evidence, answer "ungrounded".
Answer with exactly one word: grounded or ungrounded.
"""

_CORRECTNESS_TEMPLATE = """\
You are a senior ML reliability engineer reviewing an incident diagnosis.

DETECTED ANOMALY:
{anomaly}

EVIDENCE:
{evidence}

PROPOSED ROOT CAUSE:
{hypothesis}

PROPOSED FIX:
{recommendation}

Given the anomaly and evidence, is the diagnosis + fix a CORRECT and sensible
response? "correct" = right cause class and a fitting fix; "plausible" = defensible
but not clearly right; "incorrect" = wrong cause class or a fix that doesn't follow.
Answer with exactly one word: correct, plausible, or incorrect.
"""


@lru_cache(maxsize=1)
def _classifiers() -> list:
    """Build the judge classifiers once. Lazy so importing this module never needs
    LLM creds."""
    from phoenix.evals import create_classifier

    llm = get_judge_llm()
    grounded = create_classifier(
        name="rca_groundedness",
        prompt_template=_GROUNDEDNESS_TEMPLATE,
        llm=llm,
        choices={"grounded": 1.0, "ungrounded": 0.0},
    )
    correct = create_classifier(
        name="rca_correctness",
        prompt_template=_CORRECTNESS_TEMPLATE,
        llm=llm,
        choices={"correct": 1.0, "plausible": 0.5, "incorrect": 0.0},
    )
    return [grounded, correct]


def _format_anomaly(sentinel: Any) -> str:
    anomalies = list(getattr(sentinel, "anomalies", []) or [])
    if not anomalies:
        return "(no specific anomaly; general health check)"
    a = anomalies[0]
    return (f"metric={getattr(a, 'metric', '?')} segment={getattr(a, 'segment', '?')} "
            f"ratio={getattr(a, 'ratio', '?')} — {getattr(a, 'description', '')}")


def _format_evidence(inv: Any) -> str:
    code = getattr(inv, "code_evidence", []) or []
    data = list(getattr(inv, "data_evidence", []) or [])
    lines: list[str] = []
    for c in code:
        sha = getattr(c, "commit_sha", "") or ""
        fp = getattr(c, "file_path", "") or getattr(c, "file", "") or ""
        lines.append(f"commit {sha[:8]} {fp}".strip())
    lines.extend(str(d) for d in data)
    return "\n".join(f"- {l}" for l in lines) if lines else "(none cited)"


def run_llm_evaluators(sentinel: Any, inv: Any) -> list[EvalScore]:
    """Grade the verdict with the LLM judge. Returns [] (not an error) if the judge
    is unavailable — the code evaluators still produce a result."""
    eval_input = {
        "anomaly": _format_anomaly(sentinel),
        "hypothesis": (getattr(inv, "root_cause_hypothesis", "") or "").strip() or "(none)",
        "evidence": _format_evidence(inv),
        "recommendation": (getattr(inv, "recommended_fix", "") or "").strip() or "(none)",
    }
    out: list[EvalScore] = []
    try:
        classifiers = _classifiers()
    except Exception:
        return out  # no provider/creds → skip LLM judging entirely
    for clf in classifiers:
        try:
            for sc in clf.evaluate(eval_input):
                score = sc.score if sc.score is not None else 0.0
                label = sc.label or "?"
                expl = sc.explanation or f"judge labeled this '{label}'."
                out.append(EvalScore(getattr(sc, "name", "rca_judge"),
                                     float(score), label, expl, kind="llm"))
        except Exception as e:  # noqa: BLE001 — one bad call shouldn't sink the rest
            out.append(EvalScore(getattr(clf, "name", "rca_judge"), 0.0, "judge_error",
                                 f"judge call failed: {type(e).__name__}: {e}", kind="llm"))
    return out
