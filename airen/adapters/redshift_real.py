"""Real Redshift adapter — psycopg2-backed.

Redshift speaks the Postgres wire protocol, so psycopg2 just works.

Connection auth, in priority order:
  1. REDSHIFT_DSN — single URL like postgresql://user:pass@host:5439/db?sslmode=require
  2. Split env vars: REDSHIFT_HOST, REDSHIFT_PORT, REDSHIFT_DATABASE,
     REDSHIFT_USER, REDSHIFT_PASSWORD, REDSHIFT_SSLMODE.

Will fail loudly with a clear message if neither is set. Connection is
opened lazily on the first query, closed via .close() or context manager.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any


class RealRedshiftAdapter:
    """Live Redshift connection via psycopg2. Only used on the FK VM (laptop VPN can't route)."""

    def __init__(self) -> None:
        try:
            import psycopg2  # noqa: F401
            from psycopg2.extras import RealDictCursor  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "psycopg2 not installed. Install with:\n"
                "    conda run -n mlre pip install 'psycopg2-binary>=2.9'"
            ) from e

        self._conn = None  # lazy
        self._closed = False

    # ─── connection management ───
    def _connect(self):
        if self._conn is not None:
            return self._conn

        import psycopg2
        from psycopg2.extras import RealDictCursor

        dsn = os.environ.get("REDSHIFT_DSN", "").strip()
        if dsn:
            self._conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
            return self._conn

        # Build from split fields
        required = ("REDSHIFT_HOST", "REDSHIFT_DATABASE", "REDSHIFT_USER", "REDSHIFT_PASSWORD")
        missing = [k for k in required if not os.environ.get(k, "").strip()]
        if missing:
            raise RuntimeError(
                f"Redshift config missing. Set REDSHIFT_DSN, OR all of: {', '.join(missing)}. "
                f"See .env.example for the template."
            )
        self._conn = psycopg2.connect(
            host=os.environ["REDSHIFT_HOST"].strip(),
            port=int(os.environ.get("REDSHIFT_PORT", "5439")),
            dbname=os.environ["REDSHIFT_DATABASE"].strip(),
            user=os.environ["REDSHIFT_USER"].strip(),
            password=os.environ["REDSHIFT_PASSWORD"],
            sslmode=os.environ.get("REDSHIFT_SSLMODE", "require"),
            connect_timeout=10,
            cursor_factory=RealDictCursor,
        )
        # Autocommit so each SELECT is its own short transaction. We deliberately
        # do NOT set readonly=True at the client — Redshift Spectrum reads need to
        # spill results to internal temp areas, which `set_session(readonly=True)`
        # blocks. Server-side enforcement comes from the `dsreadonly` IAM role;
        # the client flag would just be a redundant footgun.
        self._conn.autocommit = True
        return self._conn

    # ─── public API ───
    def query(self, sql: str, params: tuple | dict | None = None) -> list[dict[str, Any]]:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def query_predictions(
        self,
        table: str,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        # Defensive: table name comes from config, not user input, but we still
        # validate it as a SQL identifier to defeat any injection.
        _assert_safe_identifier(table)
        where = ""
        params: tuple = ()
        if since is not None:
            where = "WHERE timestamp >= %s"
            params = (since,)
        sql = f"""
            SELECT *
            FROM {table}
            {where}
            ORDER BY timestamp DESC
            LIMIT %s
        """
        return self.query(sql, params + (limit,))

    def query_actuals(
        self,
        table: str,
        load_ids: list[str] | None = None,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        _assert_safe_identifier(table)
        wheres: list[str] = []
        params: list[Any] = []
        if load_ids:
            placeholders = ",".join(["%s"] * len(load_ids))
            wheres.append(f"load_id IN ({placeholders})")
            params.extend(load_ids)
        if since is not None:
            wheres.append("delivered_at >= %s")
            params.append(since)
        where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        sql = f"""
            SELECT *
            FROM {table}
            {where_clause}
            ORDER BY delivered_at DESC
            LIMIT %s
        """
        params.append(limit)
        return self.query(sql, tuple(params))

    def close(self) -> None:
        if self._closed or self._conn is None:
            return
        try:
            self._conn.close()
        except Exception:
            pass
        self._closed = True


# ─── helpers ───
def _assert_safe_identifier(s: str) -> None:
    """Allow only letters, digits, underscore, and a single dot (schema.table)."""
    if not s or not all(part.replace("_", "").isalnum() for part in s.split(".")):
        raise ValueError(f"Unsafe table identifier: {s!r}")
    if s.count(".") > 1:
        raise ValueError(f"Table identifier has too many dots: {s!r}")
