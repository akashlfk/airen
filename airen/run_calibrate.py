"""Calibration CLI — learn a service's baselines from live telemetry.

Run this after onboarding, once your service is emitting prediction spans to
Phoenix. It validates that the onboarded schema matches the real spans, learns
the current norms, and (with your OK) writes them as Sentinel's baselines.

Usage:
    python -m airen.run_calibrate <service>
    python -m airen.run_calibrate <service> --window 720      # minutes (default 1440 = 24h)
    python -m airen.run_calibrate <service> --no-write          # report only, write nothing
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from airen.config import list_available_services, load_service_config
from airen.onboarding.calibrate import calibrate


def _flag_int(argv: list[str], flag: str, default: int) -> int:
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv) and argv[i + 1].lstrip("-").isdigit():
            return int(argv[i + 1])
    return default


def main() -> None:
    argv = sys.argv[1:]
    positional = [a for a in argv if not a.startswith("--") and not a.isdigit()]
    if not positional:
        print("Usage: python -m airen.run_calibrate <service> [--window MINUTES] [--no-write]")
        print(f"Available services: {list_available_services()}")
        sys.exit(1)

    window = _flag_int(argv, "--window", 1440)
    write_back = "--no-write" not in argv

    try:
        config = load_service_config(positional[0])
    except FileNotFoundError as e:
        print(f"❌ {e}")
        sys.exit(1)

    calibrate(config, window_minutes=window, interactive=True, write_back=write_back)


if __name__ == "__main__":
    main()
