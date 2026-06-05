"""Prediction-source dispatcher (#4).

Sentinel and calibration both need "the recent predictions as a DataFrame".
WHERE those live depends on the app's serving topology, declared in
`serving.prediction_source`:

    phoenix   → prediction spans (the default; full input.*/eval.* schema)
    kafka     → peek the output topic (raw message fields as columns)
    redshift  → query the predictions table (row columns)

This is the one place that knows about all three. Everything downstream just
gets a flattened DataFrame with a `start_time` column and whatever attributes
the source carries. For kafka/redshift the `observation.error_attribute` /
`segments` must name the real message/row fields.

All paths are mock-friendly (the kafka/redshift adapters synthesize when their
AIREN_*_MODE is mock), so this works offline and in tests.
"""

from __future__ import annotations

import itertools
import os

import pandas as pd

from airen.config import AirenServiceConfig


def source_label(config: AirenServiceConfig) -> str:
    return config.serving.prediction_source or "phoenix"


def fetch_predictions(config: AirenServiceConfig, window_minutes: int, max_rows: int = 2000) -> pd.DataFrame:
    """Return recent predictions as a flattened DataFrame, from whichever source
    the service declares. Never raises for an empty/unreachable source — returns
    an empty DataFrame so callers degrade to the no-data path."""
    src = source_label(config)
    try:
        if src == "kafka":
            return _from_kafka(config, max_rows)
        if src == "redshift":
            return _from_redshift(config, window_minutes, max_rows)
        return _from_phoenix(config, window_minutes)
    except Exception as e:  # noqa: BLE001 — degrade to no-data, but say WHY
        import sys
        print(f"  ⚠ fetch_predictions({src}) failed: {type(e).__name__}: {e}", file=sys.stderr)
        return pd.DataFrame()


def _from_phoenix(config: AirenServiceConfig, window_minutes: int) -> pd.DataFrame:
    from airen.adapters.phoenix import flatten_mlre, get_recent_spans

    spans = get_recent_spans(project_name=config.phoenix.project_name, lookback_minutes=window_minutes)
    return flatten_mlre(spans)


def _from_kafka(config: AirenServiceConfig, max_rows: int) -> pd.DataFrame:
    topic = (config.kafka.output_topic if config.kafka else None) or "predictions"
    mode = os.environ.get("AIREN_KAFKA_MODE", "mock").strip().lower()
    if mode == "real":
        from airen.adapters.kafka_real import RealKafkaAdapter

        k = config.kafka
        adapter = RealKafkaAdapter(
            topic=topic,
            bootstrap_servers=k.bootstrap_servers if k else None,
            security_protocol=k.security_protocol if k else None,
            sasl_mechanism=k.sasl_mechanism if k else None,
        )
    else:
        from airen.adapters.kafka_mock import MockKafkaAdapter

        # High rate so peeking max_rows is instant (the mock sleeps between msgs).
        adapter = MockKafkaAdapter(topic=topic, rate_per_second=1e6)
    try:
        msgs = list(itertools.islice(adapter.consume(max_rows), max_rows))
    finally:
        adapter.close()
    df = pd.DataFrame(msgs)
    return _ensure_start_time(df)


def _from_redshift(config: AirenServiceConfig, window_minutes: int, max_rows: int) -> pd.DataFrame:
    from datetime import datetime, timedelta, timezone

    from airen.adapters.redshift import get_redshift_adapter

    rc = config.redshift
    table = rc.table if rc else None
    if not table:
        return pd.DataFrame()
    adapter = get_redshift_adapter(config=rc)
    try:
        since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        rows = adapter.query_predictions(table, since=since, limit=max_rows)
    finally:
        adapter.close()
    return _ensure_start_time(pd.DataFrame(rows))


def _ensure_start_time(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize a timestamp column to `start_time` so the reliability battery's
    recent-vs-earlier split works regardless of source."""
    if df.empty or "start_time" in df.columns:
        return df
    for cand in ("timestamp", "ts", "event_time", "created_at", "prediction_time"):
        if cand in df.columns:
            return df.assign(start_time=df[cand])
    return df
