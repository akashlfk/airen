"""Redshift → Phoenix span replay.

Reads recent prediction rows from Redshift (real or mock) and emits them
as OTEL spans into the local Phoenix project. Same span shape as
demo/synthetic_data.py — so Sentinel and Investigator can monitor real
production data without ANY code change.

This is the canonical FK-real data path:
    LSTM service → Kafka → Redshift (FK pipeline)
                              │
                              │ this script reads
                              ▼
                       redshift_replay.py
                              │
                              │ emits OTEL spans
                              ▼
                       local Phoenix
                              │
                              ▼
                       Sentinel / Investigator / Validator
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from airen.adapters.redshift_base import RedshiftAdapter


class RedshiftReplay:
    """Loops: query rows → emit spans → wait → repeat."""

    def __init__(
        self,
        adapter: RedshiftAdapter,
        predictions_table: str,
        tracer: trace.Tracer,
        log_every: int = 25,
    ) -> None:
        self.adapter = adapter
        self.predictions_table = predictions_table
        self.tracer = tracer
        self.log_every = log_every
        self._counts: dict[str, int] = {"healthy": 0, "bugged": 0, "unknown": 0}

    def replay(self, max_rows: int | None = None, since_minutes: int | None = 60) -> int:
        """Emit spans for all rows from `since_minutes` ago. Returns count emitted."""
        since = None
        if since_minutes is not None:
            from datetime import timedelta

            since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)

        limit = max_rows or 1000
        rows = self.adapter.query_predictions(
            table=self.predictions_table, since=since, limit=limit
        )

        emitted = 0
        for row in rows:
            self._emit_span(row)
            emitted += 1
            if emitted % self.log_every == 0:
                print(f"  emitted {emitted}/{len(rows)} spans — counts: {self._counts}")

        print(f"\nDone. Total {emitted} spans. Counts: {self._counts}")
        return emitted

    def _emit_span(self, row: dict[str, Any]) -> None:
        regime = row.get("_regime", "unknown")
        with self.tracer.start_as_current_span("predict_eta") as span:
            span.set_attribute(
                SpanAttributes.OPENINFERENCE_SPAN_KIND,
                OpenInferenceSpanKindValues.LLM.value,
            )

            # Build feature payload — pass through all non-eval fields
            features = {
                k: _serialize(v)
                for k, v in row.items()
                if not k.startswith("_")
                and k not in {"predicted_eta_minutes", "actual_eta_minutes"}
            }
            span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps(features, default=str))
            span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")
            span.set_attribute(SpanAttributes.LLM_MODEL_NAME, row.get("model_name", "unknown"))

            # mlre.* custom attrs — Sentinel + Investigator filter on these
            span.set_attribute("mlre.request_id", str(row.get("request_id", "")))
            span.set_attribute("mlre.model.version", str(row.get("model_version", "")))
            ts = row.get("timestamp")
            span.set_attribute(
                "mlre.timestamp",
                ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            )
            for key in (
                "seq_len",
                "api_fetch_limit",
                "n_pings_used",
                "shipper",
                "miles_remaining",
            ):
                if key in row and row[key] is not None:
                    span.set_attribute(f"mlre.input.{key}", row[key])

            predicted = row.get("predicted_eta_minutes")
            actual = row.get("actual_eta_minutes")
            if predicted is not None:
                span.set_attribute("mlre.prediction.eta_minutes", float(predicted))
                span.set_attribute(
                    SpanAttributes.OUTPUT_VALUE, json.dumps({"eta_minutes": float(predicted)})
                )
                span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")
            if actual is not None:
                span.set_attribute("mlre.ground_truth.actual_minutes", float(actual))
            if predicted is not None and actual is not None:
                span.set_attribute(
                    "mlre.eval.absolute_error_minutes",
                    abs(float(predicted) - float(actual)),
                )
            span.set_attribute("mlre.eval.regime", regime)
            self._counts[regime] = self._counts.get(regime, 0) + 1


def _serialize(v: Any) -> Any:
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v
