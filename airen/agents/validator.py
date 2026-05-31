"""Validator agent — confirms remediation worked.

Pattern: pure deterministic Python. No LLM. Why? "Did the fix work?" is a
ratio comparison, not a narrative judgment — the LLM has no business here.
The LLM-driven explanation is RCA Writer's job (which has already run).

Two phases per blueprint Section 5 Agent 6:

  Phase 1 — LIVE (laptop today):
    Re-query Phoenix with a short lookback (default 10 min). Compute the
    same segment-level MAE the Sentinel originally flagged. If the worst
    ratio dropped below WARNING threshold → PASS. If still > CRITICAL → FAIL.
    In between → INCONCLUSIVE.

  Phase 2 — ACTUALS at T+24h (FK VM required):
    Query Redshift's tl_eta_datamart for delivered actuals 24h after the
    incident → compute true MAE. Confirms the long-tail of corrected
    predictions actually delivered well. Currently stubbed as DEFERRED.
"""

from __future__ import annotations

import asyncio
from typing import Any

from airen.config import AirenServiceConfig
from airen.mocks import is_mock_mode
from airen.schemas import (
    HealthStatus,
    SegmentComparison,
    SentinelVerdict,
    ValidatorStatus,
    ValidatorVerdict,
)
from airen.tools.health import get_model_health_snapshot

# Match Sentinel's thresholds — same constants, single source of truth
WARNING_RATIO = 1.2
CRITICAL_RATIO = 2.0


class Validator:
    """Re-checks health after remediation and reports back."""

    def __init__(self, config: AirenServiceConfig | None = None) -> None:
        self.config = config

    # ─── Phase 1 — live recheck against Phoenix ────────────────────
    async def validate_phase_1(
        self,
        sentinel: SentinelVerdict,
        project_name: str,
        lookback_minutes: int = 10,
    ) -> ValidatorVerdict:
        """Re-query Phoenix and compare per-segment ratios with Sentinel's original anomalies."""
        if is_mock_mode():
            return _mock_validator_pass(sentinel, lookback_minutes)

        baseline_mae = (
            self.config.sentinel.baseline_mae_minutes
            if self.config and self.config.sentinel
            else 150.0
        )
        # Run the snapshot in a thread — it does sync Phoenix client calls
        snapshot: dict[str, Any] = await asyncio.to_thread(
            get_model_health_snapshot,
            project_name=project_name,
            lookback_minutes=lookback_minutes,
            baseline_mae_minutes=baseline_mae,
        )

        return _verdict_from_snapshot(
            phase=1,
            snapshot=snapshot,
            sentinel=sentinel,
            lookback_minutes=lookback_minutes,
        )

    # ─── Phase 2 — Redshift actuals (FK VM required) ───────────────
    def validate_phase_2_stub(self, sentinel: SentinelVerdict) -> ValidatorVerdict:
        """Stub that documents the Phase 2 contract without needing Redshift."""
        return ValidatorVerdict(
            phase=2,
            status=ValidatorStatus.DEFERRED,
            summary=(
                "Phase 2 (T+24h actuals check via Redshift tl_eta_datamart) requires the FK VM. "
                "Run this on the deployment target after 24h of real traffic."
            ),
            lookback_minutes=0,
            n_spans_observed=0,
            overall_mae_before=None,
            overall_mae_after=None,
            comparisons=[],
            recommended_next_step="schedule phase 2",
        )


# ───────────────────────────────────────────────────────────────────────
#  Verdict construction (pure functions — easy to test)
# ───────────────────────────────────────────────────────────────────────
def _verdict_from_snapshot(
    phase: int,
    snapshot: dict[str, Any],
    sentinel: SentinelVerdict,
    lookback_minutes: int,
) -> ValidatorVerdict:
    n_spans = int(snapshot.get("n_spans", 0))
    overall_after = snapshot.get("overall_mae_minutes")
    overall_before = (
        sentinel.anomalies[0].current_value
        if sentinel.anomalies and sentinel.anomalies[0].segment == "overall"
        else None
    )

    if n_spans == 0:
        return ValidatorVerdict(
            phase=phase,
            status=ValidatorStatus.NO_DATA,
            summary=(
                f"No predictions in the last {lookback_minutes} min — cannot confirm "
                "remediation took effect. Wait for fresh traffic and retry."
            ),
            lookback_minutes=lookback_minutes,
            n_spans_observed=0,
            overall_mae_before=overall_before,
            overall_mae_after=None,
            comparisons=[],
            recommended_next_step="monitor",
        )

    # Build per-segment comparisons against the original anomalies
    comparisons: list[SegmentComparison] = []
    for anomaly in sentinel.anomalies:
        after_ratio = _lookup_segment_ratio(snapshot, anomaly.segment)
        improvement = None
        resolved = False
        if after_ratio is not None:
            improvement = round(anomaly.ratio - after_ratio, 2)
            resolved = after_ratio < WARNING_RATIO
        comparisons.append(
            SegmentComparison(
                segment=anomaly.segment,
                before_ratio=anomaly.ratio,
                after_ratio=after_ratio,
                improvement=improvement,
                resolved=resolved,
            )
        )

    # Status decision — look at the WORST remaining segment
    worst_after = max(
        (c.after_ratio for c in comparisons if c.after_ratio is not None),
        default=None,
    )
    if worst_after is None:
        status = ValidatorStatus.INCONCLUSIVE
        summary = "Sentinel's flagged segments are no longer present in the recheck window — likely resolved, but no direct comparison was possible."
        next_step = "monitor"
    elif worst_after < WARNING_RATIO:
        status = ValidatorStatus.PASS
        summary = f"All previously-flagged segments are back to healthy (worst ratio now {worst_after:.2f}x)."
        next_step = "monitor"
    elif worst_after < CRITICAL_RATIO:
        status = ValidatorStatus.INCONCLUSIVE
        summary = (
            f"Partial recovery — worst ratio dropped to {worst_after:.2f}x but is still "
            "above the warning threshold (1.2x). Give it another window or re-investigate."
        )
        next_step = "monitor"
    else:
        status = ValidatorStatus.FAIL
        summary = (
            f"Remediation did NOT resolve the symptom — worst ratio is still "
            f"{worst_after:.2f}x. Re-investigate or escalate."
        )
        next_step = "re-investigate"

    return ValidatorVerdict(
        phase=phase,
        status=status,
        summary=summary,
        lookback_minutes=lookback_minutes,
        n_spans_observed=n_spans,
        overall_mae_before=overall_before,
        overall_mae_after=overall_after,
        comparisons=comparisons,
        recommended_next_step=next_step,
    )


def _lookup_segment_ratio(snapshot: dict, segment: str) -> float | None:
    """Find the post-fix ratio for a segment label like 'api_fetch_limit=73' or 'shipper=Fritolay'."""
    if "=" not in segment:
        return None
    key, _, val = segment.partition("=")
    breakdown_keys = {
        "shipper": "by_shipper",
        "api_fetch_limit": "by_api_fetch_limit",
    }
    bucket = snapshot.get(breakdown_keys.get(key, ""), {})
    seg = bucket.get(val)
    if seg is None:
        return None
    return float(seg.get("ratio_vs_baseline", 0.0))


# ───────────────────────────────────────────────────────────────────────
#  Mock — deterministic PASS so the offline pipeline shows the happy path
# ───────────────────────────────────────────────────────────────────────
def _mock_validator_pass(sentinel: SentinelVerdict, lookback_minutes: int) -> ValidatorVerdict:
    comparisons: list[SegmentComparison] = []
    for a in sentinel.anomalies:
        comparisons.append(
            SegmentComparison(
                segment=a.segment,
                before_ratio=a.ratio,
                after_ratio=1.05,  # back near baseline
                improvement=round(a.ratio - 1.05, 2),
                resolved=True,
            )
        )
    return ValidatorVerdict(
        phase=1,
        status=ValidatorStatus.PASS,
        summary=(
            "All previously-flagged segments are healthy in the post-remediation window "
            "(mock mode — simulated recovery)."
        ),
        lookback_minutes=lookback_minutes,
        n_spans_observed=120,
        overall_mae_before=(sentinel.anomalies[0].current_value if sentinel.anomalies else None),
        overall_mae_after=125.0,
        comparisons=comparisons,
        recommended_next_step="monitor",
    )
