"""Mock S3 adapter — returns canned baselines, OR reads from ./baselines/ if
a real baseline JSON has been extracted by scripts/extract_baseline.py.

Resolution order when `get_training_baseline(service, version)` is called:
  1. Local file at  ./baselines/<service>/<version>.json   (extractor output)
  2. Local file at  ./baselines/<service>/latest.json      (extractor pointer)
  3. MOCK_BASELINES dict below (canned data for unit tests / offline demos)

This lets Akash run the extractor against real S3 → write a local JSON →
have Sentinel automatically use it, without touching AIREN_S3_MODE=real.
The real-mode adapter is for the future "production deploy" path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from airen.adapters.s3_base import TrainingBaseline


# Where the extractor writes baselines locally (matches scripts/extract_baseline.py
# default --out-dir). Resolved relative to current working directory.
LOCAL_BASELINES_DIR = Path("baselines")


# Hardcoded baselines mirror what FK's training pipeline WOULD produce.
# Each entry is what the model saw during training. Sentinel uses these
# to compute drift in production.
MOCK_BASELINES: dict[str, dict[str, TrainingBaseline]] = {
    "tl-eta": {
        "d9de2cc6": {
            "service": "tl-eta",
            "model_name": "tl_eta_lstm_v3",
            "model_version": "d9de2cc6",
            "trained_at": "2026-02-15T10:30:00Z",
            "training_window_days": 90,
            "n_training_examples": 1_245_000,
            "metrics": {
                "mae_minutes": 118.4,
                "mae_p95_minutes": 285.3,
                "mape": 0.082,
            },
            "attribute_distributions": {
                "api_fetch_limit": {"184": 1.0},
                "seq_len": {"48": 1.0},
                "max_pings": {"250": 1.0},
            },
        },
    },
    "ocean-eta": {
        "a3f2b791": {
            "service": "ocean-eta",
            "model_name": "ocean_eta_xgb_v2",
            "model_version": "a3f2b791",
            "trained_at": "2026-04-01T14:00:00Z",
            "training_window_days": 180,
            "n_training_examples": 412_000,
            "metrics": {
                "mae_minutes": 235.7,
                "mae_p95_minutes": 612.0,
                "mape": 0.062,
            },
            "attribute_distributions": {
                "vessel_class": {"container": 0.94, "bulker": 0.06},
                "weather_api_version": {"v3": 1.0},
            },
        },
    },
}


class MockS3Adapter:
    """Returns local baseline JSON if present, else canned synthetic data."""

    def __init__(self, bucket: str = "airen-mock-baselines") -> None:
        self.bucket = bucket

    def get_object(self, key: str) -> bytes | None:
        """Resolve baselines/<svc>/<version>.json and baselines/<svc>/latest.json."""
        parts = key.split("/")
        if len(parts) < 3 or parts[0] != "baselines":
            return None
        _, service, fname = parts[0], parts[1], parts[-1]

        # Try local file first
        local_path = LOCAL_BASELINES_DIR / service / fname
        if local_path.is_file():
            return local_path.read_bytes()

        # Fall back to canned mock data
        versions = MOCK_BASELINES.get(service)
        if not versions:
            return None
        if fname == "latest.json":
            chosen = max(versions.values(), key=lambda b: b.get("trained_at", ""))
            return json.dumps(chosen).encode("utf-8")
        version_id = fname.removesuffix(".json")
        baseline = versions.get(version_id)
        return json.dumps(baseline).encode("utf-8") if baseline else None

    def get_training_baseline(
        self,
        service: str,
        model_version: str | None = None,
    ) -> TrainingBaseline | None:
        # 1. Local file at exact version
        if model_version:
            p = LOCAL_BASELINES_DIR / service / f"{model_version}.json"
            if p.is_file():
                return json.loads(p.read_text())
        # 2. Local "latest" — newest by mtime under baselines/<service>/
        svc_dir = LOCAL_BASELINES_DIR / service
        if svc_dir.is_dir():
            jsons = sorted(svc_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
            if jsons:
                return json.loads(jsons[0].read_text())
        # 3. Canned MOCK_BASELINES fallback
        versions = MOCK_BASELINES.get(service)
        if not versions:
            return None
        if model_version and model_version in versions:
            return dict(versions[model_version])
        return dict(max(versions.values(), key=lambda b: b.get("trained_at", "")))

    def list_baselines(self, service: str) -> list[str]:
        keys: list[str] = []
        # Local files first
        svc_dir = LOCAL_BASELINES_DIR / service
        if svc_dir.is_dir():
            keys.extend(f"baselines/{service}/{p.name}" for p in sorted(svc_dir.glob("*.json")))
        # Plus canned versions
        for v in sorted(MOCK_BASELINES.get(service, {})):
            k = f"baselines/{service}/{v}.json"
            if k not in keys:
                keys.append(k)
        return keys

    def close(self) -> None:
        return
