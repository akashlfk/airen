# Airen — Autonomous ML Reliability Engineer

> When your production ML model misbehaves, Airen sees it in 5 minutes — not 5 days.

**Airen** is a multi-agent observability + monitoring system for production ML.
When accuracy drops, drift happens, or predictions go silent, Airen explains
**exactly what went wrong** — in plain English, with evidence — instead of
leaving the on-call engineer to dig through five dashboards at 2am.

Built for the **Google Cloud Rapid Agent Hackathon — Arize Track** using
**Gemini 2.5**, the **Google Agent Development Kit (ADK)**, and the
**Arize Phoenix MCP server**.

---

## The story Airen was built to solve

**Feb 19, 2026 — Fritolay LSTM incident.**

A commit added an `api_fetch_limit = seq_len + 25 = 73` argument to the
production inference path. Training had no such limit (~184 pings average).
MAE for long-haul predictions doubled (610 → 1,277 min, +109%). The
regression went undetected for days.

Airen catches this class of incident in minutes by cross-referencing five
data sources at once and turning the result into a Slack-ready incident
report.

---

## Architecture — six agents, one state machine

```
        ┌──────────────────────────────────────────────────────────────┐
        │                     AIREN ORCHESTRATOR                       │
        │  state machine: IDLE → MONITORING → ANOMALY_DETECTED →       │
        │                 INVESTIGATING → RCA_DRAFTING →               │
        │                 AWAITING_APPROVAL → [REMEDIATING] →          │
        │                 NOTIFYING → VALIDATING → RESOLVED            │
        └────┬──────────┬───────────┬─────────────┬───────────┬────────┘
             │          │           │             │           │
             ▼          ▼           ▼             ▼           ▼
        ┌────────┐ ┌────────┐ ┌──────────┐ ┌─────────────┐ ┌──────────┐
        │Sentinel│ │Investi-│ │RCA Writer│ │ Remediation │ │Validator │
        │        │ │ gator  │ │          │ │             │ │          │
        │Phoenix │ │Phoenix │ │Sentinel +│ │Investigator │ │Phoenix   │
        │MAE +   │ │MCP +   │ │Investiga-│ │+ GitHub PR  │ │Phase 1   │
        │drift   │ │GitHub +│ │tor → MD  │ │(plan-first) │ │(live);   │
        │detect  │ │MLflow  │ │incident  │ │             │ │Redshift  │
        │        │ │cross-  │ │report    │ │             │ │Phase 2   │
        │        │ │ref     │ │          │ │             │ │(T+24h)   │
        └────────┘ └────────┘ └──────────┘ └─────────────┘ └──────────┘
                                    │
                                    ▼
                             ┌─────────────┐
                             │   Slack     │  ← human notified, can act
                             └─────────────┘
```

Every state transition is recorded as an `IncidentEvent`. Each cycle
produces a typed `IncidentRun` persisted to `logs/<run-id>.json` and
rendered in the web UI.

---

## What each agent does

| Agent | Model | Job | Tools |
|---|---|---|---|
| **Sentinel** | Gemini 2.5 Flash | Health-monitor: computes MAE per segment, decides HEALTHY/WARNING/CRITICAL | `get_model_health_snapshot` (Phoenix) |
| **Investigator** | Gemini 2.5 Flash | Cross-references Phoenix + MLflow + GitHub to localize the cause | `query_anomaly_spans`, `find_commits_introducing_pattern`, `get_commit_diff`, `get_training_run_params`, `detect_training_inference_mismatch`, **Phoenix MCP** |
| **RCA Writer** | Gemini 2.5 Flash | Turns structured agent output into a polished human incident report | (no tools — pure narrative) |
| **Remediation** | Gemini 2.5 Flash | Plans the fix (revert/hotfix), executes only on approval | GitHub adapter (gated) |
| **Validator** | (pure Python) | Confirms the fix worked: Phase 1 live recheck, Phase 2 T+24h actuals | Phoenix re-query, Redshift adapter |
| **Orchestrator** | (pure Python) | State machine wiring it all together; owns external communication | Slack, audit log |

**Architecture principle:** every status/action decision is deterministic
Python; LLMs only generate narrative and judgment calls where natural
language is genuinely useful. This makes Airen reliable, testable, and
debuggable.

---

## What makes this work — the cross-reference

The strongest evidence for an ML regression is a **training vs inference
mismatch**. Investigator computes this:

```
MLflow training run params:       (no api_fetch_limit)  ← model never saw this
Phoenix production spans:         api_fetch_limit=73    ← production is using it
GitHub commit history:            commit d9de2cc6 added api_fetch_limit on Feb 19
                                            ▼
            CONFIRMED ROOT CAUSE — three independent sources agree
```

Investigator's `detect_training_inference_mismatch` tool returns
`{severity: 'high', mismatch: True, description: '...'}` when the training
run never set a parameter that production is now using. That's the smoking
gun a human would otherwise spend hours finding.

---

## Quick start (laptop)

### Requirements
- macOS / Linux
- Python 3.11
- conda / miniconda
- Node.js 18+ (for Phoenix MCP)
- ~500 MB disk

### Setup

```bash
git clone <this-repo> airen
cd airen

# 1) conda env + deps
conda create -n mlre python=3.11 -y
conda activate mlre
pip install -r requirements.txt

# 2) env vars — copy template and fill in
cp .env.example .env
# Edit .env:
#   GOOGLE_API_KEY=AIza...      (from https://aistudio.google.com/apikey)
#   OR set AIREN_LLM_BACKEND=azure  +  Azure OpenAI creds
#   GITHUB_TOKEN=ghp_...        (classic PAT with repo scope)
#   SLACK_BOT_TOKEN=xoxb-...    (optional, for incident posts)
#   SLACK_CHANNEL=#airen-test

# 3) start Phoenix (one-time, runs at localhost:6006)
phoenix serve &

# 4) emit some synthetic predictions
python -m demo.synthetic_data
```

### Run it

```bash
# Full pipeline — Sentinel → Investigator → RCA Writer → Remediation plan → Slack → Validator
python -m airen.run_orchestrator tl-eta

# Same but zero-cost (no LLM calls — uses canned agent outputs for offline dev)
AIREN_LLM_MODE=mock python -m airen.run_orchestrator tl-eta

# Open the web UI
python -m airen.run_web
# → http://localhost:8000
```

A new `logs/INC-XXXXXX.json` is written. The web UI shows it with the
agent timeline, remediation plan, and validator results.

---

## Three LLM modes — one env var

| Backend | Env var | When to use |
|---|---|---|
| **Mock** | `AIREN_LLM_MODE=mock` | offline dev, tests, demo without internet — zero cost |
| **Gemini** (default) | `AIREN_LLM_BACKEND=gemini` | hackathon submission, free-tier OK for low volume |
| **Azure OpenAI** | `AIREN_LLM_BACKEND=azure` | dev fallback when Gemini quota is constrained |

The factory in `airen/llm_factory.py` switches based on env. Agents are
backend-agnostic — same code, same Pydantic schemas, same outputs.

---

## Multi-service platform

Airen is config-driven. Each onboarded ML service drops a single
`services/<name>/airen.yaml`:

```yaml
service:
  name: tl-eta
  owner_team: ml-platform

phoenix:
  project_name: tl-eta-prediction

github:
  repo: cloudqwest/dynamic_eta_prediction
  attributes_of_interest: [api_fetch_limit, seq_len, max_pings, batch_size]

slack:
  alert_channel: "#ml-eta-incidents"

sentinel:
  baseline_mae_minutes: 150.0
  critical_ratio: 2.0
  default_lookback_minutes: 2880
```

Then:

```bash
python -m airen.run_orchestrator <service-name>
python -m airen.run_orchestrator --list   # show registered services
```

Agents stay generic; the config tells them which service they're working on.

---

## Tech stack

| Layer | Tech |
|---|---|
| LLM | **Gemini 2.5 Flash / Pro** (default) · Azure OpenAI gpt-4.1 (dev fallback) |
| Agent framework | **Google ADK** (`LlmAgent`, `FunctionTool`, `MCPToolset`) |
| Observability | **Arize Phoenix** (self-hosted at `localhost:6006` for dev; Phoenix Cloud for prod) |
| MCP integration | **`@arizeai/phoenix-mcp`** via `MCPToolset` (REQUIRED by Arize hackathon track) |
| Instrumentation | **OpenInference** + OpenTelemetry SDK |
| Training lineage | **MLflow** (`mlflow-skinny` client) |
| Code-side evidence | **PyGithub** for commit search + diff |
| Notifications | **Slack Web API** (Block Kit incident cards) |
| Web UI | FastAPI + Jinja2 + plain HTML/CSS (dark mode) |
| Validation | psycopg2 → Redshift (Phase 2 actuals) |
| Config | Pydantic v2 + PyYAML |
| Storage | SQLite (Phoenix) · JSON files (audit `logs/`) |

---

## Repo layout

```
mlre/
├── airen/
│   ├── agents/             # Sentinel, Investigator, RCA Writer, Remediation, Validator, Orchestrator
│   ├── adapters/           # Phoenix, GitHub, MLflow, Redshift, Kafka, Slack — real + mock
│   ├── tools/              # ADK FunctionTools (health metrics, investigation)
│   ├── ingestion/          # Kafka peek, Redshift replay
│   ├── web/                # FastAPI dashboard
│   ├── config.py           # airen.yaml schema + loader
│   ├── llm_factory.py      # Gemini ↔ Azure ↔ Mock switch
│   ├── instrumentation.py  # Phoenix OTEL tracing setup
│   ├── mocks.py            # canned agent outputs for offline dev
│   ├── schemas.py          # Pydantic models — verdict, run, event, plan
│   ├── run_orchestrator.py # canonical entrypoint
│   ├── run_sentinel.py     # one-agent runners (for debugging)
│   ├── run_investigator.py
│   ├── run_web.py          # uvicorn server
│   └── run_kafka_tap.py    # Kafka peek (mock + real)
├── demo/                   # synthetic data emitter
├── services/
│   ├── tl-eta/airen.yaml   # FK Truckload ETA reference impl
│   └── ocean-eta/airen.yaml # second service for multi-service demo
├── logs/                   # IncidentRun JSONs (audit trail; UI reads from here)
├── .gemini/settings.json   # Phoenix MCP config for Gemini CLI parity
├── .env.example
├── pyproject.toml
├── requirements.txt
└── LICENSE                 # Apache-2.0
```

---

## Submission checklist (Arize hackathon track)

- [x] Built with **Gemini** + Google **ADK**
- [x] **Phoenix** observability — local self-hosted with OpenInference tracing
- [x] **Phoenix MCP server** configured in agent (`MCPToolset` in Investigator)
- [x] `openinference-instrumentation-google-adk` for auto-tracing
- [x] Public open-source repo with **Apache-2.0** LICENSE
- [x] Multi-mode dev story (`AIREN_LLM_MODE=mock` for offline)
- [ ] Hosted project URL — Cloud Run deploy in progress
- [ ] ≤3-min demo video — script in `docs/demo-script.md`

---

## Credits & acknowledgments

- **Arize** for Phoenix + the OpenInference standard
- **Google ADK team** for a clean agent framework
- The **Gemini Hackathon team** for the well-designed starter template at
  [`Arize-ai/gemini-hackathon`](https://github.com/Arize-ai/gemini-hackathon)
- The blueprint reference at `MLRE-technical-blueprint.md` (2,632 lines —
  the design doc this implementation tracks against)

---

## License

Apache-2.0 — see [`LICENSE`](./LICENSE).

Copyright 2026 Akash Lakshmipathy.
