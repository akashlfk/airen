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


def summarize_dataframe(df: pd.DataFrame, source: str = "?") -> ObservedSchema:
    """Turn a sample DataFrame into a typed field summary (pure)."""
    out = ObservedSchema(source=source, n_sampled=int(len(df)))
    if df.empty:
        return out
    for col in df.columns:
        s = df[col].dropna()
        if s.empty:
            kind, sample = "other", None
        elif pd.api.types.is_bool_dtype(s):
            kind, sample = "bool", bool(s.iloc[0])
        elif pd.api.types.is_numeric_dtype(pd.to_numeric(s, errors="coerce")) and pd.to_numeric(s, errors="coerce").notna().mean() > 0.8:
            kind, sample = "numeric", float(pd.to_numeric(s, errors="coerce").dropna().iloc[0])
        else:
            kind, sample = "categorical", str(s.iloc[0])
        out.fields.append(ObservedField(
            name=str(col), kind=kind, sample=sample, n_distinct=int(s.nunique()),
        ))
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
    }
    return RoleProposal(tap_field_map=field_map, observation=observation, notes=notes, ambiguities=amb)


# ───────────────────────────────────────────────────────────────────────
#  connection-info gaps + live probe
# ───────────────────────────────────────────────────────────────────────
def required_connection_info(config: AirenServiceConfig) -> list[tuple[str, str]]:
    """What's still needed to REACH the source, so the agent can prompt for it.
    Returns [(field_path, human_prompt)]. Secrets are reported by env-var name."""
    import os

    src = config.serving.prediction_source or "phoenix"
    gaps: list[tuple[str, str]] = []
    if src == "kafka":
        k = config.kafka
        # non-secrets → yaml (prompted + written to airen.yaml)
        if not (k and k.bootstrap_servers) and not os.environ.get("KAFKA_BOOTSTRAP_SERVERS"):
            gaps.append(("kafka.bootstrap_servers", "Kafka bootstrap servers (host:port)"))
        if not (k and k.output_topic):
            gaps.append(("kafka.output_topic", "Kafka topic that carries predictions"))
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

    try:
        df = fetch_predictions(config, window_minutes=window_minutes, max_rows=max_rows)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    return summarize_dataframe(df, source=config.serving.prediction_source or "phoenix")
