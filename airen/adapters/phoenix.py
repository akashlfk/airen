"""Phoenix adapter — low-level span fetching.

Layer: this is the boring SDK call. Higher-level analysis (computing MAE,
detecting drift) lives in airen/tools/health.py.

Why a separate adapter file: when we swap Phoenix-local for Phoenix Cloud,
or later add a BigQuery adapter for the FK-real demo path, the agent code
stays untouched. Same call signature, different backend.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from phoenix.client import Client


def _client() -> Client:
    base_url = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")
    return Client(base_url=base_url)


def get_recent_spans(
    project_name: str,
    lookback_minutes: int = 60,
    limit: int = 10_000,
) -> pd.DataFrame:
    """Return spans from `project_name` in the last `lookback_minutes`.

    The returned DataFrame is the raw Phoenix shape — nested `attributes.mlre`
    column holds a dict. Use _flatten_mlre() to expand it.
    """
    spans = _client().spans.get_spans_dataframe(
        project_identifier=project_name, limit=limit
    )
    if spans.empty:
        return spans

    # Filter to lookback window. Phoenix stores start_time as nanoseconds since epoch.
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    if "start_time" in spans.columns:
        spans = spans[pd.to_datetime(spans["start_time"], utc=True) >= cutoff]
    return spans


def flatten_mlre(spans: pd.DataFrame) -> pd.DataFrame:
    """Phoenix nests dotted attributes; expand `attributes.mlre` into flat columns."""
    if spans.empty or "attributes.mlre" not in spans.columns:
        return spans
    expanded = pd.json_normalize(spans["attributes.mlre"].dropna())
    expanded.index = spans["attributes.mlre"].dropna().index
    return spans.join(expanded)


def list_projects() -> list[str]:
    """Discover Phoenix projects (useful when an agent doesn't know names)."""
    try:
        return [p.name for p in _client().projects.list()]
    except Exception:
        return []
