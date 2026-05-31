"""Mock Redshift adapter — synthesizes rows matching the expected shape.

Lets us:
  - Test Validator Phase 2 + ingestion code without VPN
  - Run unit tests in CI without a real Redshift cluster
  - Demo Airen end-to-end when working from a laptop

Row shape matches our synthetic prediction emitter (demo/synthetic_data.py)
plus an `actual_eta_minutes` column for the actuals table — same fields
Sentinel and Investigator already understand.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any


HEALTHY_API_FETCH_LIMIT = 184
HEALTHY_MAE_BASE = 610.0
BUGGED_API_FETCH_LIMIT = 73
BUGGED_MAE_BASE = 1277.0

MODEL_NAME = "tl_eta_lstm_v3"
MODEL_VERSION = "d9de2cc6"


class MockRedshiftAdapter:
    """Returns synthetic rows resembling FK's Redshift output."""

    def __init__(self, bugged_fraction: float = 0.20, seed: int | None = None) -> None:
        self.bugged_fraction = bugged_fraction
        self._rng = random.Random(seed)

    def query(self, sql: str, params: tuple | dict | None = None) -> list[dict[str, Any]]:
        # Generic query — we just generate a small batch
        return [self._make_prediction_row() for _ in range(10)]

    def query_predictions(
        self,
        table: str,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        n = min(limit, 200)
        rows = [self._make_prediction_row() for _ in range(n)]
        # Sort newest-first to mimic the real query's ORDER BY timestamp DESC
        rows.sort(key=lambda r: r["timestamp"], reverse=True)
        return rows

    def query_actuals(
        self,
        table: str,
        load_ids: list[str] | None = None,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if load_ids:
            # Generate one actual per requested load_id
            return [self._make_actual_row(lid) for lid in load_ids[:limit]]
        return [self._make_actual_row() for _ in range(min(limit, 100))]

    def close(self) -> None:
        return

    # ─── row factories ───
    def _make_prediction_row(self) -> dict[str, Any]:
        bugged = self._rng.random() < self.bugged_fraction
        api_fetch_limit = BUGGED_API_FETCH_LIMIT if bugged else HEALTHY_API_FETCH_LIMIT
        miles = self._rng.randint(50, 2000)
        actual_eta = miles * 1.2 + self._rng.gauss(0, 60)
        sigma = BUGGED_MAE_BASE if bugged else HEALTHY_MAE_BASE / 4
        predicted_eta = actual_eta + self._rng.gauss(0, sigma)
        ts = datetime.now(timezone.utc) - timedelta(minutes=self._rng.randint(0, 120))
        return {
            "request_id": str(uuid.uuid4()),
            "load_id": f"LD{self._rng.randint(100000, 999999)}",
            "shipper": self._rng.choice(["Fritolay", "Shipper A", "Shipper B"]),
            "model_name": MODEL_NAME,
            "model_version": MODEL_VERSION,
            "predicted_eta_minutes": round(max(0.0, predicted_eta), 2),
            "actual_eta_minutes": round(max(0.0, actual_eta), 2),
            "api_fetch_limit": api_fetch_limit,
            "seq_len": 48,
            "n_pings_used": min(api_fetch_limit, self._rng.randint(50, 250)),
            "miles_remaining": miles,
            "timestamp": ts,
            "_regime": "bugged" if bugged else "healthy",
        }

    def _make_actual_row(self, load_id: str | None = None) -> dict[str, Any]:
        ts = datetime.now(timezone.utc) - timedelta(hours=self._rng.randint(0, 48))
        miles = self._rng.randint(50, 2000)
        actual_eta = miles * 1.2 + self._rng.gauss(0, 60)
        return {
            "load_id": load_id or f"LD{self._rng.randint(100000, 999999)}",
            "shipper": self._rng.choice(["Fritolay", "Shipper A", "Shipper B"]),
            "actual_eta_minutes": round(max(0.0, actual_eta), 2),
            "delivered_at": ts,
            "haul_type": "long_haul" if miles > 400 else "short_haul",
        }
