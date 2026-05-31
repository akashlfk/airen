"""Abstract S3 adapter — the contract every implementation honors.

Sentinel and Investigator use S3 to fetch training-time baselines: what the
model's MAE looked like on the training set, and what feature-attribute
distributions the model was trained on. These are MUCH richer than the
hand-tuned `attribute_baselines` in airen.yaml — they come straight from
the training pipeline.

Expected S3 layout:
    s3://<bucket>/baselines/<service>/<model_version>.json
    s3://<bucket>/baselines/<service>/latest.json   (optional pointer)

The JSON shape is documented in TrainingBaseline below.

Mode selected via AIREN_S3_MODE env var. Defaults to mock so laptop dev works
without AWS creds; flip to 'real' when AWS_ACCESS_KEY_ID is configured.
"""

from __future__ import annotations

from typing import Any, Protocol, TypedDict


class TrainingBaseline(TypedDict, total=False):
    """The JSON shape stored at s3://bucket/baselines/<svc>/<version>.json.

    All fields are optional so old/partial baselines still load — Sentinel
    falls back to airen.yaml hand-tuned values when a field is missing.
    """

    service: str
    model_name: str
    model_version: str
    trained_at: str  # ISO 8601
    training_window_days: int
    n_training_examples: int
    metrics: dict[str, float]  # e.g. {"mae_minutes": 118.4}
    attribute_distributions: dict[str, dict[str, float]]
    # {"api_fetch_limit": {"184": 1.0}}  — value → fraction


class S3Adapter(Protocol):
    """Read-only S3 client. Returns parsed Python objects, not boto3 types."""

    def get_object(self, key: str) -> bytes | None:
        """Fetch raw bytes from `s3://<bucket>/<key>`. None if not found."""
        ...

    def get_training_baseline(
        self,
        service: str,
        model_version: str | None = None,
    ) -> TrainingBaseline | None:
        """Fetch the training baseline JSON for a service/model_version pair.

        If `model_version` is None, fetches `latest.json` (the pointer maintained
        by the training pipeline). Returns None when the object doesn't exist —
        callers fall back to yaml-hardcoded baselines in that case.
        """
        ...

    def list_baselines(self, service: str) -> list[str]:
        """List available baseline keys under `baselines/<service>/`. Empty list
        if the prefix has nothing or the bucket is unreachable.
        """
        ...

    def close(self) -> None:
        """Release any underlying connections/clients. No-op for stateless impls."""
        ...
