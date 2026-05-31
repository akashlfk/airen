"""Run the Redshift → Phoenix replay.

Modes:
    --mode mock  (default)  — pulls synthetic rows from MockRedshiftAdapter
    --mode real             — pulls from real Redshift (FK VM required)

Examples:
    # Local dev — emit 100 synthetic rows
    python -m airen.run_redshift_replay --mode mock --max 100

    # FK VM — emit last 60 min of real predictions
    python -m airen.run_redshift_replay --mode real --since 60
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from airen.instrumentation import setup_tracing

PROJECT_NAME = os.environ.get("PHOENIX_PROJECT_NAME_TAP", "tl-eta-prediction")
tracer_provider = setup_tracing()
tracer = tracer_provider.get_tracer("mlre.ingestion.redshift_replay")

from airen.adapters.redshift import get_redshift_adapter  # noqa: E402
from airen.config import load_service_config  # noqa: E402
from airen.ingestion.redshift_replay import RedshiftReplay  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["mock", "real"], default="mock")
    parser.add_argument("--service", default="tl-eta", help="service config to resolve table name")
    parser.add_argument("--max", type=int, default=200, help="row limit")
    parser.add_argument("--since", type=int, default=60, help="lookback in minutes")
    parser.add_argument("--table", default=None, help="explicit table override")
    args = parser.parse_args()

    # Resolve table from CLI > env > airen.yaml
    table = (
        args.table
        or os.environ.get("REDSHIFT_PREDICTIONS_TABLE")
        or _load_table_from_config(args.service)
    )
    if not table:
        print(
            "❌ No predictions table configured. Set REDSHIFT_PREDICTIONS_TABLE in .env, "
            "or fill `redshift.table` in services/<service>/airen.yaml."
        )
        sys.exit(1)

    print(f"Redshift REPLAY  ·  mode={args.mode}  ·  table={table}  ·  since={args.since}m")
    adapter = get_redshift_adapter(mode=args.mode)
    try:
        replay = RedshiftReplay(adapter=adapter, predictions_table=table, tracer=tracer)
        replay.replay(max_rows=args.max, since_minutes=args.since)
    finally:
        adapter.close()
        tracer_provider.force_flush()


def _load_table_from_config(service_name: str) -> str | None:
    try:
        cfg = load_service_config(service_name)
        if cfg.redshift and cfg.redshift.table:
            return cfg.redshift.table
    except Exception:
        return None
    return None


if __name__ == "__main__":
    main()
