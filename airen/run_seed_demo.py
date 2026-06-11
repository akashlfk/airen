"""Seed a Phoenix project with MASKED synthetic ETA predictions for the demo.

Writes the generated records (demo.sample_data.eta_demo_generator) as Phoenix
spans — identical shape to the real Kafka tap (`mlre.input.*`, `mlre.model.version`)
but with synthetic, non-sensitive values and an explicit per-record start_time so
the spans have a real time spread (healthy earlier window + drifted recent cohort).
No real Kafka, no real repo, nothing from FourKites.

Usage:
    python -m airen.run_seed_demo --service eta-demo
    python -m airen.run_seed_demo --service eta-demo --records 5000 --window 180
    python -m airen.run_seed_demo --service eta-demo --no-drift   # all-healthy baseline
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from phoenix.otel import register  # noqa: E402

from demo.sample_data.eta_demo_generator import generate  # noqa: E402


def _ns(iso_ts: str) -> int:
    """ISO timestamp → nanoseconds since epoch (OTel span start/end time)."""
    return int(datetime.fromisoformat(iso_ts).timestamp() * 1_000_000_000)


def _resolve_project(service: str | None) -> str:
    if service:
        try:
            from airen.config import load_service_config

            return load_service_config(service).phoenix.project_name
        except Exception:
            pass
    return "eta-demo-prediction"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--service", default="eta-demo", help="onboarded demo service (for its Phoenix project)")
    ap.add_argument("--records", type=int, default=4000)
    ap.add_argument("--window", type=int, default=180, help="minutes of history to spread spans across")
    ap.add_argument("--no-drift", action="store_true", help="seed an all-healthy baseline (no incident)")
    args = ap.parse_args()

    project = _resolve_project(args.service)
    recs = generate(n=args.records, window_minutes=args.window, drift=not args.no_drift)

    tp = register(project_name=project, auto_instrument=False, batch=False)
    tracer = tp.get_tracer("airen.demo.seed")

    drifted = 0
    for r in recs:
        start = _ns(r["timestamp"])
        span = tracer.start_span("predict", start_time=start)
        try:
            for k, v in r.items():
                if k == "timestamp":
                    continue
                span.set_attribute(f"mlre.input.{k}", v)
            span.set_attribute("mlre.model.version", str(r.get("version", "")))
            span.set_attribute("mlre.timestamp", r["timestamp"])
            if r["model_family"] == "patchtst_v1" and r["model_haul_type"] == "long_haul" and r["confidence_score"] < 0.6:
                drifted += 1
        finally:
            span.end(end_time=start)
    tp.force_flush()

    print(f"✓ Seeded {len(recs)} masked spans into Phoenix project '{project}' "
          f"({'no drift' if args.no_drift else f'{drifted} in the drifted long-haul/patchtst_v1 cohort'}).")
    print(f"  Spread across the last {args.window} min. All values synthetic — safe to show.")


if __name__ == "__main__":
    main()
