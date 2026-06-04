"""Config-driven Kafka→Phoenix tap mapping (airen.ingestion.kafka_tap).

A fake tracer records set_attribute calls so we can assert that an arbitrary
message schema maps to the right mlre.* span attributes — no broker, no Phoenix.
"""

from __future__ import annotations

from airen.config import TapFieldMap
from airen.ingestion.kafka_tap import KafkaTap


class _RecSpan:
    def __init__(self):
        self.attrs = {}

    def set_attribute(self, k, v):
        self.attrs[k] = v

    def set_status(self, *a):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _RecTracer:
    def __init__(self):
        self.spans = []

    def start_as_current_span(self, name):
        s = _RecSpan()
        self.spans.append(s)
        return s


def test_mapped_emit_uses_field_map_for_arbitrary_schema():
    fm = TapFieldMap(
        prediction="risk_score", actual="label", model_version="version",
        timestamp="ts", inputs=["merchant", "region"],
    )
    tracer = _RecTracer()
    tap = KafkaTap(adapter=None, tracer=tracer, field_map=fm)
    tap._emit_span_mapped({
        "risk_score": 0.9, "label": 1, "version": "v4", "ts": "2026-06-02T00:00:00Z",
        "merchant": "acme", "region": "us-west", "ignored_internal": 5,
    })
    a = tracer.spans[0].attrs
    assert a["mlre.input.merchant"] == "acme"
    assert a["mlre.input.region"] == "us-west"
    assert a["mlre.model.version"] == "v4"
    assert a["mlre.prediction.value"] == 0.9
    assert a["mlre.ground_truth.value"] == 1.0
    # error is computed from prediction/actual when no explicit error field
    assert abs(a["mlre.eval.error"] - abs(0.9 - 1.0)) < 1e-9


def test_mapped_emit_explicit_error_field():
    fm = TapFieldMap(prediction="pred", error="err", inputs=["seg"])
    tracer = _RecTracer()
    KafkaTap(adapter=None, tracer=tracer, field_map=fm)._emit_span_mapped(
        {"pred": 10.0, "err": 4.2, "seg": "A"}
    )
    a = tracer.spans[0].attrs
    assert a["mlre.eval.error"] == 4.2
    assert a["mlre.input.seg"] == "A"


def test_mapped_emit_empty_inputs_copies_all_scalars():
    fm = TapFieldMap(prediction="pred")  # inputs=[] → copy all non-mapped scalars
    tracer = _RecTracer()
    KafkaTap(adapter=None, tracer=tracer, field_map=fm)._emit_span_mapped(
        {"pred": 1.0, "shipper": "X", "limit": 73, "_topic": "ignored"}
    )
    a = tracer.spans[0].attrs
    assert a["mlre.input.shipper"] == "X"
    assert a["mlre.input.limit"] == 73
    assert "mlre.input._topic" not in a  # underscore-prefixed are skipped


def test_default_path_still_eta_shaped_when_no_field_map():
    tracer = _RecTracer()
    tap = KafkaTap(adapter=None, tracer=tracer)  # no field_map → ETA default
    tap._emit_span({
        "predicted_eta_minutes": 600.0, "actual_eta_minutes": 150.0,
        "shipper": "Fritolay", "api_fetch_limit": 73, "model_version": "v3",
        "_regime": "bugged",
    })
    a = tracer.spans[0].attrs
    assert a["mlre.eval.absolute_error_minutes"] == 450.0
    assert a["mlre.input.shipper"] == "Fritolay"
