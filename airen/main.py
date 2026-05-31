"""Airen entrypoint — run a single agent turn from the CLI.

Order of operations matters here:
  1. load .env  (so GOOGLE_API_KEY and PHOENIX_* are set)
  2. setup_tracing()  (monkey-patches ADK / google-genai)
  3. import the agent (now ADK is wrapped — every call becomes a span)
  4. run a turn

Usage:
    python -m airen.main "How many TL ETA spans do we have?"
    python -m airen.main   # uses a default question
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv

# Step 1 — load env BEFORE anything else
# override=True makes .env authoritative even if the shell already exports
# a conflicting value (e.g., a stale token left over from a previous session).
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

# Step 2 — instrument BEFORE importing ADK
from airen.instrumentation import setup_tracing

setup_tracing()

# Step 3 — now safe to import ADK & the agent
from google.adk.runners import InMemoryRunner
from google.genai import types

from airen.agents.hello import root_agent

APP_NAME = "airen"
USER_ID = "akash"


async def run_turn(user_text: str) -> None:
    session_id = secrets.token_hex(8)
    runner = InMemoryRunner(agent=root_agent, app_name=APP_NAME)
    await runner.session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id
    )

    print(f"\n👤 user: {user_text}\n🤖 airen: ", end="", flush=True)

    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=user_text)]),
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    print(part.text, end="", flush=True)
    print()


def main() -> None:
    msg = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "How many TL ETA prediction spans do we have in Phoenix right now?"
    )
    asyncio.run(run_turn(msg))


if __name__ == "__main__":
    main()
