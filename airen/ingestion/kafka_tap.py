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


def _num_or_str(v: Any):
    """Phoenix span attrs take scalars; coerce to float when numeric, else str."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v)


class KafkaTap:
    """Long-lived consumer that converts Kafka prediction messages to Phoenix spans."""

    def __init__(
        self,
        adapter: KafkaAdapter,
        tracer: trace.Tracer,
        log_every: int = 25,
        field_map=None,
    ) -> None:
        self.adapter = adapter
        self.tracer = tracer
        self.log_every = log_every
        # When a field_map is provided (config-driven), use it to map THIS
        # service's message fields → span attributes. Otherwise fall back to the
        # built-in ETA shape (back-compat with the demo / tl-eta).
        self.field_map = field_map
        self._counts: dict[str, int] = {"healthy": 0, "bugged": 0, "failed": 0, "unknown": 0}

    def run(self, max_messages: int | None = None) -> None:
        """Consume forever (or up to max_messages) and emit one span per message."""
        emitted = 0
        emit = self._emit_span_mapped if self.field_map is not None else self._emit_span
        try:
            for msg in self.adapter.consume(max_messages=max_messages):
                emit(msg)
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

    def _emit_span_mapped(self, msg: dict[str, Any]) -> None:
        """Config-driven: map THIS service's fields via field_map (no hardcoding).

        Writes generic, predictable attributes so the `observation` block lines up:
          inputs[f] → mlre.input.<f>   (→ column input.<f> = observation.segments)
          error/computed → mlre.eval.error  (→ column eval.error = observation.error_attribute)
        """
        fm = self.field_map
        mapped = {fm.prediction, fm.actual, fm.error, fm.model_version, fm.timestamp}
        with self.tracer.start_as_current_span("predict") as span:
            span.set_attribute(
                SpanAttributes.OPENINFERENCE_SPAN_KIND,
                OpenInferenceSpanKindValues.LLM.value,
            )
            # inputs → mlre.input.<field>. Explicit list, else every scalar field.
            input_fields = fm.inputs or [
                k for k, v in msg.items()
                if not k.startswith("_") and k not in mapped and isinstance(v, (str, int, float, bool))
            ]
            feat: dict[str, Any] = {}
            for f in input_fields:
                if f in msg and msg[f] is not None:
                    span.set_attribute(f"mlre.input.{f}", msg[f])
                    feat[f] = msg[f]
            span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps(feat, default=str))
            span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")

            if fm.model_version and msg.get(fm.model_version) is not None:
                span.set_attribute("mlre.model.version", str(msg[fm.model_version]))
            if fm.timestamp and msg.get(fm.timestamp) is not None:
                span.set_attribute("mlre.timestamp", str(msg[fm.timestamp]))

            pred = msg.get(fm.prediction) if fm.prediction else None
            actual = msg.get(fm.actual) if fm.actual else None
            if pred is not None:
                span.set_attribute("mlre.prediction.value", _num_or_str(pred))
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps({"prediction": pred}, default=str))
                span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")
            if actual is not None:
                span.set_attribute("mlre.ground_truth.value", _num_or_str(actual))

            # error: explicit field, else computed |prediction - actual|
            err = msg.get(fm.error) if fm.error else None
            if err is None and pred is not None and actual is not None:
                try:
                    err = abs(float(pred) - float(actual))
                except (TypeError, ValueError):
                    err = None
            if err is not None:
                try:
                    span.set_attribute("mlre.eval.error", float(err))
                except (TypeError, ValueError):
                    pass

            self._counts["unknown"] = self._counts.get("unknown", 0) + 1
