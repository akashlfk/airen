"""Real S3 adapter — boto3-backed.

Auth (priority order):
  1. Default boto3 credential chain (env vars, ~/.aws/credentials, IAM role)
  2. Explicit AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY env vars are picked up
     by boto3 automatically — no special handling needed here

Bucket comes from S3_BASELINES_BUCKET env var; region from AWS_REGION
(defaults to us-east-1 if unset).

Failure mode: every method returns None / [] on error rather than raising,
so a missing object or unreachable bucket gracefully degrades to "no
baseline available" upstream. Sentinel then falls back to airen.yaml.
"""

from __future__ import annotations

import json
import os
from typing import Any

from airen.adapters.s3_base import TrainingBaseline

# Default region — boto3 will use this if AWS_REGION isn't set.
DEFAULT_REGION = "us-east-1"


class RealS3Adapter:
    """Live S3 client via boto3. Only used when AIREN_S3_MODE=real."""

    def __init__(self, bucket: str | None = None) -> None:
        try:
            import boto3  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "boto3 not installed. Install with:\n"
                "    conda run -n mlre pip install 'boto3>=1.34'"
            ) from e

        resolved_bucket = (bucket or os.environ.get("S3_BASELINES_BUCKET", "")).strip()
        if not resolved_bucket:
            raise RuntimeError(
                "S3_BASELINES_BUCKET env var is required for real S3 mode."
            )
        self.bucket = resolved_bucket
        self._client = None  # lazy

    def _get_client(self):
        if self._client is not None:
            return self._client
        import boto3

        region = os.environ.get("AWS_REGION", DEFAULT_REGION).strip() or DEFAULT_REGION
        self._client = boto3.client("s3", region_name=region)
        return self._client

    # ─── public API ───
    def get_object(self, key: str) -> bytes | None:
        try:
            client = self._get_client()
            resp = client.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"].read()
        except Exception as e:
            # ClientError(404), NoSuchKey, network — all collapse to "not found"
            # so callers can fall back to hardcoded baselines.
            err_name = type(e).__name__
            if err_name not in {"ClientError", "NoSuchKey", "EndpointConnectionError"}:
                # Unexpected error — log it but still degrade gracefully
                print(f"⚠️  S3 get_object({key!r}) unexpected error: {err_name}: {e}")
            return None

    def get_training_baseline(
        self,
        service: str,
        model_version: str | None = None,
    ) -> TrainingBaseline | None:
        # Try exact version first; fall back to latest.json
        candidates = []
        if model_version:
            candidates.append(f"baselines/{service}/{model_version}.json")
        candidates.append(f"baselines/{service}/latest.json")

        for key in candidates:
            raw = self.get_object(key)
            if raw is None:
                continue
            try:
                return json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                print(f"⚠️  S3 baseline {key!r} is malformed JSON: {e}")
                continue
        return None

    def list_baselines(self, service: str) -> list[str]:
        try:
            client = self._get_client()
            resp = client.list_objects_v2(
                Bucket=self.bucket,
                Prefix=f"baselines/{service}/",
            )
            return [obj["Key"] for obj in resp.get("Contents", [])]
        except Exception as e:
            print(f"⚠️  S3 list_baselines({service!r}) failed: {type(e).__name__}: {e}")
            return []

    def close(self) -> None:
        # boto3 clients don't need explicit cleanup
        self._client = None
