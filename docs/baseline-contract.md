# Baseline contract — how a model's training baseline reaches Airen

Airen compares production behaviour against a **baseline**. The strongest
baseline is the **training baseline**: the feature distributions and error the
model was actually built on. The cleanest way to keep it fresh is for the
**training pipeline to emit it at train time** — every time a model is trained,
it already has the training data in hand, so it writes one baseline file keyed
by model version. Airen then just consumes the latest one for the deployed
version, and the version-drift guardrail (below) guarantees the two never
diverge silently.

## What the trainer writes

One JSON per model version, plus a `latest.json` pointer:

```
s3://<bucket>/baselines/<service>/<model_version>.json
s3://<bucket>/baselines/<service>/latest.json      # copy of the newest
```

Schema (matches `airen.adapters.s3_base.TrainingBaseline`):

```json
{
  "service": "tl-eta",
  "model_name": "tl_eta_pg_north_america",
  "model_version": "pg-na-2026q2",          // MUST match what production emits
  "trained_at": "2026-05-31T12:00:00Z",
  "source": { "bucket": "...", "prefix": "...", "n_rows": 39928, "n_columns": 349 },
  "metrics": { "mae_minutes": 118.4 },        // training-time headline metric
  "target_column": "shippers_expected_journey_minutes",
  "feature_stats": {
    "api_fetch_limit": { "kind": "numeric", "mean": 184, "std": 3,
                          "percentiles": {"p5": 180, "p50": 184, "p95": 190} },
    "shipper":         { "kind": "categorical", "top_k": {"Fritolay": 0.5, "...": 0.3} }
  }
}
```

`model_version` is the contract key: it must equal the `model_version` the model
emits on each prediction (Phoenix span attribute / Kafka field). That's what
lets Airen tell "the baseline matches the live model" from "the baseline is
stale".

## Until the trainer does it — the manual bridge

`python -m scripts.extract_baseline --bucket ... --prefix ... --service tl-eta
--model-version <v>` reads the training parquet and writes the same JSON
locally (upload to S3 when ready). Or, with no training-data access, use the
**operational** baseline: `python -m airen.run_calibrate <service>` learns norms
from live telemetry and stamps them with the live `model_version`.

## Baseline resolution order (airen/tools/health.py)

1. explicit kwarg
2. **S3 training baseline** (`baselines/<service>/<version>.json`) — preferred
3. **calibration** (`baselines/<service>/calibration.json`) — operational
4. Redshift 7-day rolling MAE
5. yaml `sentinel.baseline_mae_minutes` / `attribute_baselines`
6. built-in default

## Version-drift guardrail

On every cycle Sentinel reads the live `model_version` and compares it to the
baseline's. If they differ it emits a `baseline_stale` signal:

> Production model_version='pg-na-2026q2' but the baseline was set for
> 'pg-na-2026q1'. A new model is live — recalibrate.

This is the safety net that stops Airen from ever silently comparing a new
model against an old model's baseline. (Auto-refresh on version change is a
planned next step; today it warns and points you at `run_calibrate`.)
