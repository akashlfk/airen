"""Abstract Redshift adapter — the contract every implementation honors.

Same Protocol pattern as kafka_base.py. Upstream code (Sentinel, Investigator,
Validator) calls methods on this interface; the actual implementation
(`RealRedshiftAdapter` via psycopg2, `MockRedshiftAdapter` via synthetic rows)
is selected at startup based on `AIREN_REDSHIFT_MODE`.

Why a thin Protocol vs a full ORM:
  - We only do read queries against well-known table shapes.
  - Adapter swap is one env var, not a config-rewrite.
  - Mock-mode developers don't pay the psycopg2 import cost.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol


class RedshiftAdapter(Protocol):
    """Read-only Redshift client. Returns dicts so callers don't import psycopg2 cursors."""

    def query(self, sql: str, params: tuple | dict | None = None) -> list[dict[str, Any]]:
        """Run an arbitrary SELECT. Returns one dict per row (column_name → value)."""
        ...

    def query_predictions(
        self,
        table: str,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Fetch recent prediction rows (the Kafka-mirror table)."""
        ...

    def query_actuals(
        self,
        table: str,
        load_ids: list[str] | None = None,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Fetch delivered-ETA rows (Validator Phase 2 uses this for actuals MAE)."""
        ...

    def close(self) -> None:
        """Release the connection."""
        ...
