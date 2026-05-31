"""Full Airen pipeline — Sentinel → Investigator → RCA Writer.

This is the end-to-end demo flow:
    user trigger → Sentinel → (cool down) → Investigator → RCA Writer → printed report

Each agent's output is validated as Pydantic, then passed to the next as
structured input. We never lose type safety, even across agents.

Usage:
    python -m airen.run_pipeline
    python -m airen.run_pipeline tl-eta-prediction 2880 cloudqwest/dynamic_eta_prediction
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from airen.instrumentation import setup_tracing

setup_tracing()

from google.adk.runners import InMemoryRunner
from google.genai import types

from airen.agents.investigator import root_agent as investigator_agent
from airen.agents.rca_writer import root_agent as rca_writer_agent
from airen.agents.sentinel import root_agent as sentinel_agent
from airen.llm_factory import extract_json
from airen.mocks import (
    is_mock_mode,
    mock_incident_report,
    mock_investigator_verdict,
    mock_sentinel_verdict,
)
from airen.schemas import IncidentReport, InvestigatorVerdict, SentinelVerdict

# Slack is optional — we only post if a token is configured and --no-slack
# wasn't passed. Import lazily so the pipeline still works without slack-sdk.
def _maybe_post_to_slack(report: IncidentReport, incident_id: str, skip: bool) -> None:
    if skip:
        return
    if not os.environ.get("SLACK_BOT_TOKEN", "").strip():
        return  # silently skip when not configured
    try:
        from airen.adapters.slack import post_incident_report
        print("\n📣 Posting to Slack...")
        result = post_incident_report(report, incident_id=incident_id)
        if result.get("ok"):
            link = result.get("permalink") or f"(ts={result.get('ts')})"
            print(f"  ✓ posted: {link}")
        else:
            print(f"  ✗ slack post failed: {result.get('error')} — {result.get('details', '')}")
    except Exception as e:
        print(f"  ✗ slack post threw: {type(e).__name__}: {e}")

APP_NAME = "airen"
USER_ID = "akash"


async def _run_agent_once(agent, user_msg: str) -> str:
    session_id = secrets.token_hex(8)
    runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
    await runner.session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id
    )
    final_text = ""
    event_count = 0
    saw_function_call = False
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=user_msg)]),
    ):
        event_count += 1
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    final_text += part.text
                if getattr(part, "function_call", None):
                    saw_function_call = True

    if not final_text.strip():
        raise RuntimeError(
            f"Agent {agent.name!r} returned no text output after {event_count} events "
            f"(function_calls_seen={saw_function_call}). "
            "Likely silent Gemini quota / content-filter issue — check ai.dev/rate-limit."
        )
    return final_text


async def _run_agent(agent, user_msg: str, max_retries: int = 3) -> str:
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
            hint = re.search(r"retry in (\d+(?:\.\d+)?)s", msg) or re.search(
                r"retryDelay['\":\s]+(\d+)s", msg
            )
            delay = float(hint.group(1)) + 2 if hint else 5 * (2**attempt)
            print(
                f"  ⏳ Gemini transient ({'429' if is_429 else '503'}). "
                f"Backing off {delay:.0f}s (attempt {attempt + 1}/{max_retries})…"
            )
            await asyncio.sleep(delay)
    raise last_exc if last_exc else RuntimeError("retry loop exited")


def _confidence_band(confidence: float) -> str:
    if confidence >= 0.85:
        return "high"
    if confidence >= 0.6:
        return "medium"
    return "low"


async def run_pipeline(project_name: str, lookback_min: int, repo: str, skip_slack: bool = False) -> None:
    mock = is_mock_mode()
    if mock:
        print(f"\n🧪 AIREN_LLM_MODE=mock — using canned agent outputs (zero Gemini calls)\n")

    # ── Stage 1: Sentinel ─────────────────────────────────────────────
    print(f"🛡  Sentinel — checking {project_name} ({lookback_min} min window)…\n")
    if mock:
        sentinel = mock_sentinel_verdict(project_name=project_name)
    else:
        sentinel_raw = await _run_agent(
            sentinel_agent,
            f"Check the health of Phoenix project '{project_name}' over the last "
            f"{lookback_min} minutes. Return the SentinelVerdict.",
        )
        sentinel = SentinelVerdict.model_validate_json(extract_json(sentinel_raw))
    _print_sentinel(sentinel)
    if sentinel.status.value == "HEALTHY":
        print("\nNo anomalies → pipeline stops here. Done.\n")
        return

    # ── Cooldown (only needed for real LLM mode — free-tier per-minute) ─
    cooldown = int(os.environ.get("PIPELINE_COOLDOWN_SEC", "65"))
    if not mock:
        print(f"\n⏳ Cooldown {cooldown}s…")
        await asyncio.sleep(cooldown)

    # ── Stage 2: Investigator ─────────────────────────────────────────
    print(f"\n🔎 Investigator — diagnosing against repo {repo}…\n")
    if mock:
        investigator = mock_investigator_verdict(sentinel=sentinel)
    else:
        investigator_raw = await _run_agent(
            investigator_agent,
            f"Investigate the following SentinelVerdict against GitHub repo `{repo}`. "
            f"The Phoenix project is `{project_name}`. Find the root cause and return "
            f"an InvestigatorVerdict.\n\nSentinelVerdict JSON:\n"
            f"{sentinel.model_dump_json(indent=2)}",
        )
        investigator = InvestigatorVerdict.model_validate_json(extract_json(investigator_raw))
    _print_investigator(investigator)

    if not mock:
        print(f"\n⏳ Cooldown {cooldown}s…")
        await asyncio.sleep(cooldown)

    # ── Stage 3: RCA Writer ───────────────────────────────────────────
    print("\n📝 RCA Writer — drafting incident report…\n")
    if mock:
        report = mock_incident_report(sentinel=sentinel, investigator=investigator)
        _print_report(report)
        _maybe_post_to_slack(report, incident_id="INC-MOCK", skip=skip_slack)
        return

    band = _confidence_band(investigator.confidence)
    report_date = datetime.now(timezone.utc).isoformat()
    rca_raw = await _run_agent(
        rca_writer_agent,
        f"Draft an IncidentReport from the SentinelVerdict and InvestigatorVerdict "
        f"below.\n\n"
        f"confidence_band: {band}\n"
        f"report_date: {report_date}\n"
        f"service_name: {project_name}\n\n"
        f"SentinelVerdict JSON:\n{sentinel.model_dump_json(indent=2)}\n\n"
        f"InvestigatorVerdict JSON:\n{investigator.model_dump_json(indent=2)}",
    )
    try:
        report = IncidentReport.model_validate_json(extract_json(rca_raw))
        _print_report(report)
        # Use a real-looking incident id from the timestamp suffix
        incident_id = f"INC-{secrets.token_hex(3).upper()}"
        _maybe_post_to_slack(report, incident_id=incident_id, skip=skip_slack)
    except Exception as e:
        print(f"⚠️  Failed to parse IncidentReport: {e}")
        print(f"\nRaw RCA output:\n{rca_raw}")


# ──────────────────────────────────────────────────────────────────────
#  pretty-printers
# ──────────────────────────────────────────────────────────────────────
def _print_sentinel(v: SentinelVerdict) -> None:
    emoji = {"HEALTHY": "🟢", "WARNING": "🟡", "CRITICAL": "🔴"}[v.status.value]
    print(f"  {emoji} {v.status.value}  (severity {v.severity_score:.2f})")
    print(f"  spans: {v.n_spans_analyzed}  action: {v.recommended_action.value}")
    print(f"  → {v.summary}")
    for a in v.anomalies:
        print(f"    • {a.metric} @ {a.segment}: {a.current_value:.1f} (ratio {a.ratio:.2f}x)")


def _print_investigator(v: InvestigatorVerdict) -> None:
    print(f"  🔎 Hypothesis  (confidence {v.confidence:.2f})")
    print(f"     {v.root_cause_hypothesis}\n")
    if v.code_evidence:
        for ev in v.code_evidence[:1]:
            sha = ev.commit_sha[:8] if ev.commit_sha else "?"
            print(f"    • commit {sha} by {ev.commit_author or '?'}  ({ev.file_path or '?'})")
    if v.data_evidence:
        for d in v.data_evidence:
            print(f"    • {d}")


def _print_report(r: IncidentReport) -> None:
    sev_emoji = {"HEALTHY": "🟢", "WARNING": "🟡", "CRITICAL": "🔴"}[r.severity.value]
    print("═" * 78)
    print(f"{sev_emoji}  INCIDENT REPORT  ·  {r.severity.value}")
    print("═" * 78)
    print(f"\n## {r.title}\n")
    print(f"**TL;DR:**  {r.tldr}\n")
    print("### What happened")
    print(f"{r.what_happened}\n")
    print("### Root cause")
    print(f"{r.root_cause}\n")
    if r.evidence_bullets:
        print("### Evidence")
        for b in r.evidence_bullets:
            print(f"- {b}")
        print()
    print("### Recommended fix")
    print(f"{r.recommended_fix}\n")
    if r.open_questions:
        print("### Open questions")
        for q in r.open_questions:
            print(f"- {q}")
        print()
    print("═" * 78)
    print("\nRaw IncidentReport JSON:")
    print(json.dumps(r.model_dump(mode="json"), indent=2))


def main() -> None:
    # Lightweight arg parsing — keep CLI shape simple, optional --no-slack flag.
    args = [a for a in sys.argv[1:] if a != "--no-slack"]
    skip_slack = "--no-slack" in sys.argv[1:]
    project = args[0] if len(args) > 0 else "tl-eta-prediction"
    lookback = int(args[1]) if len(args) > 1 else 2880
    repo = args[2] if len(args) > 2 else "cloudqwest/dynamic_eta_prediction"
    asyncio.run(run_pipeline(project, lookback, repo, skip_slack=skip_slack))


if __name__ == "__main__":
    main()
