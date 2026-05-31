"""Airen web dashboard — FastAPI app.

Routes:
    GET  /                       home (fleet view + recent incidents)
    GET  /incident/{id}          single incident detail
    GET  /live                   LIVE agent feed — auto-refreshes on most recent run
    GET  /live/{run_id}          LIVE feed for a specific run
    GET  /events/{run_id}        SSE stream — server pushes IncidentEvent as it lands
    GET  /api/incidents          JSON list (for SSE / external dashboards)
    GET  /api/incident/{id}      JSON for one incident
    POST /api/run/{service}      trigger a new orchestrator cycle
    POST /slack/events           Slack Events API webhook — Concierge handles @mentions + DMs
    GET  /healthz                liveness probe

Renders narrative-first markdown — explicitly NOT a Phoenix-style trace explorer.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from airen.web import store

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
TEMPLATE_DIR = HERE / "templates"

md = MarkdownIt("commonmark", {"linkify": True, "html": False})

app = FastAPI(title="Airen — ML Reliability", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _md_filter(text: str) -> str:
    """Render markdown safely for embedding in templates."""
    return md.render(text or "")


def _humanize(dt) -> str:
    """Render a datetime OR ISO string as 'Ns ago'/'Nm ago'/'Nh ago'/'Nd ago'."""
    if dt is None:
        return "—"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except (ValueError, TypeError):
            return dt  # give up and show the raw string
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


templates.env.filters["md"] = _md_filter
templates.env.filters["humanize"] = _humanize


@app.on_event("startup")
def _startup() -> None:
    store.seed_with_mock_data()


# ───────────────────────────────────────────────────────────────────────
#  HTML routes
# ───────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    services = store.list_services()
    incidents = store.list_incidents()
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "services": services,
            "incidents": incidents,
        },
    )


@app.get("/incident/{incident_id}", response_class=HTMLResponse)
async def incident_detail(request: Request, incident_id: str) -> HTMLResponse:
    inc = store.get_incident(incident_id)
    if inc is None:
        raise HTTPException(status_code=404, detail=f"incident {incident_id} not found")
    if inc.report is None:
        # Render a minimal error page for incomplete/failed runs instead of 500ing
        return templates.TemplateResponse(
            "incident_incomplete.html",
            {"request": request, "inc": inc},
        )
    return templates.TemplateResponse(
        "incident.html",
        {"request": request, "inc": inc},
    )


# ───────────────────────────────────────────────────────────────────────
#  JSON routes (for the UI's JS + future external consumers)
# ───────────────────────────────────────────────────────────────────────
@app.get("/api/incidents")
async def api_list_incidents() -> JSONResponse:
    incs = store.list_incidents()
    return JSONResponse(
        [
            {
                "incident_id": i.incident_id,
                "service_name": i.service_name,
                "severity": i.report.severity.value if i.report else None,
                "title": i.report.title if i.report else "(incomplete run)",
                "tldr": i.report.tldr if i.report else None,
                "final_state": i.final_state.value,
                "created_at": i.created_at.isoformat(),
                "duration_seconds": i.duration_seconds,
                "slack_permalink": i.slack_permalink,
            }
            for i in incs
        ]
    )


@app.get("/api/incident/{incident_id}")
async def api_get_incident(incident_id: str) -> JSONResponse:
    inc = store.get_incident(incident_id)
    if inc is None:
        raise HTTPException(status_code=404, detail=f"incident {incident_id} not found")
    return JSONResponse(
        {
            "incident_id": inc.incident_id,
            "service_name": inc.service_name,
            "created_at": inc.created_at.isoformat(),
            "report": inc.report.model_dump(mode="json"),
            "sentinel": inc.sentinel.model_dump(mode="json") if inc.sentinel else None,
            "investigator": inc.investigator.model_dump(mode="json") if inc.investigator else None,
        }
    )


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


# ───────────────────────────────────────────────────────────────────────
#  Slack Events API — Concierge agent handles @-mentions and DMs
# ───────────────────────────────────────────────────────────────────────
@app.post("/slack/events", response_model=None)
async def slack_events(
    request: Request,
    x_slack_signature: str | None = Header(None, alias="X-Slack-Signature"),
    x_slack_request_timestamp: str | None = Header(None, alias="X-Slack-Request-Timestamp"),
):
    """Handle incoming Slack events (app_mention, message in DM, url_verification).

    Slack POSTs JSON here. We verify the signature (HMAC of body + timestamp),
    then either:
      - Echo back the challenge (initial URL verification)
      - Or hand the event to the Concierge agent and reply in the channel/thread
    """
    body_bytes = await request.body()

    # Signature verification (skipped only if SLACK_SIGNING_SECRET is unset and
    # AIREN_SLACK_SKIP_VERIFY=1 — useful for ngrok dev). Default = strict.
    from airen.adapters.slack import verify_slack_signature

    if os.environ.get("AIREN_SLACK_SKIP_VERIFY", "0").strip() != "1":
        ok = verify_slack_signature(
            body=body_bytes,
            timestamp=x_slack_request_timestamp or "",
            signature=x_slack_signature or "",
        )
        if not ok:
            raise HTTPException(status_code=401, detail="invalid Slack signature")

    try:
        payload = json.loads(body_bytes.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    # Slack's initial URL-verification handshake
    if payload.get("type") == "url_verification":
        return PlainTextResponse(payload.get("challenge", ""))

    # Process the event in a background task so we return 200 quickly
    # (Slack times out if we take >3s to respond).
    if payload.get("type") == "event_callback":
        event = payload.get("event", {})
        # Ignore bot's own messages to prevent loops
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return JSONResponse({"ok": True, "ignored": "bot message"})
        if event.get("type") in {"app_mention", "message"}:
            # For DMs (channel_type='im') and @-mentions, hand to Concierge
            asyncio.create_task(_handle_slack_event(event))
            return JSONResponse({"ok": True})

    return JSONResponse({"ok": True, "noop": True})


@app.post("/slack/actions", response_model=None)
async def slack_actions(
    request: Request,
    x_slack_signature: str | None = Header(None, alias="X-Slack-Signature"),
    x_slack_request_timestamp: str | None = Header(None, alias="X-Slack-Request-Timestamp"),
):
    """Handle Slack interactive Block Kit button clicks.

    Slack POSTs application/x-www-form-urlencoded with a `payload` field that
    holds JSON. Signature verification is identical to /slack/events. Each
    action carries an action_id ('approve_remediation' / 'reject_remediation')
    and a value (the incident_id).
    """
    body_bytes = await request.body()

    if os.environ.get("AIREN_SLACK_SKIP_VERIFY", "0").strip() != "1":
        from airen.adapters.slack import verify_slack_signature

        ok = verify_slack_signature(
            body=body_bytes,
            timestamp=x_slack_request_timestamp or "",
            signature=x_slack_signature or "",
        )
        if not ok:
            raise HTTPException(status_code=401, detail="invalid Slack signature")

    # Slack sends form-encoded with a `payload` field containing JSON
    from urllib.parse import parse_qs

    form = parse_qs(body_bytes.decode("utf-8"))
    payload_raw = (form.get("payload") or [""])[0]
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid action payload")

    actions = payload.get("actions") or []
    if not actions:
        return JSONResponse({"ok": True, "noop": True})
    action = actions[0]
    action_id = action.get("action_id", "")
    incident_id = action.get("value", "")
    user_name = (payload.get("user") or {}).get("name", "an engineer")
    response_url = payload.get("response_url")  # ephemeral reply channel

    asyncio.create_task(
        _handle_slack_action(
            action_id=action_id,
            incident_id=incident_id,
            user_name=user_name,
            response_url=response_url,
        )
    )
    return JSONResponse({"ok": True})


async def _handle_slack_action(
    action_id: str, incident_id: str, user_name: str, response_url: str | None
) -> None:
    """Record the approve/reject decision into logs/<run_id>.json and post a
    follow-up to the same channel via the response_url."""
    from datetime import datetime, timezone

    logs_dir = Path(__file__).resolve().parent.parent.parent / "logs"
    path = logs_dir / f"{incident_id}.json"
    if not path.exists():
        _post_response_url(response_url, f"⚠️ Couldn't find incident `{incident_id}`.")
        return
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        _post_response_url(response_url, f"⚠️ Couldn't load `{incident_id}`: {e}")
        return

    now = datetime.now(timezone.utc).isoformat()
    if action_id == "approve_remediation":
        data["approval"] = {"decision": "APPROVED", "by": user_name, "at": now}
        msg = f"✅ *Approved* by *{user_name}* — remediation plan accepted. " \
              f"Airen would now open the proposed PR (gated by `AIREN_ALLOW_REMEDIATION_EXECUTE=1` for safety)."
    elif action_id == "reject_remediation":
        data["approval"] = {"decision": "REJECTED", "by": user_name, "at": now}
        msg = f"❌ *Rejected* by *{user_name}* — incident escalated for manual review."
    else:
        return  # unknown action id

    try:
        path.write_text(json.dumps(data, indent=2))
    except Exception:
        pass

    _post_response_url(response_url, msg)


def _post_response_url(response_url: str | None, text: str) -> None:
    """Post a follow-up message to Slack via the per-message response_url."""
    if not response_url:
        return
    try:
        import requests

        requests.post(
            response_url,
            json={"text": text, "replace_original": False, "response_type": "in_channel"},
            timeout=10,
        )
    except Exception:
        pass


async def _handle_slack_event(event: dict) -> None:
    """Invoke the Concierge agent on a Slack message + post the reply.

    If the message is a *thread reply* on an Airen-posted incident, we look up
    the matching IncidentRun by parent message ts and inject a one-line context
    header so Concierge answers with the relevant incident in scope.
    """
    from airen.adapters.slack import post_text
    from airen.agents.concierge import root_agent as concierge_agent
    from google.adk.runners import InMemoryRunner
    from google.genai import types as genai_types
    import secrets

    text: str = event.get("text", "") or ""
    channel: str = event.get("channel", "")
    # `thread_ts` is only set when this message is INSIDE an existing thread.
    # `ts` is the message's own timestamp. For a thread reply, thread_ts points
    # to the parent message — which is what we use to look up the incident.
    parent_ts: str | None = event.get("thread_ts")
    reply_thread_ts: str | None = parent_ts or event.get("ts")

    if not text or not channel:
        return

    # Strip @-mentions before sending to the LLM
    cleaned = " ".join(part for part in text.split() if not part.startswith("<@"))

    # ── Thread → incident context lookup ──
    # If user replied in a thread that Airen started, prepend an incident-context
    # header so Concierge knows which incident is in scope without the user
    # having to paste the run_id.
    incident_ctx = _build_thread_incident_context(parent_ts) if parent_ts else ""
    if incident_ctx:
        cleaned = incident_ctx + "\n\n" + cleaned

    try:
        runner = InMemoryRunner(agent=concierge_agent, app_name="airen-concierge")
        sid = secrets.token_hex(8)
        await runner.session_service.create_session(
            app_name="airen-concierge", user_id="slack-user", session_id=sid
        )
        reply = ""
        async for ev in runner.run_async(
            user_id="slack-user",
            session_id=sid,
            new_message=genai_types.Content(role="user", parts=[genai_types.Part(text=cleaned)]),
        ):
            if ev.content and ev.content.parts:
                for part in ev.content.parts:
                    if getattr(part, "text", None):
                        reply += part.text
        if not reply.strip():
            reply = "_(I didn't have a response — try `help` for what I can do.)_"
        post_text(channel=channel, text=reply, thread_ts=reply_thread_ts)
    except Exception as e:
        post_text(
            channel=channel,
            text=f"_(Concierge errored: {type(e).__name__}: {e})_",
            thread_ts=reply_thread_ts,
        )


def _build_thread_incident_context(parent_ts: str) -> str:
    """Find the IncidentRun whose Slack post matches `parent_ts` and return a
    one-line context header for Concierge. Empty string if no match.
    """
    logs_dir = Path(__file__).resolve().parent.parent.parent / "logs"
    if not logs_dir.is_dir() or not parent_ts:
        return ""
    # Scan logs/*.json for a matching slack_message_ts
    for path in sorted(logs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if data.get("slack_message_ts") != parent_ts:
            continue
        # Match — build a compact context block
        run_id = data.get("run_id", path.stem)
        state = data.get("final_state", "?")
        svc = data.get("project_name", "?")
        report = data.get("incident_report") or {}
        title = report.get("title", "(no title yet)")
        severity = report.get("severity", "?")
        tldr = (report.get("tldr") or "")[:280]
        approval = data.get("approval")
        approval_line = ""
        if approval:
            approval_line = (
                f"  Human decision: {approval.get('decision')} by "
                f"{approval.get('by')} at {approval.get('at')}\n"
            )
        return (
            f"[Thread context — this reply belongs to incident {run_id} "
            f"on service {svc}, state={state}, severity={severity}]\n"
            f"  Title: {title}\n"
            f"  TL;DR: {tldr}\n"
            f"{approval_line}"
            f"Use this context to ground your answer. The user's question follows."
        )
    return ""


# ───────────────────────────────────────────────────────────────────────
#  Run trigger — kick off an orchestrator cycle from the UI
# ───────────────────────────────────────────────────────────────────────
@app.post("/api/run/{service_name}")
async def trigger_run(service_name: str) -> JSONResponse:
    """Spawn an Orchestrator cycle in the background. Returns the run_id immediately."""
    from airen.agents.orchestrator import AirenOrchestrator
    from airen.config import load_service_config

    try:
        config = load_service_config(service_name)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=404)

    orch = AirenOrchestrator(config=config)
    run_id = orch.run.run_id

    # Fire-and-forget — SSE consumers will tail logs/<run_id>.json as it fills
    asyncio.create_task(orch.run_cycle())

    return JSONResponse({"run_id": run_id, "service_name": service_name, "live_url": f"/live/{run_id}"})


# ───────────────────────────────────────────────────────────────────────
#  Live agent feed — SSE
# ───────────────────────────────────────────────────────────────────────
@app.get("/live", response_class=HTMLResponse)
async def live_default(request: Request) -> HTMLResponse:
    """Show the LIVE feed for the most recent run (or a waiting state if none)."""
    incs = store.list_incidents()
    target = incs[0].incident_id if incs else None
    return templates.TemplateResponse(
        "live.html",
        {"request": request, "target_run_id": target, "auto_follow": True},
    )


@app.get("/live/{run_id}", response_class=HTMLResponse)
async def live_specific(request: Request, run_id: str) -> HTMLResponse:
    """Show the LIVE feed for a specific run."""
    return templates.TemplateResponse(
        "live.html",
        {"request": request, "target_run_id": run_id, "auto_follow": False},
    )


@app.get("/events/{run_id}")
async def events(run_id: str) -> StreamingResponse:
    """SSE stream for `run_id`. Tails the logs/<run_id>.json file."""
    logs_dir = Path(__file__).resolve().parent.parent.parent / "logs"
    path = logs_dir / f"{run_id}.json"

    async def gen():
        last_count = 0
        last_mtime = 0.0
        stable_ticks = 0
        max_stable_ticks = 80  # ~20s of no changes after RESOLVED/FAILED → close
        while True:
            if path.exists():
                try:
                    mt = path.stat().st_mtime
                    if mt != last_mtime:
                        last_mtime = mt
                        data = json.loads(path.read_text())
                        events_list = data.get("events", [])
                        new_events = events_list[last_count:]
                        for e in new_events:
                            yield f"event: state\ndata: {json.dumps(e)}\n\n"
                        last_count = len(events_list)
                        # Send a final 'done' event when terminal state reached
                        if data.get("final_state") in ("RESOLVED", "FAILED"):
                            yield f"event: done\ndata: {json.dumps({'final_state': data['final_state'], 'run_id': run_id})}\n\n"
                            stable_ticks += 1
                            if stable_ticks > 4:
                                break
                except (json.JSONDecodeError, OSError):
                    # mid-write — try again next tick
                    pass
            else:
                # File doesn't exist yet — send a keepalive comment line
                yield ": waiting\n\n"
            await asyncio.sleep(0.25)
            if stable_ticks >= max_stable_ticks:
                break

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
