"""Run one full Airen Orchestrator cycle — or many on a loop.

Canonical entrypoint, driven by the per-service `airen.yaml` config.

Usage:
    python -m airen.run_orchestrator <service>          # by service name (required)
    python -m airen.run_orchestrator path/to/airen.yaml # explicit file
    python -m airen.run_orchestrator --list             # show onboarded services

    # Autonomous mode — runs every N seconds until Ctrl+C
    python -m airen.run_orchestrator tl-eta --loop 300   # every 5 minutes

    # Actually open the remediation PR (default = plan-only)
    python -m airen.run_orchestrator tl-eta --execute

    # Combine — autonomous monitoring with one-line override of cooldowns
    AIREN_LLM_MODE=mock python -m airen.run_orchestrator tl-eta --loop 30

The flow (Sentinel → Investigator → RCA Writer → Slack → Validator) and
every state transition are recorded as IncidentEvents on the IncidentRun,
persisted to logs/<run_id>.json, and surfaced in the web UI.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from airen.instrumentation import setup_tracing

setup_tracing()

from airen.agents.orchestrator import AirenOrchestrator
from airen.config import AirenServiceConfig, list_available_services, load_service_config
from airen.mocks import is_mock_mode
from airen.schemas import IncidentRun


async def main_async(config: AirenServiceConfig, execute: bool = False) -> None:
    mode_tag = "🧪 MOCK" if is_mock_mode() else "🔴 LIVE"
    exec_tag = "  · ⚠️  --execute" if execute else ""
    print()
    print("═" * 78)
    print(
        f"  AIREN ORCHESTRATOR  ·  {mode_tag}  ·  service={config.service.name}  "
        f"phoenix={config.phoenix.project_name}{exec_tag}"
    )
    if config.github:
        print(f"  github={config.github.repo}")
    print("═" * 78)
    print()
    orch = AirenOrchestrator(config=config, execute_remediation=execute)
    print(f"  run_id: {orch.run.run_id}")
    print(f"  state-trace:")
    print()
    run = await orch.run_cycle()

    print()
    print("═" * 78)
    print(f"  FINISHED  ·  {run.final_state.value}  ·  events: {len(run.events)}")
    print("═" * 78)

    if run.incident_report:
        print()
        print(f"INCIDENT REPORT — {run.incident_report.title}")
        print(f"  TL;DR  {run.incident_report.tldr}")

    if run.slack_permalink:
        print()
        print(f"Slack: {run.slack_permalink}")

    if run.error:
        print()
        print(f"❌ Error: {run.error}")

    # Persist the full run as JSON for downstream consumption
    out_path = Path(__file__).resolve().parent.parent / "logs" / f"{run.run_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(run.model_dump(mode="json"), indent=2))
    print()
    print(f"Full IncidentRun JSON  →  {out_path}")
    print()


async def loop_main(config: AirenServiceConfig, execute: bool, interval_sec: int) -> None:
    """Autonomous monitoring mode — re-runs the cycle every `interval_sec` seconds."""
    print(
        f"\n🔁 LOOP mode — service={config.service.name}  interval={interval_sec}s\n"
        f"   Ctrl+C to stop.\n"
    )
    iteration = 0
    try:
        while True:
            iteration += 1
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"\n{'━' * 78}")
            print(f"  ITERATION {iteration}  ·  {ts} UTC")
            print(f"{'━' * 78}\n")
            await main_async(config, execute=execute)
            print(f"\n💤 Sleeping {interval_sec}s before next cycle... (Ctrl+C to stop)")
            await asyncio.sleep(interval_sec)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(f"\n\n✓ Loop stopped after {iteration} iteration(s).\n")


def _parse_loop_seconds(argv: list[str]) -> int | None:
    """Pull --loop N out of argv; return N or None. Bare --loop defaults to 300s."""
    if "--loop" not in argv:
        return None
    idx = argv.index("--loop")
    if idx + 1 < len(argv) and argv[idx + 1].isdigit():
        return int(argv[idx + 1])
    return 300  # default: 5 min


def main() -> None:
    # Support --list to discover services
    if "--list" in sys.argv:
        services = list_available_services()
        if not services:
            print("No services found in services/ directory.")
        else:
            print("Available services:")
            for s in services:
                print(f"  • {s}")
            print(f"\nRun one with:  python -m airen.run_orchestrator <name>")
            print(f"Or loop:       python -m airen.run_orchestrator <name> --loop 300")
        return

    # Flags
    execute = "--execute" in sys.argv
    loop_sec = _parse_loop_seconds(sys.argv)
    # Strip flags + their numeric values from positional args
    positional: list[str] = []
    skip_next = False
    for arg in sys.argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg == "--execute":
            continue
        if arg == "--loop":
            skip_next = True  # next arg is the interval int (if present)
            continue
        if arg.isdigit() and "--loop" in sys.argv[: sys.argv.index(arg)] and sys.argv[sys.argv.index(arg) - 1] == "--loop":
            continue
        positional.append(arg)

    if not positional:
        print("❌ Specify a service to run, e.g.:  python -m airen.run_orchestrator <service>")
        print(f"   Onboarded services: {list_available_services() or '(none yet — run python -m airen.run_onboard)'}")
        sys.exit(1)
    name = positional[0]
    try:
        config = load_service_config(name)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        sys.exit(1)

    # Readiness check: warn (don't block) about any enabled source whose
    # secret/connection detail is missing. Mode-aware — mock runs stay quiet.
    try:
        from airen.onboarding.preflight import print_runtime_warnings

        print_runtime_warnings(config)
    except Exception:
        pass

    if loop_sec is not None:
        asyncio.run(loop_main(config, execute=execute, interval_sec=loop_sec))
    else:
        asyncio.run(main_async(config, execute=execute))


if __name__ == "__main__":
    main()
