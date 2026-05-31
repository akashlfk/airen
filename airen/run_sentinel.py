"""Run Sentinel once against a Phoenix project.

Same launch pattern as airen.main: env → tracing → import agent → run turn.

Usage:
    python -m airen.run_sentinel                          # default: tl-eta-prediction, 60 min
    python -m airen.run_sentinel tl-eta-prediction 120    # custom project + window
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from airen.instrumentation import setup_tracing

setup_tracing()

from google.adk.runners import InMemoryRunner
from google.genai import types

from airen.agents.sentinel import root_agent
from airen.llm_factory import extract_json
from airen.schemas import SentinelVerdict

APP_NAME = "airen"
USER_ID = "akash"


async def run_sentinel(project_name: str, lookback_minutes: int) -> None:
    session_id = secrets.token_hex(8)
    runner = InMemoryRunner(agent=root_agent, app_name=APP_NAME)
    await runner.session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id
    )

    user_msg = (
        f"Check the health of Phoenix project '{project_name}' "
        f"over the last {lookback_minutes} minutes. Return the SentinelVerdict."
    )
    print(f"\n👤 trigger: {user_msg}\n🛡  sentinel: working...\n")

    final_text: str = ""
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=user_msg)]),
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    final_text += part.text

    # Parse the structured verdict
    try:
        verdict = SentinelVerdict.model_validate_json(extract_json(final_text))
        _print_verdict(verdict)
    except Exception as e:
        print(f"⚠️  Failed to parse verdict as SentinelVerdict: {e}")
        print(f"Raw output:\n{final_text}")


def _print_verdict(v: SentinelVerdict) -> None:
    status_emoji = {"HEALTHY": "🟢", "WARNING": "🟡", "CRITICAL": "🔴"}[v.status.value]
    print(f"{status_emoji}  STATUS: {v.status.value}   (severity {v.severity_score:.2f})")
    print(f"    project:  {v.project_name}")
    print(f"    spans:    {v.n_spans_analyzed}")
    print(f"    action:   {v.recommended_action.value}")
    print(f"\n    {v.summary}")
    if v.anomalies:
        print(f"\n    Anomalies ({len(v.anomalies)}):")
        for a in v.anomalies:
            print(
                f"      • {a.metric} @ {a.segment}: "
                f"{a.current_value:.1f} (baseline {a.baseline_value:.1f}, "
                f"ratio {a.ratio:.2f}x)"
            )
            print(f"        {a.description}")
    print()
    print("Raw JSON:")
    print(json.dumps(v.model_dump(mode="json"), indent=2))


def main() -> None:
    project = sys.argv[1] if len(sys.argv) > 1 else "tl-eta-prediction"
    lookback = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    asyncio.run(run_sentinel(project, lookback))


if __name__ == "__main__":
    main()
