"""Extract a training-time baseline JSON from an S3 parquet prefix.

This is the one-shot operational tool that produces the `TrainingBaseline`
JSON Airen's Sentinel consumes. Output is written locally (under ./baselines/);
upload to S3 manually when ready.

Stats per column ("rich" mode):
  - count, n_nulls, null_frac
  - For numerics:   mean, std, min, max, percentiles [5, 25, 50, 75, 95]
  - For categorical: top-K most common values + their frequencies
  - For both:       correlation with --target (if column is numeric)

Usage:
    python -m scripts.extract_baseline \\
        --bucket data-science-4k \\
        --prefix OTR_TL_ETA/unified_new/procter-gamble-north-america/train_data/shipper_id=procter-gamble-north-america/ \\
        --service tl-eta \\
        --model-version pg-north-america-2026q1 \\
        --target shippers_expected_journey_minutes \\
        --sample-files 10

Outputs:  ./baselines/<service>/<model_version>.json

The output JSON is shaped to match airen.adapters.s3_base.TrainingBaseline,
extended with a `feature_stats` block carrying the rich per-feature stats.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import pandas as pd
import pyarrow.parquet as pq
from dotenv import load_dotenv

# Top-K values to keep per categorical column
TOP_K = 20
PERCENTILES = [5, 25, 50, 75, 95]


def _list_parquet_keys(s3, bucket: str, prefix: str, limit: int | None = None) -> list[str]:
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".parquet"):
                keys.append(obj["Key"])
                if limit and len(keys) >= limit:
                    return keys
    return keys


def _download_parquet(s3, bucket: str, key: str) -> pd.DataFrame:
    buf = io.BytesIO()
    s3.download_fileobj(bucket, key, buf)
    buf.seek(0)
    return pq.read_table(buf).to_pandas()


def _is_numeric(s: pd.Series) -> bool:
    return pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s)


def _numeric_stats(s: pd.Series) -> dict[str, Any]:
    clean = s.dropna()
    if clean.empty:
        return {"count": 0}
    quantiles = clean.quantile([p / 100 for p in PERCENTILES]).to_dict()
    return {
        "count": int(clean.size),
        "mean": _r(clean.mean()),
        "std": _r(clean.std(ddof=0)),
        "min": _r(clean.min()),
        "max": _r(clean.max()),
        "percentiles": {f"p{p}": _r(quantiles[p / 100]) for p in PERCENTILES},
    }


def _categorical_stats(s: pd.Series) -> dict[str, Any]:
    """Top-K most common values + frequencies. Tolerates columns containing
    unhashable values (lists, numpy arrays) by stringifying before counting.

    For "sequence-like" columns (lists of geohashes, ping series, etc.) the
    stringified form would be a unique blob per row — not useful — so we
    flag those with kind=sequence-like and skip value counts.
    """
    clean = s.dropna()
    total = int(clean.size)
    if total == 0:
        return {"count": 0, "n_distinct_in_sample": 0, "top_k": {}, "top_k_fractions": {}}

    # Detect sequence-like values (lists, ndarrays) — stringifying these
    # gives one giant unique value per row, which is useless for top-K.
    sample = clean.iloc[0]
    if isinstance(sample, (list, tuple)) or (hasattr(sample, "__len__") and hasattr(sample, "shape")):
        # Pull example lengths instead of values
        try:
            lengths = clean.apply(lambda x: len(x) if hasattr(x, "__len__") else 0)
            return {
                "count": total,
                "kind_note": "sequence-like (lists / arrays) — value-counts skipped",
                "len_min": int(lengths.min()),
                "len_max": int(lengths.max()),
                "len_mean": _r(float(lengths.mean())),
            }
        except Exception:
            return {"count": total, "kind_note": "sequence-like (uncountable)"}

    # Normal hashable case — stringify defensively in case of mixed types
    str_series = clean.astype(str)
    counts = str_series.value_counts().head(TOP_K)
    return {
        "count": total,
        "n_distinct_in_sample": int(str_series.nunique()),
        "top_k": {str(v): int(c) for v, c in counts.items()},
        "top_k_fractions": {str(v): _r(c / max(total, 1)) for v, c in counts.items()},
    }


def _r(x: Any) -> Any:
    """Round numerics to 4 places; leave the rest alone."""
    if x is None:
        return None
    if isinstance(x, float):
        if math.isnan(x) or math.isinf(x):
            return None
        return round(x, 4)
    return x


def _correlate_with_target(df: pd.DataFrame, target: str) -> dict[str, float]:
    """Pearson correlation of every numeric column with `target`. Skips columns
    with insufficient overlap or zero variance."""
    if target not in df.columns or not _is_numeric(df[target]):
        return {}
    t = df[target]
    out: dict[str, float] = {}
    for col in df.columns:
        if col == target or not _is_numeric(df[col]):
            continue
        try:
            corr = df[col].corr(t)
        except Exception:
            continue
        if corr is None or (isinstance(corr, float) and math.isnan(corr)):
            continue
        out[col] = _r(float(corr))
    return out


def main() -> None:
    load_dotenv(override=True)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bucket", required=True, help="S3 bucket name")
    p.add_argument("--prefix", required=True, help="S3 prefix containing parquet files")
    p.add_argument("--service", required=True, help="Logical service name (e.g. 'tl-eta')")
    p.add_argument("--model-version", required=True, help="Free-form version tag for this baseline")
    p.add_argument("--model-name", default="", help="Optional human-friendly model name")
    p.add_argument("--target", default=None,
                   help="Target column to compute correlations against (numeric column). Skip if unset.")
    p.add_argument("--sample-files", type=int, default=5,
                   help="Number of parquet files to sample (default 5 = ~10MB). Use -1 for all.")
    p.add_argument("--out-dir", default="baselines", help="Local output directory (default ./baselines)")
    p.add_argument("--region", default=None, help="Override AWS_REGION env")
    args = p.parse_args()

    region = args.region or os.environ.get("AWS_REGION", "us-east-1")
    s3 = boto3.client("s3", region_name=region)

    print(f"🔍 Listing s3://{args.bucket}/{args.prefix}")
    limit = None if args.sample_files == -1 else args.sample_files
    keys = _list_parquet_keys(s3, args.bucket, args.prefix, limit=limit)
    if not keys:
        raise SystemExit(f"No .parquet files under {args.prefix!r}")
    print(f"   Found {len(keys)} parquet files {'(sampled)' if limit else '(all)'}")

    print(f"📥 Downloading + concatenating…")
    frames: list[pd.DataFrame] = []
    for i, key in enumerate(keys, 1):
        df = _download_parquet(s3, args.bucket, key)
        frames.append(df)
        print(f"   {i}/{len(keys)}  {key.split('/')[-1]:55s} → {len(df):,} rows")
    df = pd.concat(frames, ignore_index=True)
    print(f"\n📊 Combined: {len(df):,} rows × {len(df.columns)} columns "
          f"({df.memory_usage(deep=True).sum() / 1024 / 1024:.1f} MB in memory)")

    # ── per-column stats ──
    print(f"\n🧮 Computing per-column stats (numerics: percentiles; categoricals: top-{TOP_K})…")
    feature_stats: dict[str, dict] = {}
    for col in df.columns:
        s = df[col]
        nulls = int(s.isna().sum())
        base = {
            "dtype": str(s.dtype),
            "n_total": int(len(s)),
            "n_nulls": nulls,
            "null_frac": _r(nulls / max(len(s), 1)),
        }
        if _is_numeric(s):
            base["kind"] = "numeric"
            base.update(_numeric_stats(s))
        elif pd.api.types.is_datetime64_any_dtype(s):
            base["kind"] = "datetime"
            clean = s.dropna()
            if not clean.empty:
                base["min"] = clean.min().isoformat()
                base["max"] = clean.max().isoformat()
        else:
            base["kind"] = "categorical"
            base.update(_categorical_stats(s))
        feature_stats[col] = base

    # ── correlations with target ──
    correlations: dict[str, float] = {}
    if args.target:
        print(f"📐 Computing correlations vs target {args.target!r}…")
        correlations = _correlate_with_target(df, args.target)
        # Sort: most positive first, then most negative
        correlations = dict(
            sorted(correlations.items(), key=lambda kv: -abs(kv[1]))
        )
        print(f"   Top 5 by |correlation|:")
        for col, c in list(correlations.items())[:5]:
            print(f"     {c:+.3f}  {col}")

    # ── assemble + write ──
    baseline = {
        "service": args.service,
        "model_name": args.model_name or args.service,
        "model_version": args.model_version,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "bucket": args.bucket,
            "prefix": args.prefix,
            "n_files_sampled": len(keys),
            "n_rows": int(len(df)),
            "n_columns": int(len(df.columns)),
        },
        "metrics": {},   # filled in by a separate step (we don't have MAE labels here)
        "target_column": args.target,
        "feature_stats": feature_stats,
        "correlations_with_target": correlations,
        # The simpler `attribute_distributions` shape that Sentinel's PSI
        # path consumes directly. Modal values from the top_k_fractions.
        "attribute_distributions": {
            col: {top: frac for top, frac in stats["top_k_fractions"].items()}
            for col, stats in feature_stats.items()
            if stats["kind"] == "categorical" and "top_k_fractions" in stats
        },
    }

    out_dir = Path(args.out_dir) / args.service
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.model_version}.json"
    out_path.write_text(json.dumps(baseline, indent=2, default=str))
    size_kb = out_path.stat().st_size / 1024
    print(f"\n✅ Wrote {out_path}  ({size_kb:.1f} KB)")
    print(f"   {len(feature_stats)} features, "
          f"{sum(1 for f in feature_stats.values() if f['kind'] == 'numeric')} numeric, "
          f"{sum(1 for f in feature_stats.values() if f['kind'] == 'categorical')} categorical, "
          f"{sum(1 for f in feature_stats.values() if f['kind'] == 'datetime')} datetime")


if __name__ == "__main__":
    main()
