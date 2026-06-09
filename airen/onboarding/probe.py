"""Live source probe (onboarding Step 2+3).

After graphify reads the *code*, this looks at the *real data*: it samples the
service's live source (read-only) and learns the actual field names + types, then
infers which field is the prediction / actual / error / model_version / segments.
That auto-configures `tap.field_map` and `observation` so a Kafka/Redshift service
is set up without hand-mapping.

Safety:
  • Read-only feeds only — Kafka peek / Phoenix sample / Redshift SELECT.
    We never fire a request at a sync-API (a POST could cause side effects);
    for sync-api use graphify's statically-detected I/O schema instead.
  • To reach a source Airen often needs connection info it can't infer
    (broker, topic, table, creds). `required_connection_info()` surfaces the
    gaps; the onboarding agent prompts for them before probing.
  • Graceful: unreachable / no data → returns None, caller falls back to the
    interview.

Pure helpers (`summarize_dataframe`, `infer_field_roles`) are unit-tested; the
live `probe_source` orchestration reuses the prediction-source dispatcher.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from airen.config import AirenServiceConfig


# ───────────────────────────────────────────────────────────────────────
#  observed schema
# ───────────────────────────────────────────────────────────────────────
@dataclass
class ObservedField:
    name: str
    kind: str          # numeric | categorical | bool | other
    sample: Any
    n_distinct: int


@dataclass
class ObservedSchema:
    source: str
    n_sampled: int
    fields: list[ObservedField] = field(default_factory=list)


def _safe_ndistinct(s: pd.Series) -> int:
    """Distinct count that tolerates unhashable values (nested dict/list fields)."""
    try:
        return int(s.nunique())
    except TypeError:
        return int(len({str(v) for v in s}))


def summarize_dataframe(df: pd.DataFrame, source: str = "?") -> ObservedSchema:
    """Turn a sample DataFrame into a typed field summary (pure). Tolerant of
    nested (dict/list) fields that real prediction messages often carry."""
    out = ObservedSchema(source=source, n_sampled=int(len(df)))
    if df.empty:
        return out
    for col in df.columns:
        s = df[col].dropna()
        if s.empty:
            out.fields.append(ObservedField(str(col), "empty", None, 0))
            continue
        first = s.iloc[0]
        # Nested JSON (dict/list) → not a scalar feature/metric; record + skip.
        if isinstance(first, (dict, list, set, tuple)):
            out.fields.append(ObservedField(str(col), "nested", str(first)[:80], _safe_ndistinct(s)))
            continue
        if pd.api.types.is_bool_dtype(s) or all(isinstance(v, bool) for v in s.head(20)):
            kind, sample = "bool", bool(first)
        else:
            num = pd.to_numeric(s, errors="coerce")
            if num.notna().mean() > 0.8:
                kind, sample = "numeric", float(num.dropna().iloc[0])
            else:
                kind, sample = "categorical", str(first)
                # A categorical that's actually serialized JSON (a dict/list sent
                # as a string) explodes into dotted sub-columns once the tap bridges
                # it to Phoenix — so it's not a usable single segment/feature. Treat
                # it as nested, same as a real dict/list value.
                fs = str(first).strip()
                if fs[:1] in ("{", "[") and fs[-1:] in ("}", "]"):
                    kind = "nested"
        out.fields.append(ObservedField(str(col), kind, sample, _safe_ndistinct(s)))
    return out


# ───────────────────────────────────────────────────────────────────────
#  role inference → proposed tap.field_map + observation
# ───────────────────────────────────────────────────────────────────────
_PATTERNS = {
    "timestamp": re.compile(r"(^|_)(ts|time|timestamp|date|_at|event_time)($|_)", re.I),
    "model_version": re.compile(r"(model[_.]?version|^version$|model_ver)", re.I),
    "error": re.compile(r"(error|mae|rmse|mape|loss|residual|abs[_]?err|deviation)", re.I),
    "actual": re.compile(r"(actual|ground[_]?truth|y[_]?true|label|target|observed)", re.I),
    "prediction": re.compile(r"(predict|prediction|pred|score|proba|prob|estimate|yhat|y[_]?hat|output|eta)", re.I),
}


@dataclass
class RoleProposal:
    tap_field_map: dict          # → services yaml `tap.field_map`
    observation: dict            # → services yaml `observation` (error_attribute, segments)
    notes: list[str] = field(default_factory=list)   # human-facing "I detected…"
    ambiguities: list[str] = field(default_factory=list)  # what to confirm/ask


def infer_field_roles(observed: ObservedSchema) -> RoleProposal:
    """Map observed fields → prediction/actual/error/version/timestamp/segments (pure)."""
    used: set[str] = set()

    def match(role: str, *, numeric: bool) -> str | None:
        pat = _PATTERNS[role]
        for f in observed.fields:
            if f.name in used:
                continue
            if pat.search(f.name) and (not numeric or f.kind == "numeric"):
                used.add(f.name)
                return f.name
        return None

    ts = match("timestamp", numeric=False)
    ver = match("model_version", numeric=False)
    err = match("error", numeric=True)
    actual = match("actual", numeric=True)
    pred = match("prediction", numeric=True)

    # Segments = categorical fields with modest cardinality (good for grouping).
    # Segments are LOW-cardinality categoricals (region, category, shipper) —
    # cap on absolute distinct count so it works on small samples too; exclude
    # high-cardinality ids (user_id, request_id) which aren't useful to group by.
    seg_cap = max(50, observed.n_sampled // 5)
    segs = [
        f.name for f in observed.fields
        if f.name not in used and f.kind == "categorical"
        and 1 < f.n_distinct <= seg_cap
    ]
    used.update(segs)
    # Remaining scalar fields → generic inputs to keep for drift.
    inputs = [f.name for f in observed.fields
              if f.name not in used and f.kind in ("numeric", "categorical", "bool")
              and not _PATTERNS["timestamp"].search(f.name)]

    field_map: dict = {}
    for k, v in (("prediction", pred), ("actual", actual), ("error", err),
                 ("model_version", ver), ("timestamp", ts)):
        if v:
            field_map[k] = v
    field_map["inputs"] = segs + inputs

    notes, amb = [], []
    if pred:
        notes.append(f"prediction ← {pred}")
    else:
        amb.append("Couldn't identify the prediction field — please confirm.")
    if err:
        notes.append(f"error ← {err}")
    elif pred and actual:
        notes.append(f"error ← computed |{pred} - {actual}|")
    else:
        amb.append("No error/actual field — accuracy can't be computed until ground truth arrives.")
    if ver:
        notes.append(f"model_version ← {ver}")
    else:
        amb.append("No model_version field — version-drift detection will be off.")
    if segs:
        notes.append(f"segments ← {', '.join(segs)}")
    else:
        amb.append("No obvious categorical segments — confirm what to group health by.")

    # The tap writes error→mlre.eval.error and inputs[f]→mlre.input.<f>; align observation.
    observation = {
        "error_attribute": "eval.error",
        "segments": [f"input.{s}" for s in segs],
        # No error/actual field in the live feed → it's prediction-only (no ground
        # truth). Record that so Sentinel won't read a missing error attribute as a
        # broken pipeline, and judges health on drift/volume/schema instead.
        "has_ground_truth": bool(err or actual),
    }
    if not (err or actual):
        notes.append("ground truth: none in feed → accuracy disabled, drift/volume/schema only")
    # Reconcile problem_type from the REAL data (overrides graphify's code guess):
    # a score-like prediction (is_correct/label/proba) → classification; a plain
    # numeric prediction → regression.
    if pred:
        pl = pred.lower()
        if any(h in pl for h in ("is_correct", "correct", "accuracy", "label", "class", "proba", "prob")):
            observation["problem_type"] = "classification"
            observation["metric_direction"] = "higher_is_better"
        else:
            observation["problem_type"] = "regression"
            observation["metric_direction"] = "lower_is_better"
        notes.append(f"problem type (from data): {observation['problem_type']}")
    return RoleProposal(tap_field_map=field_map, observation=observation, notes=notes, ambiguities=amb)


# ───────────────────────────────────────────────────────────────────────
#  connection-info gaps + live probe
# ───────────────────────────────────────────────────────────────────────
def probe_source_kind(config: AirenServiceConfig) -> str:
    """Where the PROBE should read — i.e. where the data physically is RIGHT NOW.
    This can differ from the runtime `prediction_source`: an async-kafka service
    reads its predictions from Kafka (the tap later bridges them into Phoenix, so
    runtime prediction_source=phoenix), and a batch service from Redshift."""
    mode = config.serving.mode
    if mode == "async-kafka":
        return "kafka"
    if mode == "batch":
        return "redshift"
    return config.serving.prediction_source or "phoenix"


def required_connection_info(config: AirenServiceConfig) -> list[tuple[str, str]]:
    """What's still needed to REACH the source, so the agent can prompt for it.
    Returns [(field_path, human_prompt)]. Secrets are reported by env-var name."""
    import os

    src = probe_source_kind(config)
    gaps: list[tuple[str, str]] = []
    if src == "kafka":
        k = config.kafka
        # non-secrets → yaml (prompted + written to airen.yaml)
        if not (k and k.bootstrap_servers) and not os.environ.get("KAFKA_BOOTSTRAP_SERVERS"):
            gaps.append(("kafka.bootstrap_servers", "Kafka bootstrap servers (host:port)"))
        if not (k and k.output_topic):
            gaps.append(("kafka.output_topic",
                         "Kafka OUTPUT topic where the model PUBLISHES its predictions "
                         "(the feed Airen watches, e.g. prod_ml_output) — NOT the input/request topic"))
        if not (k and k.security_protocol) and not os.environ.get("KAFKA_SECURITY_PROTOCOL"):
            gaps.append(("kafka.security_protocol", "Kafka security protocol (e.g. SASL_SSL; blank if none)"))
        if not (k and k.sasl_mechanism) and not os.environ.get("KAFKA_SASL_MECHANISM"):
            gaps.append(("kafka.sasl_mechanism", "Kafka SASL mechanism (e.g. PLAIN; blank if none)"))
        # secrets → .env only
        if not os.environ.get("KAFKA_SASL_USERNAME"):
            gaps.append(("env:KAFKA_SASL_USERNAME", "Kafka SASL username"))
        if not os.environ.get("KAFKA_SASL_PASSWORD"):
            gaps.append(("env:KAFKA_SASL_PASSWORD", "Kafka SASL password"))
    elif src == "redshift":
        r = config.redshift
        if not (r and r.host) and not os.environ.get("REDSHIFT_HOST"):
            gaps.append(("redshift.host", "Redshift host"))
        if not (r and r.table):
            gaps.append(("redshift.table", "Redshift predictions table"))
        if not os.environ.get("REDSHIFT_PASSWORD"):
            gaps.append(("env:REDSHIFT_PASSWORD", "Redshift password (set in .env)"))
    else:  # phoenix
        if not config.phoenix.project_name:
            gaps.append(("phoenix.project_name", "Phoenix project name"))
    return gaps


def probe_source(config: AirenServiceConfig, *, max_rows: int = 300, window_minutes: int = 10080) -> ObservedSchema | None:
    """Sample the live source (read-only) and summarize its fields. Returns None
    if unreachable / no data. Reuses the prediction-source dispatcher."""
    from airen.tools.prediction_source import fetch_predictions

    # Probe the source where the data ACTUALLY is (kafka for async-kafka), even if
    # the runtime prediction_source is phoenix (tap-bridged). Read from a copy with
    # prediction_source overridden so fetch_predictions hits the right backend.
    kind = probe_source_kind(config)
    probe_cfg = config.model_copy(deep=True)
    probe_cfg.serving.prediction_source = kind
    try:
        df = fetch_predictions(probe_cfg, window_minutes=window_minutes, max_rows=max_rows)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    return summarize_dataframe(df, source=kind)
