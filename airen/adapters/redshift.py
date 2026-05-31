"""Factory + facade for the Redshift adapter.

Pick mode via AIREN_REDSHIFT_MODE in the environment (or pass an explicit
adapter to functions that take one). Defaults to 'mock' so laptop dev
just works; flip to 'real' on the FK VM.

Modes:
    mock  (default) — MockRedshiftAdapter; synthesizes rows, no network.
    real            — RealRedshiftAdapter; opens a psycopg2 connection.
"""

from __future__ import annotations

import os
from typing import Optional

from airen.adapters.redshift_base import RedshiftAdapter


def get_redshift_adapter(mode: Optional[str] = None) -> RedshiftAdapter:
    resolved = (mode or os.environ.get("AIREN_REDSHIFT_MODE", "mock")).strip().lower()
    if resolved == "real":
        from airen.adapters.redshift_real import RealRedshiftAdapter

        return RealRedshiftAdapter()
    if resolved == "mock":
        from airen.adapters.redshift_mock import MockRedshiftAdapter

        return MockRedshiftAdapter(
            seed=int(os.environ["AIREN_REDSHIFT_MOCK_SEED"])
            if os.environ.get("AIREN_REDSHIFT_MOCK_SEED")
            else None,
        )
    raise ValueError(f"Unknown AIREN_REDSHIFT_MODE: {mode!r}. Expected 'mock' or 'real'.")
