"""Factory + facade for the S3 adapter.

Pick mode via AIREN_S3_MODE in the environment. Defaults to 'mock' so laptop
dev works without AWS creds; flip to 'real' once S3_BASELINES_BUCKET and
AWS_* creds are configured.

Modes:
    mock  (default) — MockS3Adapter; synthesizes baselines, no network.
    real            — RealS3Adapter; opens a boto3 client.
"""

from __future__ import annotations

import os
from typing import Optional

from airen.adapters.s3_base import S3Adapter


def get_s3_adapter(mode: Optional[str] = None, *, bucket=None, region=None) -> S3Adapter:
    """`bucket`/`region` come from airen.yaml (s3 block); both fall back to env
    (S3_BASELINES_BUCKET / AWS_REGION) when None. AWS credentials are read by
    boto3 from the standard AWS_* env vars — never passed here."""
    resolved = (mode or os.environ.get("AIREN_S3_MODE", "mock")).strip().lower()
    if resolved == "real":
        from airen.adapters.s3_real import RealS3Adapter

        return RealS3Adapter(bucket=bucket, region=region)
    if resolved == "mock":
        from airen.adapters.s3_mock import MockS3Adapter

        return MockS3Adapter()
    raise ValueError(f"Unknown AIREN_S3_MODE: {mode!r}. Expected 'mock' or 'real'.")
