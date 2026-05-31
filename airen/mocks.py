"""Canned agent outputs for testing the pipeline without LLM calls.

Activate with AIREN_LLM_MODE=mock in .env. The mock outputs match the shape
real agents produce — same Pydantic schemas, same fields — so downstream
code can't tell the difference.

These were captured from real successful agent runs against
cloudqwest/dynamic_eta_prediction. Edit them when the schemas evolve.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from airen.schemas import (
    Anomaly,
    CodeEvidence,
    HealthStatus,
    IncidentReport,
    InvestigatorVerdict,
    RecommendedAction,
    SentinelVerdict,
)


def is_mock_mode() -> bool:
    """True when AIREN_LLM_MODE=mock (defaults to real LLM otherwise)."""
    return os.environ.get("AIREN_LLM_MODE", "real").strip().lower() == "mock"


# ───────────────────────────────────────────────────────────────────────
#  Mock Sentinel verdict — modelled after the real run
# ───────────────────────────────────────────────────────────────────────
def mock_sentinel_verdict(
    project_name: str = "tl-eta-prediction",
    n_spans: int = 200,
) -> SentinelVerdict:
    return SentinelVerdict(
        project_name=project_name,
        n_spans_analyzed=n_spans,
        status=HealthStatus.CRITICAL,
        severity_score=0.92,
        anomalies=[
            Anomaly(
                metric="psi",
                segment="api_fetch_limit",
                current_value=1.43,
                baseline_value=0.10,
                ratio=14.3,
                description=(
                    "Feature distribution drift — training expected api_fetch_limit=184, "
                    "production now contains values {73, 184}. PSI 1.43 is 5.7× the critical threshold."
                ),
            ),
            Anomaly(
                metric="mae",
                segment="api_fetch_limit=73",
                current_value=631.5,
                baseline_value=150.0,
                ratio=4.21,
                description="Long-haul predictions with truncated ping history are significantly off.",
            ),
            Anomaly(
                metric="mae",
                segment="shipper=Fritolay",
                current_value=237.3,
                baseline_value=150.0,
                ratio=1.58,
                description="Fritolay's overall MAE is elevated due to api_fetch_limit=73 affecting their long-hauls.",
            ),
            Anomaly(
                metric="mae",
                segment="shipper=Shipper A",
                current_value=230.4,
                baseline_value=150.0,
                ratio=1.54,
                description="Shipper A's MAE is moderately elevated.",
            ),
        ],
        recommended_action=RecommendedAction.ALERT_HUMAN,
        summary=(
            "Model is CRITICAL — TWO signals: (1) PSI 1.43 on api_fetch_limit indicates major "
            "feature distribution drift, and (2) predictions with api_fetch_limit=73 are 4.21× "
            "the baseline MAE. 37 of 200 predictions affected, concentrated on long-haul loads."
        ),
    )


# ───────────────────────────────────────────────────────────────────────
#  Mock Investigator verdict — modelled after the real run
# ───────────────────────────────────────────────────────────────────────
def mock_investigator_verdict(
    sentinel: SentinelVerdict | None = None,
) -> InvestigatorVerdict:
    triggering = (sentinel.anomalies[0] if sentinel and sentinel.anomalies
                  else mock_sentinel_verdict().anomalies[0])
    return InvestigatorVerdict(
        triggering_anomaly=triggering,
        # Four-source agreement: Phoenix + MLflow + GitHub + Graphify (call-site).
        # Confidence pegs at 0.98 because Graphify identifies the exact code line.
        root_cause_hypothesis=(
            "CONFIRMED root cause with call-site precision. FOUR independent sources agree:\n"
            "1) Phoenix shows 37 of 200 long-haul predictions with `api_fetch_limit=73` "
            "since 2026-05-27 19:42 UTC (MAE 631.5 min vs 150 baseline — 4.21×).\n"
            "2) MLflow training run `mock_run_d9de2cc6` (tl-eta-prod, latest) has NO "
            "`api_fetch_limit` parameter — the model was NEVER trained against this constraint.\n"
            "3) GitHub: commit 6270ca55 by akashlfk on 2026-05-27 (PR #741, ETAI-338).\n"
            "4) Graphify pinpointed the exact assignments: `main.py:95` and "
            "`predict/processing/patchtst/PatchTSTProcessor.py:296` both compute "
            "`api_fetch_limit = seq_len + api_limit_buffer`. The drift driver is "
            "`api_limit_buffer` — it was reduced in PR #741, shrinking the fetched "
            "ping window for long-haul loads."
        ),
        confidence=0.98,
        code_evidence=[
            CodeEvidence(
                commit_sha="6270ca55",
                commit_author="akashlfk",
                commit_date="2026-05-27T10:07:23+00:00",
                commit_message="ETAI-338 — Test infrastructure v2: activate regression suite, push coverage to 93.5% (#736) (#741)",
                file_path="main.py",
                matched_pattern="api_fetch_limit = seq_len + api_limit_buffer",
                pr_number=741,
                pr_url="https://github.com/cloudqwest/dynamic_eta_prediction/pull/741",
                diff_snippet=(
                    "-api_limit_buffer = 136   # production buffer matching training (seq_len 48 + 136 = 184)\n"
                    "+api_limit_buffer = 25    # tightened buffer; api_fetch_limit now 73 for long-haul\n"
                    " api_fetch_limit = seq_len + api_limit_buffer\n"
                ),
            ),
        ],
        data_evidence=[
            "MLflow tl-eta-prod latest run `mock_run_d9de2cc6` — training params: "
            "seq_len=48, max_pings=250, batch_size=64. NO `api_fetch_limit` parameter; "
            "training implicitly used the full ping history.",
            "Phoenix shows 37 of 200 predictions with api_fetch_limit=73 → MAE 631.5 min "
            "(4.21x baseline). Training-time MAE was 118.4 min — production is 5.3x worse.",
            "Earliest occurrence 2026-05-27T19:42 UTC — aligns with PR #741 deploy time.",
            "Graphify call-site evidence: `api_fetch_limit = seq_len + api_limit_buffer` "
            "appears at `main.py:95` and `predict/processing/patchtst/PatchTSTProcessor.py:296`. "
            "Both sites take their value from the same `api_limit_buffer` constant.",
            "Shipper Fritolay disproportionately affected: 18 of 37 broken predictions.",
        ],
        recommended_fix=(
            "1. Restore `api_limit_buffer` to its pre-PR-#741 value (136) at both "
            "`main.py:95` and `PatchTSTProcessor.py:296` — this reverts api_fetch_limit "
            "to 184 (= seq_len 48 + buffer 136), matching training-time behavior. "
            "2. Alternatively retrain with the new buffer value as an explicit MLflow "
            "param so future drift is detectable up-front. Restoring is safer for now."
        ),
        further_investigation_needed=[
            "Confirm with PR #741 author whether reducing api_limit_buffer to 25 was "
            "intentional optimization or an accidental config change.",
        ],
    )


# ───────────────────────────────────────────────────────────────────────
#  Mock Incident report — what RCA Writer would have produced
# ───────────────────────────────────────────────────────────────────────
def mock_incident_report(
    sentinel: SentinelVerdict | None = None,
    investigator: InvestigatorVerdict | None = None,
) -> IncidentReport:
    return IncidentReport(
        title="TL ETA: training/inference mismatch — api_fetch_limit=73 (not in training)",
        severity=HealthStatus.CRITICAL,
        tldr=(
            "Airen has CONFIRMED a training/inference mismatch on the TL ETA model. "
            "Production is using a parameter (`api_fetch_limit=73`) that the model was "
            "never trained against — MLflow's latest training run has no such parameter. "
            "Long-haul predictions are 4.2× worse than baseline as a result. "
            "Recommend reverting commit 6270ca55 to restore the training-time invariant."
        ),
        what_happened=(
            "Since 2026-05-27 19:42 UTC, the TL ETA model has been producing significantly "
            "worse predictions for a specific slice of long-haul traffic:\n\n"
            "- **Affected segment**: predictions where `api_fetch_limit=73`\n"
            "- **Volume**: 37 of 200 recent predictions (≈18%)\n"
            "- **Error magnitude**: 631.5 min average, vs the 150 min healthy baseline (4.21× worse)\n"
            "- **Customer impact**: Fritolay is most affected (1.58× shipper-level MAE), with "
            "Shipper A also elevated (1.54×)\n"
            "- **Training-time MAE** for the deployed model was 118.4 min — production is **5.3× worse**.\n"
        ),
        root_cause=(
            "**Confirmed: training/inference mismatch — call site pinpointed.** "
            "FOUR independent sources agree:\n\n"
            "1. **MLflow** (`mock_run_d9de2cc6`, tl-eta-prod latest) — the deployed model was "
            "trained without an explicit `api_fetch_limit` parameter; training used the full "
            "ping history with `max_pings=250` and `seq_len=48`.\n"
            "2. **Phoenix** — production spans show `api_fetch_limit=73` on 37 of 200 recent "
            "predictions. This value was never seen at training time.\n"
            "3. **GitHub** — commit "
            "[`6270ca55`](https://github.com/cloudqwest/dynamic_eta_prediction/pull/741) by "
            "akashlfk on 2026-05-27 (PR #741, ETAI-338).\n"
            "4. **Graphify** (code knowledge graph) — pinpointed the exact assignments:\n"
            "    - `main.py:95` — `api_fetch_limit = seq_len + api_limit_buffer`\n"
            "    - `predict/processing/patchtst/PatchTSTProcessor.py:296` — `api_fetch_limit = seq_len + api_limit_buffer`\n"
            "    Both sites read the same `api_limit_buffer` constant. PR #741 reduced this "
            "buffer from `136` → `25`, which made api_fetch_limit drop from 184 (training-aligned) "
            "to 73 — the value Sentinel surfaced.\n\n"
            "Investigator confidence: **0.98** (four-source cross-reference)."
        ),
        evidence_bullets=[
            "**MLflow**: training run `mock_run_d9de2cc6` — params: seq_len=48, max_pings=250, "
            "batch_size=64. No explicit api_fetch_limit; training-time MAE 118.4 min.",
            "**Phoenix**: 37 of 200 predictions with `api_fetch_limit=73` → MAE 631.5 min "
            "(4.21× baseline) since 2026-05-27 19:42 UTC",
            "**GitHub**: commit 6270ca55 in PR #741 — author akashlfk, merged 2026-05-27",
            "**Graphify**: `main.py:95` and `PatchTSTProcessor.py:296` both compute "
            "`api_fetch_limit = seq_len + api_limit_buffer`. Drift driver: `api_limit_buffer` "
            "reduced 136 → 25 in PR #741.",
            "**Customer impact**: Fritolay overrepresented in failures (18/37 = 49%, vs ~50% "
            "expected by their traffic share — concentrated on long-haul loads)",
        ],
        recommended_fix=(
            "1. **Revert PR #741** — restores `api_limit_buffer` from 25 back to 136 at both "
            "`main.py:95` and `predict/processing/patchtst/PatchTSTProcessor.py:296`, bringing "
            "`api_fetch_limit` back to 184 (training-aligned).\n"
            "2. **Verify**: after deploy, watch the Phoenix `api_fetch_limit` PSI for 1 hour. "
            "Should drop back under the 0.10 warning threshold. Airen's Validator runs this "
            "check automatically (Phase 1).\n"
            "3. **Phase 2 validation**: at T+24h, the Validator compares actual delivered "
            "ETAs from `tl_eta_datamart` to confirm the fix held under real traffic.\n"
            "4. **Long-term**: log `api_limit_buffer` as an MLflow tag on every training run "
            "so this class of buffer-tuning drift is detectable before deploy, not after."
        ),
        open_questions=[
            "Confirm with the PR #741 author whether reducing `api_limit_buffer` from 136 → 25 "
            "was intentional optimization or an accidental config change.",
            "Should we log `api_limit_buffer` as an MLflow tag on future training runs so this "
            "class of buffer-tuning drift is detected before deploy, not after?",
        ],
    )
