"""Run Airen's RCA evals as a batch experiment and print a scorecard.

This is the standalone "run evaluations" artifact for the Arize track: it grades
a set of Investigator verdicts with the code + LLM-as-judge evaluators and reports
how the RCA quality looks. It runs in two modes:

    # Built-in labeled dataset (good vs deliberately-weak verdicts) — proves the
    # evals discriminate quality. Works offline (code evals; LLM judge if creds).
    python -m airen.run_evals

    # Grade the Investigator verdicts from past real incident runs in logs/
    python -m airen.run_evals --runs
    python -m airen.run_evals --runs --no-llm     # code evals only

The LLM judge follows AIREN_LLM_BACKEND (azure now, gemini for submission).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from airen.evals.rca_eval import evaluate_investigation
from airen.evals.types import RcaEvalResult
from airen.schemas import Anomaly, HealthStatus, InvestigatorVerdict, SentinelVerdict

_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"


def _sentinel(metric: str, segment: str) -> SentinelVerdict:
    return SentinelVerdict(
        project_name="demo", n_spans_analyzed=500, status=HealthStatus.CRITICAL,
        severity_score=0.85, recommended_action="INVESTIGATE", summary=f"{metric} on {segment}",
        anomalies=[Anomaly(metric=metric, segment=segment, current_value=0.4,
                           baseline_value=0.05, ratio=8.0, description=f"{segment} {metric} spiked")],
    )


def _labeled_dataset() -> list[tuple[str, SentinelVerdict, InvestigatorVerdict]]:
    """A few (label, sentinel, verdict) cases spanning good and weak RCAs."""
    s_drift = _sentinel("input_drift_auto", "input.region")
    s_infra = _sentinel("volume_drop", "—")
    return [
        ("good_drift", s_drift, InvestigatorVerdict(
            triggering_anomaly=s_drift.anomalies[0],
            root_cause_hypothesis="Data/concept drift: region distribution shifted (PSI 8x); no code cause found.",
            confidence=0.72, code_evidence=[],
            data_evidence=["input.region PSI 0.40 vs 0.05 baseline over 24h"],
            recommended_fix="RETRAIN on the last 30 days; region mix drifted.",
            further_investigation_needed=[])),
        ("good_infra", s_infra, InvestigatorVerdict(
            triggering_anomaly=s_infra.anomalies[0],
            root_cause_hypothesis="Infra/serving outage: prediction volume fell 95% at 07:10; not a model issue.",
            confidence=0.8, code_evidence=[],
            data_evidence=["volume 500/h → 25/h at 07:10 UTC"],
            recommended_fix="ROLLBACK deploy 1.4.2 and page on-call.",
            further_investigation_needed=[])),
        ("weak_hallucinated", s_drift, InvestigatorVerdict(
            triggering_anomaly=s_drift.anomalies[0],
            root_cause_hypothesis="The model is broken.", confidence=0.98,
            code_evidence=[], data_evidence=[], recommended_fix="",
            further_investigation_needed=[])),
        ("weak_offtarget", s_infra, InvestigatorVerdict(
            triggering_anomaly=_sentinel("input_drift_auto", "input.region").anomalies[0],
            root_cause_hypothesis="Some feature drifted maybe.", confidence=0.5,
            code_evidence=[], data_evidence=[], recommended_fix="look into it",
            further_investigation_needed=[])),
    ]


def _print_scorecard(rows: list[tuple[str, RcaEvalResult]]) -> None:
    print("\n" + "═" * 78)
    print("  AIREN RCA EVAL SCORECARD")
    print("═" * 78)
    dims: list[str] = []
    for _, r in rows:
        for s in r.scores:
            if s.name not in dims:
                dims.append(s.name)
    for label, r in rows:
        verdict = "PASS" if r.passed else "FAIL"
        print(f"\n  {label:<20} overall={r.overall:.2f}  [{verdict}]")
        for s in r.scores:
            tag = "🧠" if s.kind == "llm" else "⚙️"
            print(f"     {tag} {s.name:<26} {s.score:.2f}  {s.label}")
    n = len(rows)
    passed = sum(1 for _, r in rows if r.passed)
    avg = round(sum(r.overall for _, r in rows) / n, 3) if n else 0.0
    print("\n" + "─" * 78)
    print(f"  {passed}/{n} passed · mean RCA quality {avg}")
    print("─" * 78 + "\n")


def _eval_from_logs(use_llm: bool) -> list[tuple[str, RcaEvalResult]]:
    rows: list[tuple[str, RcaEvalResult]] = []
    if not _LOGS_DIR.exists():
        print(f"  No logs/ dir at {_LOGS_DIR} — nothing to grade.")
        return rows
    for p in sorted(_LOGS_DIR.glob("INC-*.json")):
        try:
            data = json.loads(p.read_text())
            sv, iv = data.get("sentinel_verdict"), data.get("investigator_verdict")
            if not sv or not iv:
                continue
            sentinel = SentinelVerdict.model_validate(sv)
            inv = InvestigatorVerdict.model_validate(iv)
            rows.append((data.get("run_id", p.stem), evaluate_investigation(sentinel, inv, use_llm=use_llm)))
        except Exception as e:  # noqa: BLE001
            print(f"  (skipped {p.name}: {type(e).__name__}: {e})")
    return rows


def main() -> None:
    argv = sys.argv[1:]
    use_llm = "--no-llm" not in argv
    if "--runs" in argv:
        rows = _eval_from_logs(use_llm)
        if not rows:
            print("  No past incident runs to grade. Run the orchestrator first.")
            return
    else:
        rows = [(label, evaluate_investigation(s, v, use_llm=use_llm))
                for label, s, v in _labeled_dataset()]
    _print_scorecard(rows)


if __name__ == "__main__":
    main()
