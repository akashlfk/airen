"""Airen self-evaluation + self-improvement (Arize track core).

This package is how Airen *grades its own work* and *gets better from it* — the
two things the Arize track rewards beyond plain tracing:

  • evals      — score the Investigator's root-cause analysis (code-based +
                 LLM-as-judge), then attach each score to the agent's Phoenix
                 span as an annotation. The grades live next to the trace.
  • learning   — the self-improvement loop: a low eval score triggers a
                 reflection retry (the agent reads its OWN critique and tries
                 again), and Validator-confirmed incidents are remembered so
                 past root causes seed future investigations.

Everything degrades gracefully: no Phoenix / no LLM / mock mode → the code
evaluators still run, the LLM judge is skipped, and nothing breaks a cycle.
"""

from __future__ import annotations

from airen.evals.types import EvalScore, RcaEvalResult

__all__ = ["EvalScore", "RcaEvalResult"]
