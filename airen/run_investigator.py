"""Run Investigator once against a Sentinel verdict.

Usage:
    # Default — runs Sentinel first to get a verdict, then Investigator against it
    python -m airen.run_investigator

    # Explicit project + lookback + repo
    python -m airen.run_investigator tl-eta-prediction 2880 cloudqwest/dynamic_eta_prediction

Order of operations matters: load .env → setup tracing → import ADK.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from airen.instrumentation import setup_tracing

setup_tracing()

from google.adk.runners import InMemoryRunner
from google.genai import types

from airen.agents.investigator import root_agent as investigator_agent
from airen.agents.sentinel import root_agent as sentinel_agent
from airen.llm_factory import extract_json
from airen.schemas import InvestigatorVerdict, SentinelVerdict

APP_NAME = "airen"
USER_ID = "akash"


async def _run_agent_once(agent, user_msg: str) -> str:
    session_id = secrets.token_hex(8)
    runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
    await runner.session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id
    )
    final_text = ""
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=user_msg)]),
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    final_text += part.text
    return final_text


async def _run_agent(agent, user_msg: str, max_retries: int = 3) -> str:
    """Run an agent with retry-with-backoff for transient Gemini API errors.

    Free-tier Gemini hits 429 (rate limit) and 503 (overload) often during
    multi-tool reasoning chains. We honour Gemini's `retryDelay` hint when
    present, otherwise fall back to exponential backoff.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await _run_agent_once(agent, user_msg)
        except Exception as e:
            last_exc = e
            msg = str(e)
            is_429 = "RESOURCE_EXHAUSTED" in msg or "429" in msg
            is_503 = "UNAVAILABLE" in msg or "503" in msg
            if not (is_429 or is_503) or attempt == max_retries:
                raise
            # Parse Gemini's hint "Please retry in 59.9s." or "retryDelay: 37s"
            hint = re.search(r"retry in (\d+(?:\.\d+)?)s", msg) or re.search(
                r"retryDelay['\":\s]+(\d+)s", msg
            )
            delay = float(hint.group(1)) + 2 if hint else 5 * (2**attempt)
            print(
                f"  ⏳ Gemini transient ({'429' if is_429 else '503'}). "
                f"Backing off {delay:.0f}s (attempt {attempt + 1}/{max_retries})…"
            )
            await asyncio.sleep(delay)
    # Unreachable, but keeps type-checkers happy
    raise last_exc if last_exc else RuntimeError("retry loop exited unexpectedly")


async def run_pipeline(project_name: str, lookback_min: int, repo: str) -> None:
    # Stage 1 — Sentinel
    print(f"\n🛡  Sentinel — checking {project_name} ({lookback_min} min window)…\n")
    sentinel_msg = (
        f"Check the health of Phoenix project '{project_name}' over the last "
        f"{lookback_min} minutes. Return the SentinelVerdict."
    )
    sentinel_raw = await _run_agent(sentinel_agent, sentinel_msg)
    verdict = SentinelVerdict.model_validate_json(extract_json(sentinel_raw))
    _print_sentinel(verdict)

    if verdict.status.value == "HEALTHY":
        print("\nNo anomalies → no investigation needed. Done.\n")
        return

    # Per-minute rate limit on free-tier Gemini is 5 RPM. Sentinel just used
    # ~3 calls; we cool down so Investigator gets a fresh 5-call budget.
    cooldown = int(__import__("os").environ.get("PIPELINE_COOLDOWN_SEC", "65"))
    print(f"\n⏳ Cooling down {cooldown}s to respect Gemini per-minute quota…")
    await asyncio.sleep(cooldown)

    # Stage 2 — Investigator
    print(f"\n🔎 Investigator — diagnosing against repo {repo}…\n")
    investigator_msg = (
        f"Investigate the following SentinelVerdict against GitHub repo "
        f"`{repo}`. The Phoenix project is `{project_name}`. Find the root "
        f"cause and return an InvestigatorVerdict.\n\n"
        f"SentinelVerdict JSON:\n{verdict.model_dump_json(indent=2)}"
    )
    investigator_raw = await _run_agent(investigator_agent, investigator_msg)
    try:
        result = InvestigatorVerdict.model_validate_json(extract_json(investigator_raw))
        _print_investigator(result)
    except Exception as e:
        print(f"⚠️  Failed to parse InvestigatorVerdict: {e}")
        print(f"\nRaw output:\n{investigator_raw}")


def _print_sentinel(v: SentinelVerdict) -> None:
    status_emoji = {"HEALTHY": "🟢", "WARNING": "🟡", "CRITICAL": "🔴"}[v.status.value]
    print(f"  {status_emoji} {v.status.value}  (severity {v.severity_score:.2f})")
    print(f"  spans: {v.n_spans_analyzed}  action: {v.recommended_action.value}")
    print(f"  → {v.summary}")
    if v.anomalies:
        for a in v.anomalies:
            print(f"    • {a.metric} @ {a.segment}: {a.current_value:.1f} (ratio {a.ratio:.2f}x)")


def _print_investigator(v: InvestigatorVerdict) -> None:
    print(f"  🔎 Root-cause hypothesis  (confidence {v.confidence:.2f})")
    print(f"\n  {v.root_cause_hypothesis}\n")

    if v.code_evidence:
        print("  CODE EVIDENCE")
        for ev in v.code_evidence:
            sha = ev.commit_sha[:8] if ev.commit_sha else "?"
            author = ev.commit_author or "?"
            print(f"    • commit {sha} by {author}")
            if ev.commit_date:
                print(f"      date:    {ev.commit_date}")
            if ev.file_path:
                print(f"      file:    {ev.file_path}")
            if ev.matched_pattern:
                print(f"      matched: {ev.matched_pattern!r}")
            if ev.pr_url:
                print(f"      PR:      {ev.pr_url}")
            if ev.diff_snippet:
                print("      diff:")
                for line in ev.diff_snippet.splitlines()[:6]:
                    print(f"        {line}")

    if v.data_evidence:
        print("\n  DATA EVIDENCE")
        for d in v.data_evidence:
            print(f"    • {d}")

    print(f"\n  RECOMMENDED FIX")
    print(f"    {v.recommended_fix}")

    if v.further_investigation_needed:
        print(f"\n  STILL UNKNOWN")
        for q in v.further_investigation_needed:
            print(f"    • {q}")
    print()
    print("Raw InvestigatorVerdict JSON:")
    print(json.dumps(v.model_dump(mode="json"), indent=2))


def main() -> None:
    project = sys.argv[1] if len(sys.argv) > 1 else "tl-eta-prediction"
    lookback = int(sys.argv[2]) if len(sys.argv) > 2 else 2880
    repo = sys.argv[3] if len(sys.argv) > 3 else "cloudqwest/dynamic_eta_prediction"
    asyncio.run(run_pipeline(project, lookback, repo))


if __name__ == "__main__":
    main()
