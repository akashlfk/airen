"""Mock Kafka adapter — synthesizes prediction messages that look like the real ones.

Generation logic mirrors demo/synthetic_data.py exactly so a Phoenix span emitted
by this tap is indistinguishable from one emitted by the demo emitter. That means
Sentinel + Investigator work against mock data with no code changes — the same
api_fetch_limit=73 anomaly will appear and be caught.

Usage:
    adapter = MockKafkaAdapter(rate_per_second=10, bugged_fraction=0.2)
    for msg in adapter.consume(max_messages=50):
        ...
"""

from __future__ import annotations

import random
import time
import uuid
from datetime import datetime, timezone
from typing import Iterator


HEALTHY_API_FETCH_LIMIT = 184
HEALTHY_MAE_BASE = 610.0

BUGGED_API_FETCH_LIMIT = 73
BUGGED_MAE_BASE = 1277.0

MODEL_NAME = "tl_eta_lstm_v3"
MODEL_VERSION = "d9de2cc6"


class MockKafkaAdapter:
    """Yields synthetic prediction messages matching FK's expected shape.

    The 79/20/1 healthy/bugged/failed split mirrors what synthetic_data.py emits
    so downstream Sentinel detects the same Fritolay-style signal.
    """

    def __init__(
        self,
        rate_per_second: float = 10.0,
        bugged_fraction: float = 0.20,
        fail_fraction: float = 0.01,
        topic: str = "tl_eta_predictions",
        seed: int | None = None,
    ) -> None:
        self.rate_per_second = rate_per_second
        self.bugged_fraction = bugged_fraction
        self.fail_fraction = fail_fraction
        self.topic = topic
        self._rng = random.Random(seed)

    def consume(self, max_messages: int | None = None) -> Iterator[dict]:
        i = 0
        interval = 1.0 / max(self.rate_per_second, 0.01)
        while max_messages is None or i < max_messages:
            i += 1
            yield self._make_message()
            time.sleep(interval)

    def close(self) -> None:
        # Nothing to release for the mock
        return

    # ───── internals ─────
    def _make_message(self) -> dict:
        roll = self._rng.random()
        if roll < self.fail_fraction:
            return self._make_failed()
        bugged = roll < (self.fail_fraction + self.bugged_fraction)
        return self._make_prediction(bugged=bugged)

    def _make_prediction(self, bugged: bool) -> dict:
        api_fetch_limit = BUGGED_API_FETCH_LIMIT if bugged else HEALTHY_API_FETCH_LIMIT
        n_pings = min(api_fetch_limit, self._rng.randint(50, 250))
        miles = self._rng.randint(50, 2000)
        actual = miles * 1.2 + self._rng.gauss(0, 60)
        sigma = BUGGED_MAE_BASE if bugged else HEALTHY_MAE_BASE / 4
        predicted = actual + self._rng.gauss(0, sigma)
        actual = max(0.0, actual)
        predicted = max(0.0, predicted)

        return {
            "request_id": str(uuid.uuid4()),
            "load_id": f"LD{self._rng.randint(100000, 999999)}",
            "shipper": self._rng.choices(
                ["Fritolay", "Shipper A", "Shipper B"], weights=[0.5, 0.3, 0.2]
            )[0],
            "origin_state": self._rng.choice(["IL", "TX", "CA", "OH"]),
            "dest_state": self._rng.choice(["GA", "FL", "NY", "WA"]),
            "miles_remaining": miles,
            "n_pings_used": n_pings,
            "api_fetch_limit": api_fetch_limit,
            "seq_len": 48,
            "model_name": MODEL_NAME,
            "model_version": MODEL_VERSION,
            "predicted_eta_minutes": predicted,
            "actual_eta_minutes": actual,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "_topic": self.topic,
            "_regime": "bugged" if bugged else "healthy",
        }

    def _make_failed(self) -> dict:
        # A failed-inference message — model didn't produce a prediction
        return {
            "request_id": str(uuid.uuid4()),
            "load_id": f"LD{self._rng.randint(100000, 999999)}",
            "shipper": self._rng.choice(["Fritolay", "Shipper A", "Shipper B"]),
            "model_name": MODEL_NAME,
            "model_version": MODEL_VERSION,
            "error_type": "FeatureFetchTimeout",
            "error_message": "redshift ping fetch timed out",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "_topic": self.topic,
            "_regime": "failed",
        }
