"""Kafka tap — consume prediction messages, emit OTEL spans to Phoenix.

Architecture:
    Kafka topic → KafkaAdapter.consume() → KafkaTap → OTEL span → Phoenix

The tap is a thin orchestrator. The adapter handles broker-specific decoding;
the tap turns clean dicts into spans. Same span shape as demo/synthetic_data.py
so Sentinel and Investigator work without modification.

Designed to run as a long-lived process. Ctrl+C exits cleanly.
"""

from __future__ import annotations

import json
import time
from typing import Any

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from airen.adapters.kafka_base import KafkaAdapter


class KafkaTap:
    """Long-lived consumer that converts Kafka prediction messages to Phoenix spans."""

    def __init__(
        self,
        adapter: KafkaAdapter,
        tracer: trace.Tracer,
        log_every: int = 25,
    ) -> None:
        self.adapter = adapter
        self.tracer = tracer
        self.log_every = log_every
        self._counts: dict[str, int] = {"healthy": 0, "bugged": 0, "failed": 0, "unknown": 0}

    def run(self, max_messages: int | None = None) -> None:
        """Consume forever (or up to max_messages) and emit one span per message."""
        emitted = 0
        try:
            for msg in self.adapter.consume(max_messages=max_messages):
                self._emit_span(msg)
                emitted += 1
                if emitted % self.log_every == 0:
                    print(f"  emitted {emitted} spans — counts: {self._counts}")
        except KeyboardInterrupt:
            print("\nInterrupted — shutting down cleanly.")
        finally:
            self.adapter.close()
            print(f"\nDone. Total {emitted} spans. Counts: {self._counts}")

    def _emit_span(self, msg: dict[str, Any]) -> None:
        regime = msg.get("_regime", "unknown")
        is_failed = "error_type" in msg

        with self.tracer.start_as_current_span("predict_eta") as span:
            # OpenInference standard attributes — Phoenix UI keys off these
            span.set_attribute(
                SpanAttributes.OPENINFERENCE_SPAN_KIND,
                OpenInferenceSpanKindValues.LLM.value,
            )
            features = {k: v for k, v in msg.items() if not k.startswith("_") and k not in {"predicted_eta_minutes", "actual_eta_minutes", "error_type", "error_message"}}
            span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps(features))
            span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")
            span.set_attribute(SpanAttributes.LLM_MODEL_NAME, msg.get("model_name", "unknown"))

            # mlre.* custom attrs — match synthetic_data.py exactly
            span.set_attribute("mlre.request_id", str(msg.get("request_id", "")))
            span.set_attribute("mlre.model.version", str(msg.get("model_version", "")))
            span.set_attribute("mlre.timestamp", str(msg.get("timestamp", "")))

            for key in (
                "seq_len",
                "api_fetch_limit",
                "n_pings_used",
                "shipper",
                "miles_remaining",
            ):
                if key in msg:
                    span.set_attribute(f"mlre.input.{key}", msg[key])

            if is_failed:
                span.set_attribute("mlre.error.type", str(msg.get("error_type", "")))
                span.set_status(Status(StatusCode.ERROR, str(msg.get("error_message", ""))))
                self._counts["failed"] = self._counts.get("failed", 0) + 1
                return

            predicted = msg.get("predicted_eta_minutes")
            actual = msg.get("actual_eta_minutes")
            if predicted is not None:
                span.set_attribute("mlre.prediction.eta_minutes", float(predicted))
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps({"eta_minutes": predicted}))
                span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")
            if actual is not None:
                span.set_attribute("mlre.ground_truth.actual_minutes", float(actual))
            if predicted is not None and actual is not None:
                span.set_attribute("mlre.eval.absolute_error_minutes", abs(float(predicted) - float(actual)))
            span.set_attribute("mlre.eval.regime", regime)

            self._counts[regime] = self._counts.get(regime, 0) + 1
