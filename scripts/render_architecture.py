"""Render the Airen architecture diagram in two formats:

  1. docs/airen-architecture.excalidraw  — drag into https://excalidraw.com
  2. docs/airen-architecture.md          — Mermaid block, renders on GitHub

Re-run this any time the architecture changes (don't hand-edit either file).
The single source of truth is the SPEC dict at the bottom of this module.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


# ───────────────────────────────────────────────────────────────────────
#  Excalidraw element factories
# ───────────────────────────────────────────────────────────────────────
_SEED = [0]


def _next_seed() -> int:
    _SEED[0] += 1
    return _SEED[0]


def _rect(
    id: str, x: int, y: int, w: int, h: int,
    fill: str = "#a5d8ff", stroke: str = "#4a9eed",
    stroke_w: int = 2, opacity: int = 100,
    rounded: bool = True,
) -> dict:
    return {
        "type": "rectangle", "id": id, "x": x, "y": y, "width": w, "height": h,
        "backgroundColor": fill, "fillStyle": "solid",
        "strokeColor": stroke, "strokeWidth": stroke_w, "strokeStyle": "solid",
        "roughness": 1, "opacity": opacity,
        "roundness": {"type": 3} if rounded else None,
        "angle": 0, "groupIds": [], "frameId": None,
        "seed": _next_seed(), "version": 1, "versionNonce": _next_seed(),
        "isDeleted": False, "updated": 1, "link": None, "locked": False,
        "boundElements": [],
    }


def _text(
    id: str, x: int, y: int, text: str,
    font_size: int = 14, color: str = "#1e1e1e",
    width: int | None = None, container_id: str | None = None,
    align: str = "left",
) -> dict:
    # Rough sizing — Excalidraw will re-measure on load
    lines = text.split("\n")
    longest = max(len(line) for line in lines)
    est_w = width if width is not None else int(longest * font_size * 0.55)
    est_h = int(len(lines) * font_size * 1.25)
    return {
        "type": "text", "id": id, "x": x, "y": y,
        "width": est_w, "height": est_h,
        "text": text, "fontSize": font_size,
        "fontFamily": 1, "textAlign": align, "verticalAlign":
            "middle" if container_id else "top",
        "strokeColor": color, "backgroundColor": "transparent",
        "fillStyle": "solid", "strokeWidth": 2, "strokeStyle": "solid",
        "roughness": 1, "opacity": 100, "angle": 0,
        "groupIds": [], "frameId": None, "roundness": None,
        "seed": _next_seed(), "version": 1, "versionNonce": _next_seed(),
        "isDeleted": False, "updated": 1, "link": None, "locked": False,
        "baseline": int(font_size * 0.85),
        "containerId": container_id, "originalText": text,
        "lineHeight": 1.25,
    }


def _arrow(
    id: str, x: int, y: int, dx: int, dy: int,
    color: str = "#1e1e1e", stroke_w: int = 2,
    style: str = "solid", end_arrow: str = "arrow",
) -> dict:
    return {
        "type": "arrow", "id": id, "x": x, "y": y,
        "width": abs(dx) if dx else 0, "height": abs(dy) if dy else 0,
        "points": [[0, 0], [dx, dy]],
        "strokeColor": color, "strokeWidth": stroke_w, "strokeStyle": style,
        "fillStyle": "solid", "roughness": 1, "opacity": 100,
        "roundness": None, "angle": 0, "groupIds": [], "frameId": None,
        "seed": _next_seed(), "version": 1, "versionNonce": _next_seed(),
        "isDeleted": False, "updated": 1, "link": None, "locked": False,
        "backgroundColor": "transparent",
        "startBinding": None, "endBinding": None,
        "lastCommittedPoint": None, "startArrowhead": None,
        "endArrowhead": end_arrow, "elbowed": False,
        "boundElements": [],
    }


def _labeled_box(
    id: str, x: int, y: int, w: int, h: int,
    text: str, fill: str, stroke: str,
    font_size: int = 14, text_color: str = "#1e1e1e",
) -> list[dict]:
    """Box + its centered bound text. Returns [box, text]."""
    box = _rect(id, x, y, w, h, fill=fill, stroke=stroke)
    txt_id = f"{id}_t"
    box["boundElements"] = [{"type": "text", "id": txt_id}]
    txt = _text(txt_id, x + 6, y + 4, text, font_size=font_size,
                color=text_color, width=w - 12, container_id=id,
                align="center")
    return [box, txt]


# ───────────────────────────────────────────────────────────────────────
#  Diagram specification — single source of truth
# ───────────────────────────────────────────────────────────────────────
def build_elements() -> list[dict]:
    e: list[dict] = []

    # Title
    e.append(_text("title", 390, 18, "Airen — Autonomous ML Reliability",
                   font_size=28))
    e.append(_text("subtitle", 355, 56,
                   "6 agents · 11 adapters · plan-first, narrative-on-top",
                   font_size=16, color="#757575"))

    # Top-right legend
    e.extend(_labeled_box(
        "legend", 880, 14, 290, 60,
        "Persistence: logs/<run_id>.json\n(audit trail, drives SSE stream)\nTests: 59 passing",
        fill="#f8f9fa", stroke="#757575", font_size=12,
    ))

    # ── Zone backgrounds + headers ──
    zones = [
        ("ui",  "UI / API LAYER  —  how humans and cron talk to Airen",
         85,  90,  "#dbe4ff", "#4a9eed", "#2563eb"),
        ("app", "APPLICATION LAYER  —  state machine + Slack Q&A",
         200, 100, "#e5dbff", "#8b5cf6", "#6d28d9"),
        ("llm", "LLM FACTORY  —  switchable backend per agent via AIREN_LLM_BACKEND",
         320, 75, "#fff3bf", "#f59e0b", "#b45309"),
        ("ag",  "AGENT LAYER  —  5 ADK LlmAgents · Python decides, LLM narrates",
         415, 115, "#d3f9d8", "#22c55e", "#15803d"),
        ("ad",  "ADAPTER LAYER  —  11 Protocol-based interfaces · real/mock switchable",
         550, 165, "#c3fae8", "#06b6d4", "#0e7490"),
        ("ext", "EXTERNAL SYSTEMS  —  real services Airen integrates with",
         735, 120, "#ffd8a8", "#f59e0b", "#b45309"),
    ]
    for key, label, y, h, fill, stroke, txt_color in zones:
        e.append(_rect(f"z_{key}", 40, y, 1120, h,
                       fill=fill, stroke=stroke, stroke_w=1, opacity=30))
        e.append(_text(f"z_{key}_l", 52, y + 5, label,
                       font_size=14, color=txt_color))

    # ── UI / API row (y=115-165) ──
    BLUE = ("#a5d8ff", "#4a9eed")
    e.extend(_labeled_box("ui_web", 80, 115, 240, 50,
        "Web Dashboard\nHTML + SSE live trace", *BLUE))
    e.extend(_labeled_box("ui_api", 350, 115, 240, 50,
        "FastAPI server\nroutes + Slack webhooks", *BLUE))
    e.extend(_labeled_box("ui_cli", 620, 115, 220, 50,
        "CLI runner\nrun_orchestrator", *BLUE))
    e.extend(_labeled_box("ui_cron", 870, 115, 250, 50,
        "Cron loop\nevery 5 min", *BLUE))

    # ── App row (y=230-290) ──
    e.extend(_labeled_box("app_orch", 80, 230, 720, 60,
        "AirenOrchestrator   (state machine · pure Python · NOT an LLM)\n"
        "IDLE  >  MONITORING  >  ANOMALY  >  INVESTIGATING  >  RCA  >  "
        "APPROVAL  >  REMEDIATING  >  NOTIFYING  >  VALIDATING  >  RESOLVED",
        fill="#fff3bf", stroke="#f59e0b", font_size=12))
    e.extend(_labeled_box("app_concierge", 830, 230, 290, 60,
        "Concierge agent\nSlack /airen Q&A + status queries\n"
        "(reads logs/<run_id>.json)",
        fill="#d0bfff", stroke="#8b5cf6", font_size=12))

    # ── LLM Factory bar (y=350-385) ──
    e.extend(_labeled_box("llm_bar", 80, 350, 1040, 35,
        "get_model_for(agent)   ·   Gemini 2.5 Flash   ·   "
        "Azure OpenAI GPT-4o (LiteLLM)   ·   Mock (canned, AIREN_LLM_MODE=mock)",
        fill="#fff3bf", stroke="#f59e0b", font_size=14))

    # ── Agent row (y=445-520) ──
    GREEN = ("#b2f2bb", "#22c55e")
    e.extend(_labeled_box("ag_s", 80, 445, 195, 75,
        "Sentinel\nMAE drift +\nPSI feature drift\n(get_model_health_snapshot)",
        *GREEN, font_size=12))
    e.extend(_labeled_box("ag_i", 290, 445, 205, 75,
        "Investigator\n4-source cross-ref\nPhoenix + MLflow +\nGitHub + Graphify",
        *GREEN, font_size=12))
    e.extend(_labeled_box("ag_r", 510, 445, 195, 75,
        "RCA Writer\nstructured verdicts\n-> human incident\nreport (Markdown)",
        *GREEN, font_size=12))
    e.extend(_labeled_box("ag_rm", 720, 445, 195, 75,
        "Remediation\nplan-first +\ngit clone -> revert ->\npush -> draft PR",
        *GREEN, font_size=12))
    e.extend(_labeled_box("ag_v", 930, 445, 195, 75,
        "Validator\nPhase 1: Phoenix recheck\nPhase 2: T+24h actuals\n"
        "(Redshift datamart)",
        *GREEN, font_size=12))

    # ── Adapter row (y=580-705) ──
    TEAL = ("#c3fae8", "#06b6d4")
    e.extend(_labeled_box("ad_obs", 70, 580, 240, 125,
        "Observability\n\n· Phoenix (HTTP API)\n· Phoenix MCP (stdio,\n"
        "   @arizeai/phoenix-mcp)",
        *TEAL, font_size=13))
    e.extend(_labeled_box("ad_code", 330, 580, 250, 125,
        "Code & VCS\n\n· GitHub (PyGithub)\n· GitHub MCP (stdio,\n"
        "   server-github)\n· Graphify (code graph)",
        *TEAL, font_size=13))
    e.extend(_labeled_box("ad_ml", 600, 580, 260, 125,
        "ML Lineage & Data\n\n· MLflow (training runs)\n"
        "· S3 (training baselines)\n· Redshift (rolling baseline)\n"
        "· Kafka (peek-only)",
        *TEAL, font_size=13))
    e.extend(_labeled_box("ad_comms", 880, 580, 240, 125,
        "Comms & Tickets\n\n· Slack (Block Kit +\n   /airen + buttons)\n"
        "· Jira (lifecycle:\n   create -> In Progress\n   -> Done)",
        *TEAL, font_size=13))

    # ── External systems row (y=765-840) ──
    ORANGE = ("#ffd8a8", "#f59e0b")
    e.extend(_labeled_box("ext_phx", 70, 765, 195, 75,
        "Phoenix server\nlocalhost:6006\n(spans + traces +\nMCP backend)",
        *ORANGE, font_size=12))
    e.extend(_labeled_box("ext_gh", 285, 765, 195, 75,
        "GitHub.com\ncloudqwest/\ndynamic_eta_prediction\n(real revert PRs)",
        *ORANGE, font_size=12))
    e.extend(_labeled_box("ext_fk", 500, 765, 240, 75,
        "FK production stack\nMLflow · Redshift · Kafka\n"
        "S3 data-science-4k\n(P&G training parquets)",
        *ORANGE, font_size=12))
    e.extend(_labeled_box("ext_co", 760, 765, 195, 75,
        "Slack workspace\n+ Atlassian Jira\n(real Block Kit,\n"
        "real ticket lifecycle)",
        *ORANGE, font_size=12))
    e.extend(_labeled_box("ext_llm", 975, 765, 165, 75,
        "LLM endpoints\nGemini API /\nAzure OpenAI\n(via LiteLLM)",
        *ORANGE, font_size=12))

    # ── Vertical flow arrows between layers ──
    e.append(_arrow("f1", 600, 175, 0, 25, color="#4a9eed", stroke_w=3))
    e.append(_arrow("f2", 600, 300, 0, 20, color="#8b5cf6", stroke_w=3))
    e.append(_arrow("f3", 600, 395, 0, 20, color="#f59e0b", stroke_w=3))
    e.append(_arrow("f4", 600, 530, 0, 20, color="#22c55e", stroke_w=3))
    e.append(_arrow("f5", 600, 715, 0, 20, color="#06b6d4", stroke_w=3))

    return e


# ───────────────────────────────────────────────────────────────────────
#  Output writers
# ───────────────────────────────────────────────────────────────────────
def write_excalidraw(elements: list[dict], path: Path) -> None:
    doc: dict[str, Any] = {
        "type": "excalidraw",
        "version": 2,
        "source": "airen-architecture",
        "elements": elements,
        "appState": {
            "viewBackgroundColor": "#ffffff",
            "gridSize": None,
            "theme": "light",
        },
        "files": {},
    }
    path.write_text(json.dumps(doc, indent=2))


MERMAID = """\
# Airen — Architecture Diagram

> Generated by `scripts/render_architecture.py`. Do not hand-edit.
> For the interactive version, open `docs/airen-architecture.excalidraw`
> in [excalidraw.com](https://excalidraw.com) (drag & drop the file).

```mermaid
flowchart TB

    %% ── UI / API Layer ──
    subgraph UI["UI / API LAYER &nbsp;·&nbsp; how humans and cron talk to Airen"]
        direction LR
        Web["Web Dashboard<br/>HTML + SSE live trace"]
        API["FastAPI server<br/>routes + Slack webhooks"]
        CLI["CLI runner<br/>run_orchestrator"]
        Cron["Cron loop<br/>every 5 min"]
    end

    %% ── App Layer ──
    subgraph APP["APPLICATION LAYER &nbsp;·&nbsp; state machine + Slack Q&A"]
        direction LR
        Orch["AirenOrchestrator state machine<br/>IDLE → MONITORING → ANOMALY → INVESTIGATING →<br/>RCA → APPROVAL → REMEDIATING → NOTIFYING → VALIDATING → RESOLVED"]
        Concierge["Concierge agent<br/>Slack /airen Q&A + status queries<br/>reads logs/&lt;run_id&gt;.json"]
    end

    %% ── LLM Factory ──
    LLM["LLM Factory · get_model_for(agent)<br/>Gemini 2.5 Flash · Azure OpenAI GPT-4o (LiteLLM) · Mock canned"]

    %% ── Agent Layer ──
    subgraph AG["AGENT LAYER &nbsp;·&nbsp; 5 ADK LlmAgents · Python decides, LLM narrates"]
        direction LR
        Sentinel["Sentinel<br/>MAE drift + PSI feature drift"]
        Investigator["Investigator<br/>4-source cross-ref<br/>Phoenix + MLflow + GitHub + Graphify"]
        RCA["RCA Writer<br/>structured verdicts<br/>→ human incident report"]
        Remediation["Remediation<br/>plan-first + git clone →<br/>revert → push → draft PR"]
        Validator["Validator<br/>Phase 1: Phoenix recheck<br/>Phase 2: T+24h Redshift actuals"]
    end

    %% ── Adapter Layer ──
    subgraph AD["ADAPTER LAYER &nbsp;·&nbsp; 11 Protocol-based interfaces · real/mock switchable"]
        direction LR
        Obs["Observability<br/>· Phoenix HTTP<br/>· Phoenix MCP (stdio)"]
        Code["Code & VCS<br/>· GitHub PyGithub<br/>· GitHub MCP (stdio)<br/>· Graphify code graph"]
        ML["ML Lineage & Data<br/>· MLflow<br/>· S3 training baselines<br/>· Redshift rolling baseline<br/>· Kafka peek-only"]
        Comms["Comms & Tickets<br/>· Slack Block Kit + /airen<br/>· Jira lifecycle"]
    end

    %% ── External Systems ──
    subgraph EXT["EXTERNAL SYSTEMS &nbsp;·&nbsp; real services Airen integrates with"]
        direction LR
        PhxSrv["Phoenix server<br/>localhost:6006"]
        GH["GitHub.com<br/>cloudqwest/dynamic_eta_prediction"]
        FK["FK production stack<br/>MLflow · Redshift · Kafka<br/>S3 data-science-4k"]
        SlackJira["Slack workspace<br/>+ Atlassian Jira"]
        LLMSrv["LLM endpoints<br/>Gemini API / Azure OpenAI"]
    end

    %% ── Vertical flow ──
    UI ==> APP
    APP ==> LLM
    LLM ==> AG
    AG ==> AD
    AD ==> EXT

    %% ── Key agent ↔ adapter calls ──
    Sentinel -.calls.-> Obs
    Sentinel -.baseline.-> ML
    Investigator -.calls.-> Obs
    Investigator -.calls.-> Code
    Investigator -.calls.-> ML
    Remediation -.git push.-> Code
    Validator -.recheck.-> Obs
    Validator -.actuals.-> ML
    Orch -.alerts.-> Comms

    %% Styling
    classDef uiNode  fill:#a5d8ff,stroke:#4a9eed,color:#1e1e1e
    classDef appNode fill:#fff3bf,stroke:#f59e0b,color:#1e1e1e
    classDef llmNode fill:#fff3bf,stroke:#f59e0b,color:#1e1e1e
    classDef agNode  fill:#b2f2bb,stroke:#22c55e,color:#1e1e1e
    classDef adNode  fill:#c3fae8,stroke:#06b6d4,color:#1e1e1e
    classDef extNode fill:#ffd8a8,stroke:#f59e0b,color:#1e1e1e

    class Web,API,CLI,Cron uiNode
    class Orch appNode
    class Concierge appNode
    class LLM llmNode
    class Sentinel,Investigator,RCA,Remediation,Validator agNode
    class Obs,Code,ML,Comms adNode
    class PhxSrv,GH,FK,SlackJira,LLMSrv extNode
```

## Layer cheatsheet

| Layer | What it contains | File locations |
|---|---|---|
| **UI / API** | Entry points humans hit | `airen/web/`, `airen/run_orchestrator.py` |
| **Application** | State machine + Slack chat agent | `airen/agents/orchestrator.py`, `airen/agents/concierge.py` |
| **LLM Factory** | Model selection per agent | `airen/llm_factory.py` |
| **Agent** | 5 ADK LlmAgents (Sentinel, Investigator, RCA, Remediation, Validator) | `airen/agents/<name>.py` |
| **Adapter** | Protocol + real/mock pairs for every external system | `airen/adapters/*.py` |
| **External** | Real services Airen talks to | n/a (cloud / FK VM) |

## How an incident flows through the system

1. **Trigger** — cron tick, /airen command, or web "Run Now" button
2. **Orchestrator** picks the service, starts a new IncidentRun, walks the state machine
3. **Sentinel** reads Phoenix spans → computes MAE + PSI per segment → returns SentinelVerdict
4. If WARNING/CRITICAL: **Investigator** cross-references Phoenix MCP + GitHub MCP + MLflow + Graphify → returns InvestigatorVerdict with commit/PR/file evidence
5. **RCA Writer** turns the two verdicts into a human-readable IncidentReport
6. **Remediation** drafts a RemediationPlan (revert / hotfix / no-op). Execution gated by 3 flags + confidence ≥ 0.7
7. Orchestrator posts Slack alert + creates Jira ticket → moves ticket to "In Progress"
8. (Optional, if --execute) Remediation does real `git clone → git revert → push → draft PR`
9. **Validator** Phase 1 re-queries Phoenix to confirm the fix; Phase 2 runs at T+24h via Redshift
10. On Phase 1 PASS: Jira → "Done". Otherwise: leave open for human.

Each step writes a structured event to `logs/<run_id>.json`. The SSE stream tails this file so the web UI shows the trace live.
"""


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    elements = build_elements()

    excal_path = DOCS / "airen-architecture.excalidraw"
    write_excalidraw(elements, excal_path)
    print(f"✓ {excal_path}  ({excal_path.stat().st_size / 1024:.1f} KB, {len(elements)} elements)")

    md_path = DOCS / "airen-architecture.md"
    md_path.write_text(MERMAID)
    print(f"✓ {md_path}  ({md_path.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
