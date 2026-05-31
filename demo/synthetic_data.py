"""
Synthetic TL ETA prediction emitter.

Mirrors the Fritolay LSTM inference path well enough to test Airen end-to-end:
- 79% of spans are "healthy" (api_fetch_limit = 184, low MAE)
- 20% are "Fritolay-bugged" (api_fetch_limit = 73, high MAE)
- 1% are flat-out failures (no prediction, error attribute set)

Run while `phoenix serve` is up on localhost:6006.
Spans land in Phoenix project "tl-eta-prediction".

Usage:
    python -m demo.synthetic_data            # emit 200 spans (default)
    python -m demo.synthetic_data --n 1000   # emit 1000 spans
"""

from __future__ import annotations

import argparse
import json
import random
import time
import uuid
from datetime import datetime, timezone

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from phoenix.otel import register

PROJECT_NAME = "tl-eta-prediction"
MODEL_NAME = "tl_eta_lstm_v3"
MODEL_VERSION = "d9de2cc6"  # the actual Fritolay-incident commit SHA

# Healthy regime — matches training distribution
HEALTHY_API_FETCH_LIMIT = 184
HEALTHY_MAE_BASE = 610.0

# Bugged regime — the Feb 19 PR #621 inference-time mistake
BUGGED_API_FETCH_LIMIT = 73  # seq_len(48) + 25
BUGGED_MAE_BASE = 1277.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_features(api_fetch_limit: int) -> dict:
    """Mimic the LSTM input — ping series stats + load metadata."""
    n_pings = min(api_fetch_limit, random.randint(50, 250))
    return {
        "load_id": f"LD{random.randint(100000, 999999)}",
        "shipper": random.choices(
            ["Fritolay", "Shipper A", "Shipper B"], weights=[0.5, 0.3, 0.2]
        )[0],
        "origin_state": random.choice(["IL", "TX", "CA", "OH"]),
        "dest_state": random.choice(["GA", "FL", "NY", "WA"]),
        "miles_remaining": random.randint(50, 2000),
        "n_pings_used": n_pings,
        "api_fetch_limit": api_fetch_limit,
        "seq_len": 48,
    }


def _predict(features: dict, bugged: bool) -> tuple[float, float]:
    """Return (predicted_eta_min, actual_eta_min). Predicted drifts when bugged."""
    miles = features["miles_remaining"]
    actual = miles * 1.2 + random.gauss(0, 60)  # ~50mph + noise
    if bugged:
        predicted = actual + random.gauss(0, BUGGED_MAE_BASE)
    else:
        predicted = actual + random.gauss(0, HEALTHY_MAE_BASE / 4)
    return max(0.0, predicted), max(0.0, actual)


def _emit_one(tracer: trace.Tracer, bugged: bool, fail: bool = False) -> None:
    api_fetch_limit = BUGGED_API_FETCH_LIMIT if bugged else HEALTHY_API_FETCH_LIMIT
    features = _make_features(api_fetch_limit)
    request_id = str(uuid.uuid4())

    with tracer.start_as_current_span("predict_eta") as span:
        # OpenInference standard attributes — Phoenix UI keys off these
        span.set_attribute(
            SpanAttributes.OPENINFERENCE_SPAN_KIND,
            OpenInferenceSpanKindValues.LLM.value,
        )
        span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps(features))
        span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")
        span.set_attribute(SpanAttributes.LLM_MODEL_NAME, MODEL_NAME)

        # Custom mlre.* attrs — the Investigator will query these via Phoenix MCP
        span.set_attribute("mlre.request_id", request_id)
        span.set_attribute("mlre.model.version", MODEL_VERSION)
        span.set_attribute("mlre.input.seq_len", features["seq_len"])
        span.set_attribute("mlre.input.api_fetch_limit", api_fetch_limit)
        span.set_attribute("mlre.input.n_pings_used", features["n_pings_used"])
        span.set_attribute("mlre.input.shipper", features["shipper"])
        span.set_attribute("mlre.input.miles_remaining", features["miles_remaining"])
        span.set_attribute("mlre.timestamp", _now_iso())

        time.sleep(random.uniform(0.02, 0.12))  # fake inference latency

        if fail:
            span.set_attribute("mlre.error.type", "FeatureFetchTimeout")
            span.set_status(Status(StatusCode.ERROR, "redshift ping fetch timed out"))
            return

        predicted, actual = _predict(features, bugged)
        ae = abs(predicted - actual)

        span.set_attribute(
            SpanAttributes.OUTPUT_VALUE, json.dumps({"eta_minutes": predicted})
        )
        span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")
        span.set_attribute("mlre.prediction.eta_minutes", predicted)
        span.set_attribute("mlre.ground_truth.actual_minutes", actual)
        span.set_attribute("mlre.eval.absolute_error_minutes", ae)
        span.set_attribute("mlre.eval.regime", "bugged" if bugged else "healthy")


def main(n_spans: int) -> None:
    tracer_provider = register(project_name=PROJECT_NAME, auto_instrument=False)
    tracer = tracer_provider.get_tracer("mlre.demo.synthetic")

    print(f"Emitting {n_spans} spans to Phoenix project '{PROJECT_NAME}'...")
    counts = {"healthy": 0, "bugged": 0, "failed": 0}
    for i in range(n_spans):
        roll = random.random()
        if roll < 0.01:
            _emit_one(tracer, bugged=False, fail=True)
            counts["failed"] += 1
        elif roll < 0.21:
            _emit_one(tracer, bugged=True)
            counts["bugged"] += 1
        else:
            _emit_one(tracer, bugged=False)
            counts["healthy"] += 1
        if (i + 1) % 25 == 0:
            print(f"  {i + 1}/{n_spans} emitted")

    tracer_provider.force_flush()
    print(f"\nDone. Counts: {counts}")
    print(f"Open http://localhost:6006 and select project '{PROJECT_NAME}'.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=200, help="number of spans to emit")
    args = parser.parse_args()
    main(args.n)
