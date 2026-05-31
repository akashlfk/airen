# Airen — Architecture

> **One-line summary:** Six agents and a state machine. Deterministic Python
> for decisions, LLMs for narrative. One canonical entry point, one audit
> trail, one Slack alert.

---

## Why Airen exists

Production ML models drift, regress, and silently die. The on-call engineer
typically has to:

1. Notice the regression (often hours later)
2. Open Phoenix / Grafana / Datadog to confirm
3. Slice the data to find which segment is broken
4. Cross-reference with recent code changes via git blame
5. Cross-reference with training-time params via MLflow
6. Write a coherent incident summary
7. Decide whether to revert, hotfix, or retrain
8. Post to Slack, page the team, file a Jira ticket

Airen does steps 1-7 autonomously. Step 8 happens automatically. The human
sees the final, evidence-backed incident report in Slack and clicks Approve.

The whole loop runs in under 3 minutes.

---

## The six agents

Each agent is an independent ADK `Agent` with its own model, instruction,
tools, and (where appropriate) Pydantic output schema. They share NO state
in memory — everything passes through structured Pydantic verdicts.

### 1. Sentinel — the watchman

- **Job:** Decide if production is healthy. Returns `HEALTHY | WARNING | CRITICAL`.
- **Model:** Gemini 2.5 Flash (cheap, fast, runs every 5 min in autonomous mode).
- **Tools:** `get_model_health_snapshot` — computes MAE per segment from Phoenix spans.
- **Key insight:** The status decision is made by **Python**, not the LLM. The
  tool computes the snapshot and embeds a `decision` dict with `suggested_status`,
  `suggested_severity_score`, etc. The LLM just copies these and writes a
  narrative summary. This means Sentinel is *provably deterministic* — wrong
  decisions are bugs, not LLM hallucinations.

### 2. Investigator — the diagnostician

- **Job:** Cross-reference three independent sources to localize the cause.
- **Model:** Gemini 2.5 Flash (or Pro on Vertex when available).
- **Tools (6):**
  1. `query_anomaly_spans` — drill into Phoenix data for the flagged segment
  2. `find_commits_introducing_pattern` — GitHub search + commit ranking
  3. `get_commit_diff` — pull the actual code change for the top suspect
  4. `get_training_run_params` — MLflow training run params + metrics
  5. `detect_training_inference_mismatch` — **the smoking-gun cross-reference**
  6. **Phoenix MCP toolset** (`@arizeai/phoenix-mcp`) — adds Phoenix's full
     MCP tool set: search_spans, list_projects, get_dataset, run_evaluators,
     etc.

#### The smoking-gun pattern

The strongest evidence for an ML regression is when production uses a
parameter the model was **never trained against**. Investigator's
`detect_training_inference_mismatch` runs this check:

```python
training_params = MLflow.get_latest_run("tl-eta-prod").params
# {"seq_len": "48", "max_pings": "250", "batch_size": "64", ...}
# Note: NO `api_fetch_limit` key

observed_in_production = "api_fetch_limit=73"  # from Sentinel's anomaly

if training_params.get("api_fetch_limit") is None:
    return {"mismatch": True, "severity": "high"}  # confirmed
```

When this returns `severity: high`, Investigator's confidence jumps from
~0.75 (commit-only evidence) to 0.94 (commit + MLflow agreement).

### 3. RCA Writer — the technical writer

- **Job:** Turn structured agent output into a polished human incident report.
- **Model:** Gemini 2.5 Flash.
- **Tools:** None — pure narrative transformation.
- **Output:** A Pydantic `IncidentReport` with title, TL;DR, what-happened,
  root-cause, evidence-bullets, recommended-fix, open-questions. Same content
  fans out to Slack, the web UI, and (future) Jira ticket descriptions.

### 4. Remediation — the fixer

- **Job:** Plan a code-level fix. Optionally execute it.
- **Model:** Gemini 2.5 Flash (for PR title/body narrative only).
- **Tools:** GitHub adapter (`create_branch`, `create_pull_request`).
- **Plan-first design:** Always produces a `RemediationPlan` (zero side
  effects). Execution requires three gates: (a) `--execute` CLI flag,
  (b) `AIREN_ALLOW_REMEDIATION_EXECUTE=1` env var, (c) Investigator
  confidence ≥ 0.7. Airen never opens PRs unprompted.
- **Action types:** `REVERT_COMMIT`, `HOTFIX_PR`, `RETRAIN_MODEL` (future),
  `ROLLBACK_DEPLOY` (future), `NO_ACTION`.

### 5. Validator — the verifier

- **Job:** Confirm the fix actually worked.
- **Model:** None — pure Python (this is ratio math, not narrative).
- **Phases:**
  - **Phase 1 (live, ~10 min after fix):** Re-query Phoenix with a short
    window. Compare current segment ratios to the originals. Returns
    `PASS | FAIL | INCONCLUSIVE | NO_DATA`.
  - **Phase 2 (T+24h actuals):** Query Redshift `tl_eta_datamart` for
    delivered actuals. Compare predicted-vs-actual MAE. Stubbed in laptop
    mode; runs for real on FK VM.

### 6. Orchestrator — the conductor

- **Implementation:** Pure-Python state machine. Not an LLM agent.
- **State enum:** `IDLE → MONITORING → ANOMALY_DETECTED → INVESTIGATING →
  RCA_DRAFTING → AWAITING_APPROVAL → [REMEDIATING] → NOTIFYING →
  VALIDATING → RESOLVED` (or `FAILED` at any point).
- **Persistence:** Every state transition writes an `IncidentEvent` to
  `logs/<run_id>.json` incrementally. The web UI's SSE endpoint tails
  this file in real time.

#### Why Python, not an LLM-driven router

The judges-relevant observation: LLM-driven routing (i.e., a top-level
agent that decides which sub-agent to call) is probabilistic. For a
*monitoring* system that's the wrong tradeoff — we need provable
transitions, an auditable trace, and zero risk of "the orchestrator
forgot to call Investigator." Python state machines give us that. Each
specialist is still an ADK agent, so the system is multi-agent in the
hackathon-relevant sense — we just don't make the LLM responsible for
flow control.

---

## The architecture pattern — "Python decides, LLM narrates"

This is the single most important design rule in Airen, applied across
every agent:

| Component | What decides | Example |
|---|---|---|
| Sentinel `_decide_status()` | Pure Python | If `worst_segment_ratio > 2.0` → CRITICAL |
| Investigator action selection | Python (tool dispatch) | Always call MLflow check before GitHub search |
| Remediation action_type | Pure Python | If `confidence < 0.7` → NO_ACTION |
| Validator status | Pure Python | If `worst_after_ratio < 1.2` → PASS |
| Orchestrator state transitions | Pure Python | After NOTIFYING, always run Validator |
| **Narrative (every agent's prose output)** | LLM | "The TL ETA model is producing predictions 4× more inaccurate than usual…" |

**Why:** LLM outputs drift between runs. A monitoring agent that
*occasionally* returns the wrong status is worse than no monitoring. By
keeping all decision points in Python, Airen is deterministic where it
matters and flexible where it doesn't.

---

## The cross-reference flow (Investigator's reasoning)

```
        Sentinel verdict
        anomaly: api_fetch_limit=73, ratio 4.21x
                │
                ▼
   ┌───────────────────────────────────────────────────────┐
   │            INVESTIGATOR  (multi-source diagnosis)      │
   ├───────────────────────────────────────────────────────┤
   │                                                        │
   │  1) Phoenix  query_anomaly_spans                       │
   │     → 37 of 200 predictions match the segment          │
   │     → earliest occurrence 2026-05-27T19:42 UTC         │
   │                                                        │
   │  2) MLflow   detect_training_inference_mismatch        │
   │     → training run mock_run_d9de2cc6 has NO            │
   │       `api_fetch_limit` parameter                      │
   │     → severity: high                                   │
   │     → CONFIRMED MISMATCH (this is the smoking gun)     │
   │                                                        │
   │  3) GitHub   find_commits_introducing_pattern          │
   │     → commit 6270ca55 by akashlfk on 2026-05-27        │
   │     → file: LocationService.fetch_historical_checkcalls│
   │     → PR #741 (ETAI-338)                               │
   │                                                        │
   │  Sources agree:                                        │
   │    confidence: 0.94                                    │
   │    root_cause: "Training/inference mismatch.           │
   │                 PR #741 introduced api_fetch_limit=73, │
   │                 which training never had."             │
   │                                                        │
   └───────────────────────────────────────────────────────┘
```

---

## The data layer — adapter pattern

```
        ┌─────────────────────────────────────────────────┐
        │   Agents talk to abstract adapter Protocols     │
        │   never to psycopg2 / boto3 / kafka directly    │
        └─────────────────────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                                       ▼
   FK-real adapters                   Mock / GCP-clean adapters
   (used on FK VM)                    (used on laptop + submission)
   ────────────────                   ──────────────────────
   RealPhoenix (local)                MockPhoenix (in-memory)
   RealGitHub  (PyGithub)             MockGitHub  (canned diffs)
   RealMLflow  (mlflow-skinny)        MockMLflow  (canned run)
   RealRedshift (psycopg2)            MockRedshift (synthetic rows)
   RealKafka   (confluent-kafka)      MockKafka   (synthetic msgs)
   RealSlack   (slack-sdk)            (Slack always real;
                                       MockSlack would skip post)
```

`AIREN_REDSHIFT_MODE=real|mock` toggles per-adapter. The agent code never
sees the difference. This means:

- **Laptop dev** — everything works in mock mode, offline, in <2 seconds
- **FK VM** — real adapters connect to Kafka, Redshift, MLflow, internal Phoenix
- **Hackathon submission** — runs in mock mode OR with Phoenix Cloud + Gemini

---

## The LLM layer — `llm_factory.py`

Three backends, one switch:

```
   AIREN_LLM_BACKEND=mock     → bypass entirely, return canned outputs
   AIREN_LLM_BACKEND=gemini   → Gemini via google-genai SDK (default)
   AIREN_LLM_BACKEND=azure    → Azure OpenAI via LiteLLM (dev fallback)
```

The factory also handles the **structured-output portability** problem.
Gemini supports `response_format=json_schema` natively; many Azure
deployments don't. So:

- When the backend supports strict schemas → agent passes `output_schema=Pydantic`
- When it doesn't → agent appends the schema as prompt text + parses with
  `extract_json(text)`

Same agent code, both backends. See `airen/llm_factory.py`.

---

## The web UI — narrative, not span-tree

Built with FastAPI + Jinja2 + plain HTML/CSS (no React build step). Three pages:

1. **`/` — Home (fleet view)**
   - One row per onboarded service with traffic-light health
   - Recent incidents list
   - "Run new check" button → POST `/api/run/<service>` → redirect to live

2. **`/incident/<id>` — Full incident**
   - Severity-colored header
   - Markdown sections from the IncidentReport (what happened, root cause,
     evidence, recommended fix, open questions)
   - **Remediation plan card** with action, target commit, proposed PR title
   - **Validator card** with before/after ratio comparison table
   - **Agent timeline** showing every state transition with timestamps + durations
   - Link out to the Slack thread

3. **`/live` — Real-time agent feed**
   - SSE stream from `/events/<run_id>`
   - Animated event entries appear as state transitions happen
   - Auto-scroll, pulse indicator, final-state banner with link to incident
   - This is the screen the demo video centers on

The home page intentionally does NOT show a trace tree. The full Phoenix UI
is one click away under "Sub-agent raw output" — but it's never the front
door, because Phoenix's UI is for engineers debugging traces, not for ML
engineers making incident decisions.

---

## Phoenix MCP integration

The Arize hackathon track requires the Phoenix MCP server be configured in
the agent. Airen does this two ways:

### 1. ADK programmatic integration (`MCPToolset`)

In `airen/agents/investigator.py`:

```python
from google.adk.tools.mcp_tool import MCPToolset, StdioConnectionParams
from mcp import StdioServerParameters

phoenix_mcp = MCPToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="npx",
            args=["-y", "@arizeai/phoenix-mcp@latest",
                  "--baseUrl", "http://localhost:6006"],
        ),
        timeout=30.0,
    ),
)
```

This spawns the Phoenix MCP server as a subprocess. The MCP server talks
to Phoenix's HTTP API. ADK auto-exposes the server's tools as agent
function calls. Toggleable via `AIREN_USE_PHOENIX_MCP=0`.

### 2. Gemini CLI integration (`.gemini/settings.json`)

For interactive debugging via Gemini CLI:

```json
{
  "mcpServers": {
    "phoenix": {
      "command": "npx",
      "args": ["-y", "@arizeai/phoenix-mcp@latest", "--baseUrl", "http://localhost:6006"]
    }
  }
}
```

Matches the pattern in Arize's official starter repo. Useful for
"talk to Phoenix" workflows outside the agent system.

---

## OpenInference tracing — Airen observes itself

`airen/instrumentation.py` calls `phoenix.otel.register(auto_instrument=True)`
at startup, which discovers our installed `openinference-instrumentation-*`
packages and wraps:

- Every ADK agent invocation → an `AGENT` span
- Every `FunctionTool` call → a `TOOL` span
- Every `google-genai` LLM call → an `LLM` span
- Every MCP tool call → also captured

The result: Airen's own behavior shows up in Phoenix project `airen-dev`,
alongside the model predictions it monitors in project `tl-eta-prediction`.
**One observability platform, both the watcher and the watched.**

---

## Multi-service platform — `airen.yaml`

Onboarding a new ML service is one file:

```yaml
# services/my-new-service/airen.yaml
service:
  name: my-new-service
  owner_team: platform

phoenix:
  project_name: my-new-service-prediction

github:
  repo: cloudqwest/my-new-service
  attributes_of_interest:
    - my_special_param
    - other_constant_that_can_drift

sentinel:
  baseline_mae_minutes: 200.0   # tune per service
  critical_ratio: 2.0
```

Then `python -m airen.run_orchestrator my-new-service` — the agents pick
up the config and operate on the new service. No code changes.

---

## State machine — formal flow

```
                    IDLE
                     │
                     ▼
                MONITORING
                     │
                 ┌───┴────────┐
        HEALTHY │            │ WARNING/CRITICAL
                ▼            ▼
      HEALTHY_NO_ACTION   ANOMALY_DETECTED
                │            │
                │            ▼
                │      INVESTIGATING
                │            │
                │            ▼
                │      RCA_DRAFTING
                │            │
                │            ▼
                │      AWAITING_APPROVAL
                │            │ (plan generated)
                │      ┌─────┴─────┐
                │   --execute   no --execute
                │      │            │
                │      ▼            │
                │   REMEDIATING     │
                │      │            │
                │      └────┬───────┘
                │           ▼
                │      NOTIFYING
                │           │ (Slack post)
                │           ▼
                │       VALIDATING
                │           │
                ▼           ▼
              RESOLVED  ◄─── (or FAILED at any step)
```

Every transition is recorded with timestamp + duration in
`IncidentRun.events`. The audit trail is the source of truth — both the
UI and Slack messages read from it.

---

## What's deferred (and why)

| Component | Why deferred |
|---|---|
| Live Kafka peek to FK production | Laptop VPN can't route `172.24.x.x` — needs FK VM |
| Real Redshift queries | Same VPN routing |
| Real MLflow at `mlflow-dev.fourkites.com` | Same VPN routing |
| Validator Phase 2 (T+24h actuals check) | Needs Redshift on FK VM |
| Slack interactive Approve/Reject buttons | Requires a publicly reachable webhook URL (needs deploy or ngrok) |
| Jira / Atlassian MCP | Second MCP — additive polish, not required for eligibility |
| Vertex AI Agent Engine | Hackathon track accepts ADK alone; Cloud Run deploy is enough for the hosted URL |

Every deferred item has a mock implementation that demonstrates the contract
and works end-to-end in laptop mode. The real implementations are 1-line
adapter swaps when the right access exists.

---

## Repo layout reference

See [`README.md`](../README.md) for the full directory tree and quickstart.

---

## License & credits

Apache-2.0. Built on top of Arize Phoenix, OpenInference, Google ADK,
Gemini, MLflow, PyGithub, Slack SDK, and FastAPI.
