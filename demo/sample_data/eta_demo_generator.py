"""Masked synthetic ETA-prediction data for the demo.

Mirrors the SHAPE of a real multi-model ETA serving feed (the field names + types
match what such a service emits) but every value is synthetic — generic shipper /
carrier / load IDs and generic model-family names. **No real customer data, no
FourKites values.** Safe to show on camera.

The stream has a real time spread: a HEALTHY earlier window, then a DRIFTED recent
window for one model-family cohort (long-haul), so Airen's recent-vs-earlier drift
detection fires on a realistic-but-safe incident — and per-model segments show
exactly which model variant degraded.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone

# Generic, non-sensitive value pools (no real customers/carriers).
_STATES = ["CA", "TX", "IL", "OH", "GA", "PA", "NC", "NJ", "FL", "AZ", "WA", "CO"]
_COUNTRIES = ["US", "US", "US", "CA", "MX"]
_SEASONS = ["winter", "spring", "summer", "fall"]
_PROVIDERS = ["provider_a", "provider_b", "provider_c"]
# Masked model families — generic types, not customer-coded names.
_MODEL_FAMILIES = ["lstm_cluster", "patchtst_v1", "xgb_classifier", "regression_v2"]
_SUB_TYPES = ["standard", "express", "regional"]
_HAUL_TYPES = ["short_haul", "long_haul"]


def _rid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# patchtst_v1 is the busiest variant (it's the one that drifts in the demo).
_FAMILY_WEIGHTS = [0.22, 0.40, 0.20, 0.18]  # aligns with _MODEL_FAMILIES order


def _base_record(rng: random.Random, ts: datetime, haul: str | None = None) -> dict:
    haul = haul or rng.choice(_HAUL_TYPES)
    long = haul == "long_haul"
    dist = rng.uniform(600, 1800) if long else rng.uniform(20, 600)
    return {
        "loadId": _rid("LOAD"),
        "shipper_id": f"shipper-{rng.randint(1, 40):03d}",      # masked
        "carrier_id": f"carrier-{rng.randint(1, 120):03d}",     # masked
        "org_state": rng.choice(_STATES),
        "org_country": rng.choice(_COUNTRIES),
        "dest_state": rng.choice(_STATES),
        "dest_country": rng.choice(_COUNTRIES),
        "season": rng.choice(_SEASONS),
        "location_provider": rng.choice(_PROVIDERS),
        "model_family": rng.choices(_MODEL_FAMILIES, weights=_FAMILY_WEIGHTS)[0],
        "model_sub_type": rng.choice(_SUB_TYPES),
        "model_haul_type": haul,
        "model_region": "NA",
        "app_version": "3.4.1",
        "ec_version": "1.2.0",
        "version": "v3.4",
        "distance_in_miles": round(dist, 1),
        "dist_to_dest": round(dist * rng.uniform(0.1, 0.9), 1),
        "remaining_time_delivery": round(dist / rng.uniform(0.6, 1.1), 1),  # minutes
        "confidence_score": round(rng.uniform(0.78, 0.97), 3),
        "checkcall_hour_of_day": rng.randint(0, 23),
        "regression_prediction": round(dist / rng.uniform(0.7, 1.0), 1),    # minutes-to-arrival
        "timestamp": ts.isoformat(),
    }


def generate(
    n: int = 4000,
    *,
    window_minutes: int = 180,
    drift: bool = True,
    seed: int = 7,
) -> list[dict]:
    """Return n synthetic records spread across the last `window_minutes`.

    With drift=True, the RECENT half degrades the `long_haul` cohort of
    `patchtst_v1`: distances stretch, confidence drops, predicted ETA inflates —
    a believable "this model variant started drifting" incident, fully synthetic.
    """
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=window_minutes)
    midpoint = now - timedelta(minutes=window_minutes / 2)

    out: list[dict] = []
    for i in range(n):
        # spread timestamps across the window (gives a real recent-vs-earlier axis)
        ts = start + timedelta(seconds=(window_minutes * 60) * (i / max(n - 1, 1)))
        rec = _base_record(rng, ts)
        recent = ts >= midpoint

        if drift and recent and rec["model_family"] == "patchtst_v1":
            # the drifted variant: confidence collapses across the whole family,
            # worst on long-haul (so per-model + per-haul segments both light up).
            worst = rec["model_haul_type"] == "long_haul"
            rec["confidence_score"] = round(rng.uniform(0.20, 0.40) if worst else rng.uniform(0.40, 0.58), 3)
            rec["regression_prediction"] = round(rec["regression_prediction"] * rng.uniform(2.0, 3.0 if worst else 1.8), 1)
            if worst:
                rec["distance_in_miles"] = round(rec["distance_in_miles"] * rng.uniform(1.6, 2.4), 1)
        out.append(rec)
    return out


if __name__ == "__main__":
    import json
    import sys

    recs = generate()
    drifted = [r for r in recs if r["model_family"] == "patchtst_v1"
               and r["model_haul_type"] == "long_haul" and r["confidence_score"] < 0.6]
    print(f"generated {len(recs)} masked records · {len(drifted)} in the drifted cohort", file=sys.stderr)
    for r in recs[:3]:
        print(json.dumps(r))
