"""Factory + facade for the MLflow adapter.

Avoids name collision with the top-level `mlflow` package — adapter module
is named `mlflow_adapter` rather than `mlflow`.

Pick mode via AIREN_MLFLOW_MODE in the environment.
    mock  (default) — MockMLflowAdapter; canned training run, no network.
    real            — RealMLflowAdapter; hits MLFLOW_TRACKING_URI via VPN.
"""

from __future__ import annotations

import os
from typing import Optional

from airen.adapters.mlflow_base import MLflowAdapter


def get_mlflow_adapter(mode: Optional[str] = None) -> MLflowAdapter:
    resolved = (mode or os.environ.get("AIREN_MLFLOW_MODE", "mock")).strip().lower()
    if resolved == "real":
        from airen.adapters.mlflow_real import RealMLflowAdapter

        return RealMLflowAdapter()
    if resolved == "mock":
        from airen.adapters.mlflow_mock import MockMLflowAdapter

        return MockMLflowAdapter()
    raise ValueError(f"Unknown AIREN_MLFLOW_MODE: {mode!r}. Expected 'mock' or 'real'.")
