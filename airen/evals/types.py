"""Eval result types — backend-agnostic, no Phoenix/LLM imports here so they're
cheap to construct and trivial to unit-test."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EvalScore:
    """One graded dimension of an agent's output.

    score is normalized to 0..1 (1 = good). `kind` distinguishes a deterministic
    code/heuristic check from an LLM-as-judge grade — it maps to Phoenix's
    annotator_kind (CODE vs LLM) when we log the annotation.
    """

    name: str
    score: float
    label: str
    explanation: str
    kind: str = "code"  # "code" | "llm"

    def __post_init__(self) -> None:
        # clamp — a misbehaving judge shouldn't poison aggregates
        self.score = max(0.0, min(1.0, float(self.score)))

    @property
    def annotator_kind(self) -> str:
        return "LLM" if self.kind == "llm" else "CODE"


@dataclass
class RcaEvalResult:
    """The full grade for one InvestigatorVerdict."""

    scores: list[EvalScore] = field(default_factory=list)
    pass_threshold: float = 0.6

    @property
    def overall(self) -> float:
        if not self.scores:
            return 0.0
        return round(sum(s.score for s in self.scores) / len(self.scores), 3)

    @property
    def passed(self) -> bool:
        return self.overall >= self.pass_threshold

    def weakest(self) -> EvalScore | None:
        return min(self.scores, key=lambda s: s.score) if self.scores else None

    def failing(self) -> list[EvalScore]:
        """Scores below the bar — what the reflection retry should address."""
        return [s for s in self.scores if s.score < self.pass_threshold]

    def critique(self) -> str:
        """Human/LLM-readable summary of what fell short, for the reflection prompt."""
        bad = self.failing()
        if not bad:
            return "All evaluation dimensions passed."
        return "; ".join(f"{s.name} ({s.label}, {s.score:.2f}): {s.explanation}" for s in bad)

    def to_dict(self) -> dict:
        return {
            "overall": self.overall,
            "passed": self.passed,
            "pass_threshold": self.pass_threshold,
            "scores": [
                {"name": s.name, "score": s.score, "label": s.label,
                 "explanation": s.explanation, "kind": s.kind}
                for s in self.scores
            ],
        }
