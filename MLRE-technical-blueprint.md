# Airen — Autonomous ML Reliability Engineer
## *"While your team sleeps, Airen watches."*
### Full Technical Blueprint + Demo Script
#### Google Cloud Rapid Agent Hackathon · Arize Track · Grand Prize Submission

**Deadline:** June 11, 2026 @ 2:00pm PDT
**Track:** Arize
**Partner MCP:** Arize Phoenix MCP (primary) · Atlassian MCP · Slack MCP (supporting)
**Stack:** Gemini 2.5 Pro + Google Cloud ADK + Vertex AI Agent Engine + Cloud Run

---

## Table of Contents
1. [The One-Paragraph Pitch](#1-the-one-paragraph-pitch)
2. [The Real War Story — FourKites Fritolay Incident](#2-the-real-war-story--fourkites-fritolay-incident)
3. [Architecture Overview](#3-architecture-overview)
4. [Platform Design — Config-Driven Multi-Service](#4-platform-design--config-driven-multi-service)
5. [Agent Design — 6 Agents, Hierarchical Pattern](#5-agent-design--6-agents-hierarchical-pattern)
6. [Arize Phoenix MCP — Exact Tool Usage](#6-arize-phoenix-mcp--exact-tool-usage)
7. [Data Source Adapters](#7-data-source-adapters)
8. [GitHub Repo Integration](#8-github-repo-integration)
9. [Two-Phase Validation — Kafka + Redshift](#9-two-phase-validation--kafka--redshift)
10. [Slack Integration — Approvals + Chat Interface](#10-slack-integration--approvals--chat-interface)
11. [Jira Integration — Full Ticket Lifecycle](#11-jira-integration--full-ticket-lifecycle)
12. [Service Onboarding Flow](#12-service-onboarding-flow)
13. [Google Cloud Stack](#13-google-cloud-stack)
14. [Full Code Scaffolding](#14-full-code-scaffolding)
15. [Demo Data Setup — TL ETA Reference Implementation](#15-demo-data-setup--tl-eta-reference-implementation)
16. [Web UI — Reasoning Trace Dashboard](#16-web-ui--reasoning-trace-dashboard)
17. [Technical Deep Dive — Concepts & Build Guide](#17-technical-deep-dive--concepts--build-guide)
18. [17-Day Build Plan](#18-17-day-build-plan)
19. [3-Minute Demo Script](#19-3-minute-demo-script)
20. [Devpost Submission Checklist](#20-devpost-submission-checklist)
21. [Judge Talking Points](#21-judge-talking-points)

---

## 1. The One-Paragraph Pitch

> ML models fail silently. A Truck Load ETA model drifts because a bundled performance PR quietly truncated inference inputs. A fraud model degrades because an upstream schema changed. A recommendation engine goes stale because training data aged out. Nobody notices until the business impact is catastrophic — and by the time someone runs the diagnostic SQL, days have passed. **Airen (Autonomous ML Reliability Engineer)** is a config-driven, 6-agent autonomous platform built on Arize Phoenix + Google Cloud ADK that watches your ML models 24/7, detects anomalies the moment they emerge, diagnoses root cause by cross-referencing live predictions, training data, MLflow runs, and recent Git commits, then explains it in plain English via Slack — all while keeping humans in control at every irreversible step. Uniquely, Airen uses Phoenix to observe both the ML model AND the AI agents monitoring it: every drift check, every PR bisect, every human approval is a Phoenix span. Any ML team onboards in an afternoon by writing one YAML file. It is not a dashboard. It is an engineer that never sleeps — and it talks back.

---

## 2. The Real War Story — FourKites Fritolay Incident

This is the narrative that wins the grand prize. It is not hypothetical — it actually happened.

### The Incident (What Actually Occurred)

FourKites runs an LSTM-based Truck Load ETA prediction model for Fritolay. On **February 19, 2026 at 12:32 IST**, PR #621 was merged into `dynamic-eta-prediction`. On the surface it looked like a routine shadow performance improvement. Bundled inside was commit `d9de2cc6` which added one line:

```python
api_fetch_limit = seq_len + 25  # = 73
```

This capped the Location Service API call at inference to 73 historical checkcalls. The training code — `experiments/fritolay_based_model/train.py` — used no equivalent limit and trained on full checkcall history (200+ pings on average).

**Result:** The model was trained on rich full-history sequences and asked at inference to predict from truncated 73-ping sequences. Classic training-inference mismatch.

- 3_long PROD error: **610 min → 1,277 min (+109%)**
- Decile-1 gap: **+565 min** (early journey blindest — confirms truncation mechanism)
- Detected: **manually, via SQL against `tl_eta_datamart`, days later**
- Root cause identified: **after hours of forensic investigation**

### What Airen Does Instead

- **12:32 IST** — PR #621 merges. Deploy goes out.
- **12:37 IST** — Sentinel agent queries `tl_eta_datamart` and Arize Phoenix. Fritolay 3_long MAE crosses 2x rolling 7-day threshold. PSI on `sequence_length` = 0.41 (critical). Alert fires.
- **12:38 IST** — Investigator runs Q1 (haul bucket), Q3 (haul × decile), Q4 (daily trend) automatically. Decile-1 gap confirmed. Investigator queries GitHub: 3 PRs merged in last 24h. PR #621 touched `predict/processing/location_service/helper.py` — a high-risk file. Reads the diff. Finds `limit={api_fetch_limit}`.
- **12:41 IST** — Investigator reads training code. No equivalent limit found. Reads S3 training parquet: average sequence length 184 pings. Reads Kafka: current inference sequences averaging 73 pings. PSI on sequence length confirmed.
- **12:43 IST** — RCA Writer outputs to Slack: *"PR #621 (merged 12:32 IST) capped checkcall history at 73 pings at inference. Training used full history averaging 184 pings. Model has never seen truncated inputs. Long-haul early-journey predictions worst affected. Two options: remove the cap (10 min) or retrain with matching truncation (2 hrs)."*
- **12:47 IST** — Engineer reads 4 sentences in Slack. Clicks **Approve Option 1**.
- **12:47 IST** — Jira ticket ETA-1821 auto-created with full RCA as description.
- **12:55 IST** — Hotfix PR #624 opened and merged.
- **13:02 IST** — Deploy confirmed. Phase 1 validation starts (Kafka).
- **13:12 IST** — Phase 1 PASS. Predictions flowing, distribution normalized.
- **Next day 02:00 IST** — Phase 2 PASS. `tl_eta_datamart` shows MAE recovered to 641 min (baseline: 610 min). Jira ticket ETA-1821 closed with post-mortem.

**Total damage: $0. Total human effort: reading 4 sentences and clicking one button. Total time from breakage to fix: 25 minutes active.**

---

## 3. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        Airen System Architecture                           │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                            │
│  PRODUCTION ML SYSTEM (FourKites TL ETA — Reference Implementation)      │
│  ┌─────────────┐  predictions+features   ┌──────────────────────────┐   │
│  │  TL ETA     │ ──────────────────────► │    Arize Phoenix          │   │
│  │  Model      │                         │    (Observability)        │   │
│  │  (LSTM /    │  Kafka predictions ───► │    MCP Server             │   │
│  │  Transformer│                         └──────────┬───────────────┘   │
│  └─────────────┘                                    │                    │
│  ┌─────────────┐  actuals (delivered ETAs)          │                    │
│  │ tl_eta_     │ ──────────────────────────────────►│                    │
│  │ datamart    │                                     │                    │
│  │ (Redshift)  │                                     │                    │
│  └─────────────┘                                     │                    │
│  ┌─────────────┐                                     │                    │
│  │ Training    │  S3 parquet files                   │                    │
│  │ Data (S3)   │ ───────────────────────────────────►│                    │
│  └─────────────┘                                     │                    │
│  ┌─────────────┐                                     │                    │
│  │ MLflow      │  model lineage + run metrics        │                    │
│  │ (Internal)  │ ───────────────────────────────────►│                    │
│  └─────────────┘                                     │                    │
│  ┌─────────────┐                                     │                    │
│  │ GitHub Repo │  commits + diffs + code             │                    │
│  │ dynamic-eta │ ───────────────────────────────────►│                    │
│  └─────────────┘                                     │                    │
│                                                       │                    │
├───────────────────────────────────────────────────────┼────────────────────┤
│                                                        │                    │
│  Airen AGENT SYSTEM (Vertex AI Agent Engine)            │                    │
│                                                        ▼                    │
│  ┌──────────────────────────────────────────────────────────────────────┐ │
│  │                        Orchestrator Agent                             │ │
│  │                        (Gemini 2.5 Pro)                               │ │
│  │          Slack MCP · Jira MCP · Audit Tool                           │ │
│  └───┬──────────┬────────────┬──────────────┬───────────────────────────┘ │
│      │          │            │              │                              │
│      ▼          ▼            ▼              ▼                              │
│  ┌────────┐ ┌────────┐ ┌─────────┐ ┌────────────┐ ┌──────────┐          │
│  │Sentinel│ │Investi-│ │  RCA    │ │Remediation │ │Validator │          │
│  │ Agent  │ │ gator  │ │ Writer  │ │   Agent    │ │  Agent   │          │
│  │        │ │ Agent  │ │  Agent  │ │            │ │          │          │
│  │Phoenix │ │Phoenix │ │Gemini   │ │ Auto-      │ │ Phoenix  │          │
│  │Redshift│ │Redshift│ │RCA in   │ │ trainer    │ │ Redshift │          │
│  │ PSI    │ │S3+MLflow│ │plain   │ │ trigger +  │ │ Kafka    │          │
│  │checks  │ │GitHub  │ │English  │ │ shadow dep │ │ 2-phase  │          │
│  └────────┘ └────────┘ └─────────┘ └────────────┘ └──────────┘          │
│                                                                            │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                            │
│  HUMAN INTERFACES                                                          │
│  ┌──────────────────────────────┐   ┌──────────────────────────────────┐ │
│  │  Slack — Primary Interface   │   │  Jira — Ticket Lifecycle          │ │
│  │  · Alert messages            │   │  · Auto-created on approval       │ │
│  │  · Approve / Reject buttons  │   │  · Commented at each stage        │ │
│  │  · Conversational chat       │   │  · Auto-resolved on fix confirm   │ │
│  │  · Incident threads          │   │  · Post-mortem in description     │ │
│  └──────────────────────────────┘   └──────────────────────────────────┘ │
│  ┌──────────────────────────────────────────────────────────────────────┐ │
│  │  Web UI (Cloud Run) — Read-Only Audit Dashboard                      │ │
│  │  · Live agent status · Reasoning trace · Full audit trail            │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Platform Design — Config-Driven Multi-Service

Airen is not a one-off tool for TL ETA. It is a platform. Any ML team onboards by writing one YAML file. The agents are service-agnostic. The config drives all behavior.

### What Is Fixed (Core Platform)

- The 6-agent workflow and hierarchy
- All adapter code (Kafka, Redshift, S3, MLflow, GitHub, Phoenix, Slack, Jira)
- Two-phase validation logic
- Audit trail and web UI
- Human approval flow via Slack

### What Varies Per Service (Lives in `airen.yaml`)

- Which Kafka topic, which Redshift table, which S3 path
- Which features to compute PSI on, and thresholds
- Which dimensions to segment by (haul type for TL ETA, category for recommendations)
- Which diagnostic SQL queries to run
- Which GitHub repo and which paths are inference vs training
- Which Slack channel, which Jira project
- What "healthy" looks like for this model

### The `airen.yaml` Config File — TL ETA Reference

```yaml
service:
  name: tl-eta-prediction
  description: Truck Load ETA prediction — LSTM and Transformer models
  team: ml-eta
  slack_channel: C07AB12XYZ   # #ml-eta-incidents

models:
  - id: transformer_localized_v2
    family: transformer
    shippers: [procter-gamble, 3m-company, kraft-heinz, tyson, lactalis]
  - id: fritolay_lstm
    family: lstm
    shippers: [fritolay]

data_sources:
  predictions:
    type: kafka
    topic: tl_eta_predictions
    model_identity_fields:
      family: model_family
      sub_type: model_sub_type
      haul_type: model_haul_type
      region: model_region
    field_mappings:
      prediction_value: predicted_eta_minutes
      entity_id: load_id
      timestamp: prediction_timestamp

  actuals:
    type: redshift
    table: public.tl_eta_datamart
    update_frequency: daily
    auth_secret: airen/tl-eta/redshift-creds
    field_mappings:
      prediction: predicted_delivery_time
      actual: actual_delivery_time
      error: abs_error_minutes
      timestamp: load_delivered_at
      model_id: model_family
      entity_id: load_id

  training_data:
    type: s3
    path: s3://data-science-4k/OTR_TL_ETA/unified_new/{shipper}/train_data/
    file_format: parquet
    auth_secret: airen/tl-eta/s3-creds
    feature_columns: [sequence_length, distance_in_miles, hour_of_day, haul_type]
    label_column: actual_delivery_minutes

  mlflow:
    tracking_uri: http://mlflow.internal:5000
    experiment: tl_eta_transformer
    auth_secret: airen/tl-eta/mlflow-token

  code_repo:
    url: https://github.com/fourkites/dynamic-eta-prediction
    default_branch: main
    auth_secret: airen/org/github-pat
    inference_paths:
      - predict/processing/
      - main.py
      - predict/app_constants.py
    training_paths:
      - experiments/
      - training_pipeline/auto_trainer/
    high_risk_files:
      - predict/processing/location_service/helper.py
      - predict/processing/lstm/LSTMConstants.py
      - predict/processing/processor_factory.py
    key_patterns:
      - api_fetch_limit
      - seq_len
      - SEQUENCE_LENGTH

monitoring:
  performance:
    metric: MAE
    warning_threshold: 1.5x_rolling_7day
    critical_threshold: 2x_rolling_7day

  features:
    - name: sequence_length
      type: numerical
      psi_warning: 0.10
      psi_critical: 0.25
    - name: distance_in_miles
      type: numerical
      psi_warning: 0.10
      psi_critical: 0.25
    - name: haul_type
      type: categorical

  segments: [haul_type_bucket, decile, shipper_id]

  diagnostic_queries:
    - name: haul_bucket_breakdown
      file: queries/q1_haul_bucket.sql
    - name: haul_decile_breakdown
      file: queries/q3_haul_decile.sql
    - name: daily_trend
      file: queries/q4_daily_trend.sql

validation:
  phase1:
    source: kafka
    window_minutes: 30
    min_predictions: 100
    checks: [volume, distribution, null_rate, identity, sanity_bounds]
  phase2:
    source: redshift
    delay_hours: 24
    checks: [MAE_vs_baseline, haul_bucket_regression, shipper_regression]

  healthy_baseline:
    MAE_p50: 45
    MAE_p95: 180
    null_rate_max: 0.03
    prediction_min: 0
    prediction_max: 5000

notifications:
  slack:
    channel_id: C07AB12XYZ
    bot_token_secret: airen/org/slack-bot-token
    approval_mode: interactive_buttons
    thread_updates: true
    chat_enabled: true
    respond_in_dms: true

  jira:
    base_url: https://fourkites.atlassian.net
    project_key: ETA
    issue_type: Bug
    auth_secret: airen/org/jira-token
    priority_mapping:
      CRITICAL: High
      WARNING: Medium
    labels: [airen, auto-detected]
    auto_create: false
    check_existing: true
    lifecycle:
      comment_on_remediation_start: true
      comment_on_phase1_complete: true
      comment_on_phase2_complete: true
      auto_transition_on_resolve: true
      generate_postmortem: true
```

### Repository Structure

```
airen/
│
├── core/                              # Never touch once stable
│   ├── agents/
│   │   ├── orchestrator.py            # Generic, reads config
│   │   ├── sentinel.py
│   │   ├── investigator.py
│   │   ├── rca_writer.py
│   │   ├── remediation.py
│   │   └── validator.py
│   │
│   ├── adapters/                      # One adapter per data source type
│   │   ├── kafka.py                   # Stateless peek (no consumer group)
│   │   ├── redshift.py                # Query runner
│   │   ├── s3.py                      # Parquet reader + feature stats
│   │   ├── mlflow.py                  # Run fetcher
│   │   ├── github.py                  # PR bisect + code reader
│   │   ├── phoenix.py                 # Arize Phoenix MCP
│   │   ├── slack.py                   # Alerts + chat + approvals
│   │   └── jira.py                    # Ticket lifecycle
│   │
│   └── config/
│       ├── schema.py                  # Pydantic validation of airen.yaml
│       └── loader.py
│
├── services/                          # One folder per ML service
│   ├── tl-eta/                        # Reference implementation
│   │   ├── airen.yaml
│   │   ├── graph/                     # Graphify outputs (auto-generated at onboarding)
│   │   │   ├── graph.json             # Queryable knowledge graph
│   │   │   ├── graph.html             # Interactive visual (embedded in web UI)
│   │   │   └── GRAPH_REPORT.md        # Auto-written repo summary → replaces repo_context.md
│   │   └── queries/
│   │       ├── q1_haul_bucket.sql
│   │       ├── q3_haul_decile.sql
│   │       └── q4_daily_trend.sql
│   └── <next-service>/
│       └── airen.yaml
│
├── ui/                                # Read-only audit dashboard
├── deploy/                            # GCP deployment
└── cli/
    └── airen.py                        # airen add-service / airen status / airen run
```

---

## 5. Agent Design — 6 Agents, Hierarchical Pattern

### Agent 1: Orchestrator

**Role:** Master coordinator. Receives triggers (scheduled or event-based, or Slack chat messages), delegates to specialist agents, manages state, enforces human-in-the-loop checkpoints. Owns all external communication — Slack and Jira.

**State machine:**
```
IDLE → MONITORING → ANOMALY_DETECTED → INVESTIGATING →
RCA_WRITTEN → AWAITING_APPROVAL → REMEDIATING → VALIDATING → RESOLVED
```

```python
orchestrator = LlmAgent(
    name="AirenOrchestrator",
    model="gemini-2.5-pro",
    instruction="""
    You are Airen — the Autonomous ML Reliability Engineer orchestrator.

    You coordinate 5 specialist agents and own all human communication
    via Slack and Jira. You also respond to direct Slack chat messages
    from engineers — status checks, investigation requests, model queries.

    Workflow (automated monitor cycle):
    1. Delegate to SentinelAgent to check model health
    2. If anomaly detected (severity > MEDIUM), delegate to InvestigatorAgent
    3. After investigation, delegate to RCAWriterAgent
    4. Post RCA to Slack with approval buttons — STOP and wait for human
    5. On approval: optionally create Jira ticket, then run RemediationAgent
    6. Post phase updates as Slack thread replies and Jira comments
    7. Delegate to ValidatorAgent — two phases
    8. On full resolution: transition Jira to Done, post Slack summary

    Workflow (Slack chat message from engineer):
    - "status" / "health" → quick SentinelAgent check, reply in Slack
    - "why" / "investigate" → InvestigatorAgent, reply in thread
    - "model version" / "last retrain" → MLflow tool, reply directly
    - "what changed" / "recent commits" → GitHub tool, reply directly
    - "incident" / "post-mortem" → Audit log query, reply directly
    - Conversational follow-ups in incident thread → InvestigatorAgent with context

    CRITICAL RULES:
    1. Never trigger RemediationAgent without explicit human approval in Slack
    2. Never mark RESOLVED until ValidatorAgent Phase 2 confirms
    3. Log every state transition to AuditLogger
    4. If Slack message is in an active incident thread, load that incident context first
    """,
    tools=[slack_mcp, jira_mcp, audit_tool],
    sub_agents=[sentinel, investigator, rca_writer, remediation, validator],
)
```

---

### Agent 2: Sentinel Agent

**Role:** Continuous monitor. Polls Arize Phoenix and Redshift `tl_eta_datamart` every 5 minutes. Detects drift, performance degradation, volume anomalies. Calculates severity.

**Tools:** `arize_mcp`, `redshift_adapter`, `audit_tool`

```python
sentinel_agent = LlmAgent(
    name="SentinelAgent",
    model="gemini-2.5-flash",
    instruction="""
    You are the Sentinel Agent. Monitor ML model health using Arize Phoenix
    and Redshift. Config drives which service, which features, which thresholds.

    Each monitoring cycle:
    1. Query Arize Phoenix for recent prediction spans
    2. Query Redshift tl_eta_datamart for latest MAE by shipper + haul bucket
    3. Compute PSI for each feature listed in config:
       - Compare current distribution against training baseline from S3
       - PSI > warning_threshold = WARNING
       - PSI > critical_threshold = CRITICAL
    4. Check performance metrics per segment (haul_type_bucket, shipper_id)
       - MAE > 1.5x rolling 7-day avg = WARNING
       - MAE > 2x rolling 7-day avg = CRITICAL
    5. Check prediction volume — drop > 50% from prior day = WARNING
    6. Check null rate — above 3% = WARNING

    Return:
    {
      "status": "HEALTHY | WARNING | CRITICAL",
      "anomalies": [...],
      "severity_score": 0.0-1.0,
      "recommended_action": "MONITOR | INVESTIGATE | ALERT_HUMAN"
    }
    """,
    tools=[arize_mcp_toolset, redshift_adapter, audit_tool],
)
```

**PSI calculation:**
```python
def calculate_psi(baseline_dist: list, current_dist: list) -> float:
    """
    PSI < 0.1:    No significant change
    PSI 0.1-0.25: Moderate change, monitor
    PSI > 0.25:   Major change, investigate immediately
    """
    baseline_pct = np.array(baseline_dist) / sum(baseline_dist)
    current_pct = np.array(current_dist) / sum(current_dist)
    baseline_pct = np.where(baseline_pct == 0, 0.0001, baseline_pct)
    current_pct = np.where(current_pct == 0, 0.0001, current_pct)
    psi = np.sum((current_pct - baseline_pct) * np.log(current_pct / baseline_pct))
    return round(psi, 4)
```

---

### Agent 3: Investigator Agent

**THE CORE DIFFERENTIATOR.** When Sentinel raises an anomaly, the Investigator does in 10 minutes what an engineer does manually in hours. It cross-references five data sources simultaneously.

**Tools:** `arize_mcp`, `redshift_adapter`, `s3_adapter`, `mlflow_tool`, `github_mcp`, `audit_tool`

```python
investigator_agent = LlmAgent(
    name="InvestigatorAgent",
    model="gemini-2.5-pro",
    instruction="""
    You are the Investigator Agent. Perform forensic analysis when Sentinel
    detects an anomaly. You have access to 5 data sources — use all of them.

    Investigation steps:
    1. Run diagnostic queries against Redshift tl_eta_datamart:
       - Haul bucket breakdown (Q1): which haul types are worst affected?
       - Haul × decile breakdown (Q3): is early or late journey worse?
       - Daily trend (Q4): exactly when did MAE start rising?

    2. Query Arize Phoenix for span-level detail:
       - Which features have the highest PSI scores?
       - Are null rates or out-of-range values elevated?
       - What does prediction distribution look like vs baseline?

    3. Read training data from S3 for affected shippers:
       - What was the feature distribution at training time?
       - Compute PSI against current inference distribution
       - Flag any feature where training vs inference distributions diverge

    4. Query MLflow for model lineage:
       - Which run produced the currently deployed model?
       - What was the training data date range and S3 path?
       - What validation MAE did it achieve at training time?

    5. Query GitHub for recent code changes:
       - List PRs merged in the 48h window before drift started
       - For each PR that touched inference paths or high_risk_files,
         read the full diff
       - Read current inference code (inference_paths from config)
       - Read current training code (training_paths from config)
       - Cross-reference: does training code match inference code's
         assumptions? Look for limits, filters, truncations

    6. Synthesise findings:
       - root_cause_hypothesis (ordered by confidence)
       - affected_cohorts and segments
       - drift_start_timestamp (exact)
       - features_ranked_by_psi
       - relevant_pr_or_commit (if found)
       - estimated_hourly_business_impact

    The Repo Context document is provided in your context window.
    Use it to understand the codebase shape before reading GitHub.
    """,
    tools=[arize_mcp_toolset, redshift_adapter, s3_adapter,
           mlflow_tool, github_mcp, graphify_mcp, audit_tool],
)
```

---

### Agent 4: RCA Writer Agent

**THE NOVELTY HOOK.** Translates all 5 data sources of forensic findings into a plain-English incident report written for a business audience. This is what engineers read in Slack at 2am and what gets attached to Jira.

```python
rca_writer_agent = LlmAgent(
    name="RCAWriterAgent",
    model="gemini-2.5-pro",
    instruction="""
    You are the RCA Writer Agent. Translate the Investigator's findings
    into a clear, actionable incident report for a business audience.

    Rules:
    1. Lead with business impact — minutes of error, loads affected, risk level
    2. Explain cause in one sentence a non-technical person understands
    3. Include a "what changed and when" timeline
    4. If a specific PR or commit is identified, name it explicitly with a link
    5. Propose exactly 3 remediation options ranked by speed vs thoroughness

    Output format (this goes directly into Slack and Jira):

    ## Airen Incident Report #[ID]
    **Severity:** CRITICAL / HIGH / MEDIUM
    **Detected:** [timestamp]
    **Service:** [service name]

    ### What happened
    [1-2 sentences, plain English]

    ### Why it happened
    [Technical cause, explained simply. Name the PR/commit if found.]

    ### Timeline
    [When did the code change? When did drift start? When detected?]

    ### Affected
    [Which models, which shippers, which haul types, which cohorts]

    ### Proposed options
    Option 1 (Fastest): [description] — ~X minutes
    Option 2 (Recommended): [description] — ~X hours
    Option 3 (Thorough): [description] — ~X days

    ### Recommendation
    [Single recommendation with one-line justification]

    CRITICAL: This goes to a human who must decide. Make it credible and actionable.
    """,
    tools=[audit_tool],
)
```

---

### Agent 5: Remediation Agent

**Role:** Executes the approved fix. Only runs when `approval_status == "APPROVED"` is present in input. Triggers retraining pipeline, manages shadow deployment, logs every step.

```python
remediation_agent = LlmAgent(
    name="RemediationAgent",
    model="gemini-2.5-pro",
    instruction="""
    You are the Remediation Agent. Execute the approved fix ONLY after
    receiving explicit human approval from Slack (approval_status == "APPROVED").

    HARD RULE: If approval_status is not "APPROVED", do nothing and return.

    Remediation workflow:
    1. Verify human approval token is present
    2. Log remediation start with timestamp and approver identity
    3. Execute the approved option:
       Option 1 (hotfix): open PR with code change, notify team
       Option 2 (retrain): trigger auto-trainer pipeline for affected shippers
       Option 3 (rollback): revert to prior model version via shadow deploy
    4. For retraining: monitor training job to completion
    5. Deploy to shadow (10% traffic)
    6. Notify ValidatorAgent that shadow is ready
    7. Log every action — Slack thread reply + Jira comment at each step
    """,
    tools=[auto_trainer_tool, shadow_deploy_tool, github_tool, audit_tool],
)
```

---

### Agent 6: Validator Agent

**Role:** Confirms the fix worked. Runs two phases — immediate Kafka check and deferred Redshift accuracy check. Makes go/no-go decision on full promotion.

```python
validator_agent = LlmAgent(
    name="ValidatorAgent",
    model="gemini-2.5-pro",
    instruction="""
    You are the Validator Agent. Confirm the fix worked before full promotion.
    You run two phases.

    PHASE 1 — Immediate (Kafka, within 30 minutes of deploy):
    Read last N predictions from Kafka topic for the deployed model version.
    Check:
    - Volume: predictions flowing at expected rate?
    - Distribution: predicted ETA values in sane range?
    - Null rate: below 3%?
    - Identity: model_family / model_sub_type match deployed version?
    - Sanity bounds: no negative ETAs, no values > 5000 min?
    Return: PASS / FAIL / INCONCLUSIVE

    PHASE 2 — Deferred (Redshift, 24h after deploy):
    Query tl_eta_datamart. Compare new model against pre-incident baseline.
    Run same diagnostic queries as Investigator (Q1, Q3, Q4).
    Check:
    - MAE must recover to within 10% of pre-incident baseline
    - No haul bucket should be worse than baseline
    - PSI on previously-drifted features must drop below 0.15

    On Phase 2 PASS: recommend full promotion + close incident
    On Phase 2 FAIL: recommend rollback + escalate to human via Slack

    Report Slack thread update and Jira comment after each phase.
    """,
    tools=[kafka_adapter, redshift_adapter, arize_mcp_toolset, audit_tool],
)
```

---

## 6. Arize Phoenix MCP — Exact Tool Usage

### Setup

```bash
pip install arize-phoenix arize-phoenix-otel arizeai-phoenix-mcp
python -m phoenix.server.main serve --host 0.0.0.0 --port 6006
```

### MCP Server Configuration for ADK

```python
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StdioConnectionParams
from mcp import StdioServerParameters

arize_mcp_toolset = MCPToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="python",
            args=["-m", "phoenix.server.mcp"],
            env={
                "PHOENIX_HOST": "localhost",
                "PHOENIX_PORT": "6006",
            }
        )
    ),
    tool_filter=["get_spans", "get_traces", "get_datasets",
                 "get_experiments", "create_dataset", "get_annotations"]
)
```

### Logging TL ETA Predictions to Phoenix

```python
import phoenix as px
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

tracer_provider = TracerProvider()
tracer_provider.add_span_processor(
    SimpleSpanProcessor(OTLPSpanExporter(
        endpoint="http://localhost:6006/v1/traces"
    ))
)
trace_api.set_tracer_provider(tracer_provider)
tracer = trace_api.get_tracer("tl_eta_prediction")

def log_eta_prediction(
    load_id: str,
    features: dict,
    predicted_eta_minutes: float,
    actual_eta_minutes: float = None,
    model_family: str = "transformer",
    model_sub_type: str = "localized_v2",
    shipper_id: str = None,
):
    with tracer.start_as_current_span("eta_prediction") as span:
        span.set_attribute("model.family", model_family)
        span.set_attribute("model.sub_type", model_sub_type)
        span.set_attribute("entity.load_id", load_id)
        span.set_attribute("entity.shipper_id", shipper_id or "unknown")
        for key, val in features.items():
            span.set_attribute(f"feature.{key}", str(val))
        span.set_attribute("output.predicted_eta_minutes", predicted_eta_minutes)
        if actual_eta_minutes is not None:
            span.set_attribute("actual.eta_minutes", actual_eta_minutes)
            error = abs(predicted_eta_minutes - actual_eta_minutes)
            span.set_attribute("error.abs_minutes", error)
```

### Querying Phoenix via MCP

```python
# Sentinel: check recent predictions
result = await arize_mcp_toolset.call_tool(
    "get_spans",
    {
        "project_name": "tl_eta_prediction",
        "start_time": "2026-02-19T12:00:00Z",
        "end_time": "2026-02-19T13:00:00Z",
        "span_kind": "CHAIN",
        "limit": 1000
    }
)

# Investigator: check training baseline dataset
baseline = await arize_mcp_toolset.call_tool(
    "get_datasets",
    {
        "project_name": "tl_eta_prediction",
        "dataset_name": "training_baseline_transformer_v2"
    }
)
```

### The Killer Feature — Observing the Agents with Phoenix

> **This is what wins with Arize judges.** Most projects send ML model predictions to Phoenix. Airen does something nobody has done before: it uses Phoenix to observe the AI agents themselves as they debug the ML model. Phoenix becomes the single pane of glass for both the ML system AND the agent system monitoring it.

**Two Phoenix projects, one story:**

```
phoenix project: tl_eta_prediction      ← the ML model being monitored
phoenix project: airen_agents           ← the AI agents doing the monitoring
```

**Every agent action is a Phoenix span:**

```python
from opentelemetry import trace as otel_trace

airen_tracer = otel_trace.get_tracer("airen_agents")

# Inside Sentinel — every drift check is a span
def check_drift(service_name: str, window_minutes: int = 5):
    with airen_tracer.start_as_current_span("sentinel.drift_check") as span:
        span.set_attribute("service.name", service_name)
        span.set_attribute("window.minutes", window_minutes)
        psi = compute_psi(...)
        span.set_attribute("metric.psi", psi)
        span.set_attribute("outcome", "CRITICAL" if psi > 0.25 else "OK")
        return psi

# Inside Investigator — each tool call is a child span
def run_pr_bisect(repo: str, drift_start: str):
    with airen_tracer.start_as_current_span("investigator.pr_bisect") as span:
        span.set_attribute("repo", repo)
        span.set_attribute("drift_start", drift_start)
        pr = find_causal_pr(repo, drift_start)
        span.set_attribute("found.pr_number", pr["number"])
        span.set_attribute("found.confidence", pr["confidence"])
        return pr

# Inside Communicator — human decisions are annotated back to Phoenix
def record_human_decision(incident_id: str, decision: str, approved_by: str):
    with airen_tracer.start_as_current_span("communicator.human_decision") as span:
        span.set_attribute("incident.id", incident_id)
        span.set_attribute("decision", decision)           # approve / reject / investigate_more
        span.set_attribute("approved_by", approved_by)    # slack user id
        span.set_attribute("timestamp", datetime.utcnow().isoformat())
```

**What Phoenix shows for a single Fritolay incident:**

```
airen_agents project — Incident #001 trace:
├── sentinel.drift_check           [2.1s]  PSI=0.31, outcome=CRITICAL
├── investigator.pr_bisect         [8.4s]  PR #621, confidence=HIGH
│   ├── github.list_commits        [1.2s]  12 commits in window
│   ├── graphify.find_functions    [0.8s]  fetch_checkcalls, build_sequence
│   └── github.get_file_contents   [0.6s]  api_fetch_limit=73 found
├── investigator.code_compare      [3.1s]  training=unlimited, inference=73 → MISMATCH
├── communicator.slack_alert       [0.4s]  message sent to #ml-ops
├── communicator.human_decision    [847s]  approved by @akash
├── executor.rollback              [12.3s] PR #624 opened and merged
└── validator.phase1_kafka         [6.2s]  MAE recovering, distribution normal
```

**Phoenix Evals — auto-scoring every RCA:**

```python
import phoenix as px
from phoenix.evals import llm_classify

# After every investigation, score the quality of the root cause analysis
def evaluate_rca(rca_text: str, actual_fix: str) -> dict:
    result = llm_classify(
        dataframe=pd.DataFrame([{
            "rca": rca_text,
            "fix": actual_fix
        }]),
        template="""
        You are evaluating a root cause analysis for an ML model failure.
        RCA: {rca}
        Actual fix applied: {fix}

        Score on:
        1. Accuracy (did the RCA identify the real cause?)
        2. Actionability (was the suggested fix implementable?)
        3. Completeness (were all affected components identified?)

        Respond with: accurate/inaccurate, actionable/not_actionable, complete/incomplete
        """,
        model=OpenAIModel(model="gpt-4o"),  # or Gemini
        rails=["accurate", "inaccurate"]
    )
    # Log eval result back to Phoenix as span annotation
    px.Client().log_evaluations(
        SpanEvaluations(eval_name="rca_quality", dataframe=result)
    )
    return result
```

**Incident datasets — pattern memory:**

```python
# After every resolved incident, snapshot the pre/post spans as a Phoenix dataset
def create_incident_dataset(incident_id: str, drift_start: str, resolved_at: str):
    client = px.Client()

    pre_spans = client.query_spans(
        project_name="tl_eta_prediction",
        start_time=drift_start - timedelta(days=7),
        end_time=drift_start
    )
    post_spans = client.query_spans(
        project_name="tl_eta_prediction",
        start_time=resolved_at,
        end_time=resolved_at + timedelta(hours=24)
    )

    client.upload_dataset(
        name=f"incident_{incident_id}_pre_drift",
        dataframe=pre_spans,
        description="Spans before the Fritolay drift event"
    )
    client.upload_dataset(
        name=f"incident_{incident_id}_post_fix",
        dataframe=post_spans,
        description="Spans after rollback + validation"
    )
```

Future Sentinel agents query these datasets to ask: *"Does this new drift pattern match a known incident?"* — Phoenix becomes Airen's memory.

**The demo line that lands with Arize judges:**
> *"We don't just send our ML model's predictions to Phoenix. We send every decision our AI agents make to Phoenix too. Phoenix is the observability layer for both the model and the agents monitoring the model. One tool. Complete picture."*

---

## 7. Data Source Adapters

### Redshift Adapter

Used by Sentinel (monitoring queries), Investigator (diagnostic queries), and Validator (Phase 2 accuracy check).

```python
import boto3
import psycopg2
from google.adk.tools import FunctionTool

def run_redshift_query(sql: str, params: dict = None) -> dict:
    """
    Execute a SQL query against Redshift and return results.
    Connection config loaded from Secret Manager.
    """
    creds = get_secret("airen/tl-eta/redshift-creds")
    conn = psycopg2.connect(
        host=creds["host"],
        port=creds["port"],
        dbname=creds["database"],
        user=creds["username"],
        password=creds["password"],
    )
    cursor = conn.cursor()
    cursor.execute(sql, params or {})
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    conn.close()
    return {"columns": columns, "rows": [dict(zip(columns, r)) for r in rows]}

redshift_adapter = FunctionTool(func=run_redshift_query)
```

**Diagnostic queries the Investigator runs automatically:**

```sql
-- q1_haul_bucket.sql — which haul types are affected?
SELECT
    haul_type_bucket,
    COUNT(*) as load_count,
    AVG(abs_error_minutes) as avg_mae,
    AVG(AVG(abs_error_minutes)) OVER (
        PARTITION BY haul_type_bucket
        ORDER BY DATE_TRUNC('day', load_delivered_at)
        ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING
    ) as rolling_7day_mae
FROM public.tl_eta_datamart
WHERE load_delivered_at >= CURRENT_DATE - 14
    AND model_family = '{model_family}'
GROUP BY haul_type_bucket, DATE_TRUNC('day', load_delivered_at)
ORDER BY haul_type_bucket, 3 DESC;
```

### S3 Adapter

Used by the Investigator to compare training data distributions against inference distributions.

```python
import boto3
import pandas as pd
import numpy as np
from google.adk.tools import FunctionTool

def get_training_feature_stats(
    shipper_id: str,
    feature_name: str,
    s3_path_template: str
) -> dict:
    """
    Read training parquet from S3 and return feature distribution stats.
    Used to compute PSI: training distribution vs current inference distribution.
    """
    s3_path = s3_path_template.format(shipper=shipper_id)
    bucket = s3_path.split("/")[2]
    key = "/".join(s3_path.split("/")[3:])

    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=bucket, Key=key)
    df = pd.read_parquet(obj["Body"])

    col = df[feature_name].dropna()
    return {
        "shipper": shipper_id,
        "feature": feature_name,
        "count": len(col),
        "mean": float(col.mean()),
        "std": float(col.std()),
        "p25": float(col.quantile(0.25)),
        "p50": float(col.quantile(0.50)),
        "p75": float(col.quantile(0.75)),
        "p95": float(col.quantile(0.95)),
        "null_rate": float(df[feature_name].isna().mean()),
        "distribution": col.tolist()[:1000],  # sample for PSI computation
    }

s3_adapter = FunctionTool(func=get_training_feature_stats)
```

### MLflow Adapter

Used by Investigator (model lineage for root cause) and Validator (training-time benchmark for comparison).

```python
import mlflow
from google.adk.tools import FunctionTool

def get_latest_model_run(
    experiment_name: str,
    model_family: str,
    shipper_id: str = None
) -> dict:
    """
    Fetch the most recent MLflow run for a given model.
    Returns training data path, date range, validation MAE, parameters.
    """
    mlflow.set_tracking_uri(get_secret("airen/tl-eta/mlflow-uri"))
    experiment = mlflow.get_experiment_by_name(experiment_name)
    runs = mlflow.search_runs(
        experiment_ids=[experiment.experiment_id],
        filter_string=f"tags.model_family = '{model_family}'",
        order_by=["start_time DESC"],
        max_results=1,
    )
    if runs.empty:
        return {"error": "no runs found"}

    run = runs.iloc[0]
    return {
        "run_id": run["run_id"],
        "start_time": str(run["start_time"]),
        "training_data_path": run.get("params.training_data_path"),
        "training_data_cutoff": run.get("params.data_cutoff_date"),
        "val_mae": run.get("metrics.val_mae"),
        "val_mae_3long": run.get("metrics.val_mae_3long"),
        "sequence_length": run.get("params.sequence_length"),
        "model_artifact_uri": run["artifact_uri"],
    }

mlflow_tool = FunctionTool(func=get_latest_model_run)
```

---

## 8. GitHub Repo Integration

### How It Works

The Investigator uses two complementary tools to understand a codebase:

1. **Graphify** — builds a semantic + structural knowledge graph of the entire repo at onboarding. The Investigator queries this graph to understand architecture, call relationships, and where specific parameters are used — without reading raw files.
2. **GitHub MCP** — fetches live data: recent commits, PR diffs, and full file bodies when the graph points to a specific location.

This combination gives the Investigator a pre-built mental model (Graphify) and on-demand file access (GitHub MCP). Together they replace both the hand-crafted Repo Context Document and the custom AST map.

### Graphify Setup

```bash
# Install once, run at onboarding
uv tool install graphifyy

# Run against the service repo (cloned or via GitHub)
graphify ./dynamic-eta-prediction --mode deep --output ./services/tl-eta/graph/
```

This generates three files in `services/tl-eta/graph/`:
- `graph.json` — queryable graph (nodes = functions/classes/constants, edges = calls/imports/references)
- `graph.html` — interactive visual (embed in Airen web UI for judges)
- `GRAPH_REPORT.md` — auto-written summary of architecture, god nodes, surprising connections

**`GRAPH_REPORT.md` replaces the hand-crafted Repo Context Document entirely.** It auto-identifies god nodes like `api_fetch_limit` (referenced in 5 files) and documents the gap: present in inference path, absent in training path.

### Graphify MCP Integration

```python
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StdioConnectionParams
from mcp import StdioServerParameters

graphify_mcp = MCPToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="graphify",
            args=["serve", "--graph", "./services/tl-eta/graph/graph.json"],
        )
    ),
)
```

**What the Investigator can query via Graphify MCP:**

```python
# "Where is api_fetch_limit used?"
result = await graphify_mcp.call_tool("query", {
    "question": "find all references to api_fetch_limit"
})
# Returns: helper.py:47 (EXTRACTED), main.py:23 (EXTRACTED),
#          LSTMConstants.py:12 (EXTRACTED)
#          NOT IN: train.py, test.py  ← the smoking gun

# "What calls _prepare_fetch_location_url?"
result = await graphify_mcp.call_tool("query", {
    "question": "what calls _prepare_fetch_location_url"
})
# Returns: fetch_historical_checkcalls in helper.py (EXTRACTED, confidence: high)

# "Show me the data flow from API call to prediction"
result = await graphify_mcp.call_tool("query", {
    "question": "trace data flow from LocationService to model prediction"
})
```

**Confidence levels on every result:**
- `EXTRACTED` — explicit in code (explicit call, import, definition)
- `INFERRED` — deduced from patterns
- `AMBIGUOUS` — uncertain, verify manually

### Keeping the Graph Current

```yaml
# In airen.yaml — triggers graph rebuild on PR merge via GitHub Actions
code_repo:
  graphify:
    enabled: true
    mode: deep
    output_path: services/tl-eta/graph/
    rebuild_on_merge: true   # GitHub Action calls graphify --update after each merge
```

```yaml
# .github/workflows/airen-graph-update.yml
name: Update Airen Knowledge Graph
on:
  push:
    branches: [main]
jobs:
  update-graph:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: uv tool install graphifyy
      - run: graphify . --mode deep --update --output ./airen-graph/
      - uses: actions/upload-artifact@v4
        with:
          name: airen-graph
          path: ./airen-graph/
```

The graph stays current after every PR merge. When the Investigator runs post-incident, it's always working with a graph that includes the latest code changes.

### GitHub MCP Setup

```python
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StdioConnectionParams
from mcp import StdioServerParameters

github_mcp = MCPToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            env={"GITHUB_PERSONAL_ACCESS_TOKEN": get_secret("airen/org/github-pat")}
        )
    ),
    tool_filter=[
        "list_commits",
        "get_commit",
        "get_file_contents",
        "list_pull_requests",
        "search_code",
        "get_pull_request",
    ]
)
```

### Investigation Flow

When Investigator identifies drift starting at timestamp T:

```python
# Step 1: PRs merged in 48h window before drift
prs = await github_mcp.call_tool("list_pull_requests", {
    "owner": "fourkites",
    "repo": "dynamic-eta-prediction",
    "state": "closed",
    "base": "main",
    "sort": "updated",
    "direction": "desc",
})
# Filter: merged_at between T-48h and T

# Step 2: For each PR touching high_risk_files
commit = await github_mcp.call_tool("get_commit", {
    "owner": "fourkites",
    "repo": "dynamic-eta-prediction",
    "ref": pr["merge_commit_sha"]
})
# Inspect diff — look for limit=, filter=, truncat=, cap=

# Step 3: Read current inference code
inference_code = await github_mcp.call_tool("get_file_contents", {
    "owner": "fourkites",
    "repo": "dynamic-eta-prediction",
    "path": "predict/processing/location_service/helper.py",
})

# Step 4: Read training code — same concept
training_code = await github_mcp.call_tool("get_file_contents", {
    "owner": "fourkites",
    "repo": "dynamic-eta-prediction",
    "path": "experiments/fritolay_based_model/train.py",
})

# Step 5: Search for key pattern across repo
hits = await github_mcp.call_tool("search_code", {
    "q": "api_fetch_limit repo:fourkites/dynamic-eta-prediction",
})
```

### Full Investigator Tool Stack

```
Investigator Agent
├── Graphify MCP       → semantic + structural knowledge graph (architecture, call graph, god nodes)
├── GRAPH_REPORT.md    → auto-generated repo summary injected as context at investigation start
├── GitHub MCP         → live PR bisect, diffs, full file bodies on demand
├── Arize Phoenix MCP  → live prediction traces, span-level feature values
├── Redshift Adapter   → tl_eta_datamart diagnostic queries
├── S3 Adapter         → training parquet feature distributions
└── MLflow Tool        → current model lineage, training run metrics
```

The Investigator follows this decision path:

```
1. Read GRAPH_REPORT.md → understand repo shape
2. Query Graphify MCP → "where is sequence capping happening?"
3. Graphify: "helper.py:47, main.py:23 — NOT in train.py" ← smoking gun found
4. GitHub MCP: list_pull_requests in drift window → find PR #621
5. GitHub MCP: get_commit(PR#621) → confirm diff touches helper.py
6. GitHub MCP: get_file_contents(train.py) → confirm no equivalent limit
7. S3 Adapter: training seq_len avg 184 pings
8. Kafka / Phoenix: inference seq_len avg 73 pings
9. Confirmed: training-inference mismatch via PR #621
```

Graphify cuts steps 3–6 from "read 4 files, build mental model" to "one graph query, answer in seconds."

---

## 9. Two-Phase Validation — Kafka + Redshift

After any remediation, the Validator runs two phases with different timing and data sources.

### Phase 1 — Immediate Health Check (Kafka, T+5 to T+30 min)

Uses a stateless Kafka peek — no consumer group, no committed offsets.

```python
from confluent_kafka import Consumer, TopicPartition, OFFSET_END
from google.adk.tools import FunctionTool
import json

def kafka_peek_latest_predictions(
    topic: str,
    model_family: str,
    model_sub_type: str,
    n_messages: int = 500,
) -> dict:
    """
    Stateless read of the last N predictions for a specific model version.
    Does not join a consumer group. Does not commit offsets.
    Existing consumers are unaffected.
    """
    conf = {
        "bootstrap.servers": get_secret("airen/org/kafka-bootstrap"),
        "group.id": f"airen-validator-peek-{uuid4()}",  # unique, never reused
        "auto.offset.reset": "latest",
        "enable.auto.commit": False,
    }
    consumer = Consumer(conf)
    metadata = consumer.list_topics(topic)
    partitions = [
        TopicPartition(topic, p, OFFSET_END - n_messages)
        for p in metadata.topics[topic].partitions
    ]
    consumer.assign(partitions)

    messages = []
    for _ in range(n_messages * 2):
        msg = consumer.poll(timeout=1.0)
        if msg is None:
            break
        payload = json.loads(msg.value())
        if (payload.get("model_family") == model_family and
                payload.get("model_sub_type") == model_sub_type):
            messages.append(payload)

    consumer.close()

    if not messages:
        return {"error": "no predictions found for this model version"}

    predictions = [m["predicted_eta_minutes"] for m in messages
                   if m.get("predicted_eta_minutes") is not None]
    null_rate = 1 - len(predictions) / len(messages)

    return {
        "count": len(messages),
        "null_rate": round(null_rate, 4),
        "mean": round(np.mean(predictions), 2),
        "p50": round(np.percentile(predictions, 50), 2),
        "p95": round(np.percentile(predictions, 95), 2),
        "min": round(min(predictions), 2),
        "max": round(max(predictions), 2),
        "model_identity_confirmed": all(
            m.get("model_family") == model_family for m in messages
        ),
    }

kafka_adapter = FunctionTool(func=kafka_peek_latest_predictions)
```

**Phase 1 checks:**

| Check | Pass condition |
|---|---|
| Volume | Predictions flowing at expected rate |
| Distribution | Mean within 20% of pre-incident mean |
| Null rate | Below 3% |
| Model identity | `model_family` and `model_sub_type` match deployed version |
| Sanity bounds | No negatives, no values above 5000 |

### Phase 2 — Deferred Accuracy Check (Redshift, T+24h)

```python
# Phase 2 runs when tl_eta_datamart updates (daily)
# Compares new model against PRE-INCIDENT baseline (not pre-fix)
PHASE2_SQL = """
SELECT
    haul_type_bucket,
    DATE_TRUNC('day', load_delivered_at) as date,
    AVG(abs_error_minutes) as avg_mae,
    COUNT(*) as loads
FROM public.tl_eta_datamart
WHERE load_delivered_at >= '{baseline_start}'
    AND model_family = '{model_family}'
GROUP BY 1, 2
ORDER BY 1, 2
"""
```

Phase 2 PASS requires:
- MAE recovered to within 10% of pre-incident baseline across all haul buckets
- No shipper has regressed vs baseline
- PSI on previously-drifted features has dropped below 0.15

---

## 10. Slack Integration — Approvals + Chat Interface

All approvals happen in Slack. The web UI becomes a read-only audit view.

### Alert Message (Block Kit)

```python
from slack_sdk import WebClient

def post_incident_alert(incident: dict, channel_id: str):
    client = WebClient(token=get_secret("airen/org/slack-bot-token"))
    client.chat_postMessage(
        channel=channel_id,
        blocks=[
            {
                "type": "header",
                "text": {"type": "plain_text",
                         "text": f"🔴 Airen INCIDENT #{incident['id']} — {incident['service']}"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Severity:* {incident['severity']}"},
                    {"type": "mrkdwn", "text": f"*Detected:* {incident['detected_at']}"},
                    {"type": "mrkdwn", "text": f"*Affected:* {incident['affected_cohort']}"},
                    {"type": "mrkdwn", "text": f"*Impact:* {incident['impact_summary']}"},
                ]
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Root Cause*\n{incident['rca_summary']}"}
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn",
                         "text": f"*Option 1 (Fast):* {incident['option1']} — ~{incident['option1_time']}\n"
                                 f"*Option 2 (Recommended):* {incident['option2']} — ~{incident['option2_time']}"}
            },
            {
                "type": "actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve Option 1"},
                     "style": "primary",
                     "action_id": "approve_option_1",
                     "value": incident["id"]},
                    {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve Option 2"},
                     "style": "primary",
                     "action_id": "approve_option_2",
                     "value": incident["id"]},
                    {"type": "button", "text": {"type": "plain_text", "text": "🎫 Create Jira"},
                     "action_id": "create_jira",
                     "value": incident["id"]},
                    {"type": "button", "text": {"type": "plain_text", "text": "❌ Reject"},
                     "style": "danger",
                     "action_id": "reject",
                     "value": incident["id"]},
                ]
            }
        ]
    )
```

### Slack Chat Interface

Engineers can message the bot directly — in DMs, in channels, or in incident threads.

```python
# Cloud Run webhook handler for Slack Events API
@app.post("/webhook/slack/events")
async def slack_events(request: Request):
    payload = await request.json()

    # Slack challenge verification
    if "challenge" in payload:
        return {"challenge": payload["challenge"]}

    event = payload.get("event", {})
    if event.get("type") not in ("message", "app_mention"):
        return {}

    message_text = event.get("text", "")
    channel = event.get("channel")
    thread_ts = event.get("thread_ts")  # None if top-level
    user = event.get("user")

    # Load incident context if message is in an incident thread
    incident_context = None
    if thread_ts:
        incident_context = get_incident_by_thread(thread_ts)

    # Route to Orchestrator in conversational mode
    response = await orchestrator.chat(
        message=message_text,
        user=user,
        channel=channel,
        thread_ts=thread_ts,
        incident_context=incident_context,
    )

    # Reply in thread
    client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts or event["ts"],
        text=response,
    )
    return {}
```

### Example Slack Conversations

```
Akash: @airen what's the current health of tl-eta?

Airen: tl-eta-prediction is HEALTHY ✅
      Last check: 2 minutes ago
      MAE (7-day rolling): 47 min — within normal range
      Predictions last hour: 1,247 — volume normal
      Active incidents: none
      All 6 shippers nominal.

---

Akash: @airen why was Fritolay accuracy bad last week?

Airen: Let me pull the investigation... 🔍
      PR #621 (merged Feb 19, 12:32 IST) introduced a 73-ping
      cap on checkcall history at inference. Training used full
      history (~184 pings). Long-haul early-journey predictions
      worst affected (+109% MAE on 3_long).
      Fixed via hotfix PR #624 at 13:02 IST. Validated next morning.
      See: #ml-eta-incidents thread from Feb 19 | Jira: ETA-1821

---

Priya (in incident thread): are other shippers affected?

Airen: Checked now — DOW and Smithfield both clean.
      MAE within normal bounds, PSI flat on all features.
      This is isolated to Fritolay. Safe to approve. 👍
```

---

## 11. Jira Integration — Full Ticket Lifecycle

### Setup (Atlassian MCP)

```python
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset

jira_mcp = MCPToolset(
    connection_params=...,  # Atlassian MCP connection
    tool_filter=[
        "createJiraIssue",
        "addCommentToJiraIssue",
        "editJiraIssue",
        "getJiraIssue",
        "searchJiraIssuesUsingJql",
        "transitionJiraIssue",
        "getTransitionsForJiraIssue",
    ]
)
```

### Check for Existing Ticket Before Creating

```python
# Investigator checks if a matching ticket already exists
existing = await jira_mcp.call_tool("searchJiraIssuesUsingJql", {
    "jql": f"""
        project = ETA
        AND labels in (airen, auto-detected)
        AND status != Done
        AND text ~ "Fritolay"
        ORDER BY created DESC
    """,
    "maxResults": 5,
})
# If found: comment on existing ticket
# If not found: wait for human approval, then create
```

### Ticket Creation

```python
ticket = await jira_mcp.call_tool("createJiraIssue", {
    "projectKey": "ETA",
    "summary": f"[Airen-{incident_id}] {incident['title']}",
    "issueType": "Bug",
    "priority": "High",
    "labels": ["airen", "auto-detected", "tl-eta"],
    "description": f"""
## Summary
Detected by Airen at {incident['detected_at']}.

## Root Cause
{incident['rca_full']}

## Investigation Findings
- MAE on 3_long: {incident['mae_before']} min → {incident['mae_after']} min
- Affected: {incident['affected_cohort']}
- Drift start: {incident['drift_start']}
- Cause identified: {incident['root_cause_pr']}

## Proposed Fix
{incident['approved_option']}

## Approved by
{incident['approver']} at {incident['approval_time']} via Slack
    """
})
```

### Lifecycle Comments (Auto-posted)

```
12:47 IST  [Airen Bot] Incident confirmed. Approved by Akash L. Remediation starting.

12:55 IST  [Airen Bot] Hotfix PR #624 opened:
           https://github.com/fourkites/dynamic-eta-prediction/pull/624

13:02 IST  [Airen Bot] Deploy confirmed. Phase 1 validation (Kafka) starting.

13:12 IST  [Airen Bot] ✅ Phase 1 PASS — predictions flowing, distribution normal,
           null rate 1.2%, model identity confirmed.

Next day   [Airen Bot] ✅ Phase 2 PASS — MAE recovered:
           3_long: 641 min (baseline: 610 min) — within threshold.
           All shippers nominal. Closing ticket.

           Post-mortem summary:
           Detection to approval: 10 min
           Approval to fix deployed: 15 min
           Full resolution (Phase 2): next day
           Automated actions: 12 | Human actions: 1
```

---

## 12. Service Onboarding Flow

New ML team onboards by providing config in layers — each layer unlocking the next set of questions.

### Step 1 — Basic Identity
```
Service name:    tl-eta-prediction
Team name:       ml-eta
Description:     Truck Load ETA prediction — LSTM and Transformer models
Environment:     production
```

### Step 2 — Slack
```
Slack channel ID:    C07AB12XYZ   (#ml-eta-incidents)
Alert level:         critical-only / all-alerts / summary-only
Escalation contact:  akash@fourkites.com
Chat enabled:        yes
```

### Step 3 — Prediction Data Source
```
Type: kafka / arize-phoenix / bigquery / redshift

If kafka:
  Bootstrap servers: kafka.internal:9092
  Topic:             tl_eta_predictions
  Auth secret:       airen/tl-eta/kafka-creds
  Field mappings:
    prediction_value: predicted_eta_minutes
    model_family:     model_family
    entity_id:        load_id
    timestamp:        prediction_timestamp
```

### Step 4 — Ground Truth / Actuals
```
Type: redshift / bigquery / snowflake

If redshift:
  MCP server URL: (if existing MCP running) OR direct connection details
  Table:          public.tl_eta_datamart
  Update freq:    daily
  Auth secret:    airen/tl-eta/redshift-creds
  Field mappings:
    prediction: predicted_delivery_time
    actual:     actual_delivery_time
    error:      abs_error_minutes
    timestamp:  load_delivered_at
    model_id:   model_family
```

### Step 5 — Training Data
```
Type: s3 / gcs / redshift

If s3:
  Bucket:       data-science-4k
  Path pattern: OTR_TL_ETA/unified_new/{shipper}/train_data/
  File format:  parquet
  Auth secret:  airen/tl-eta/s3-creds
  Feature cols: [sequence_length, distance_in_miles, hour_of_day, haul_type]
  Label col:    actual_delivery_minutes
```

### Step 6 — MLflow (Optional)
```
Tracking URI: http://mlflow.internal:5000
Experiment:   tl_eta_transformer
Auth secret:  airen/tl-eta/mlflow-token
```

### Step 7 — GitHub Repo
```
Repo URL:        https://github.com/fourkites/dynamic-eta-prediction
Default branch:  main
Auth secret:     airen/org/github-pat   (org-level, shared)

Inference paths: [predict/processing/, main.py, predict/app_constants.py]
Training paths:  [experiments/, training_pipeline/auto_trainer/]
High-risk files: [predict/processing/location_service/helper.py,
                  predict/processing/lstm/LSTMConstants.py]
Key patterns:    [api_fetch_limit, seq_len, SEQUENCE_LENGTH]
```

→ Airen runs Graphify on the repo, generates `graph/graph.json`, `graph/graph.html`, and `graph/GRAPH_REPORT.md`.

### Step 8 — Monitoring Config
```
Metric:    MAE
Segments:  [haul_type_bucket, decile, shipper_id]
Thresholds:
  warning:  MAE > 1.5x rolling 7-day
  critical: MAE > 2x rolling 7-day

Features to watch:
  - sequence_length  (PSI warning: 0.10, critical: 0.25)
  - distance_in_miles
  - haul_type (categorical)

Diagnostic SQL: (upload files or paste inline)
  - q1_haul_bucket.sql
  - q3_haul_decile.sql
  - q4_daily_trend.sql
```

### Step 9 — Validation Config
```
Phase 1: kafka, window 30 min, min 100 predictions
Phase 2: redshift, delay 24h

Healthy baseline:
  MAE_p50:  45 min
  MAE_p95:  180 min
  null_rate_max: 0.03
```

### Step 10 — Jira
```
Base URL:     https://fourkites.atlassian.net
Project:      ETA
Issue type:   Bug
Auth secret:  airen/org/jira-token
Auto create:  false (always ask)
```

### What Gets Generated

```
services/
└── tl-eta/
    ├── airen.yaml              # Source of truth — safe to commit to Git
    ├── repo_context.md        # Auto-generated from GitHub scan
    └── queries/
        ├── q1_haul_bucket.sql
        ├── q3_haul_decile.sql
        └── q4_daily_trend.sql
```

### Secrets Required

```
airen/tl-eta/kafka-creds       → Kafka SASL credentials
airen/tl-eta/redshift-creds    → Redshift username + password
airen/tl-eta/s3-creds          → AWS access key + secret (or IAM role ARN)
airen/tl-eta/mlflow-token      → MLflow API token
airen/org/github-pat           → GitHub PAT (org-level, shared)
airen/org/slack-bot-token      → Slack bot token (org-level, shared)
airen/org/jira-token           → Atlassian API token (org-level, shared)
```

Credentials never appear in `airen.yaml`. YAML holds only secret name pointers. Safe to commit.

---

## 13. Google Cloud Stack

### Services Required

| Service | Purpose | Cost during hackathon |
|---|---|---|
| **Vertex AI Agent Engine** | Host the 6-agent system | ~$0.01/agent-hour |
| **Cloud Run** | Phoenix server + Web UI + Slack webhook handler | Free tier: 2M req/month |
| **BigQuery** | Audit log, mirrored prediction data for demo | Free tier: 10GB/month |
| **Cloud SQL (PostgreSQL)** | Demo training data snapshot | ~$7/month |
| **Secret Manager** | All credentials | First 6 secrets free |
| **Pub/Sub** | Trigger Sentinel on schedule | Free tier |
| **Cloud Scheduler** | Run Sentinel every 5 min | 3 free jobs/month |

### Deployment

```yaml
# cloudbuild.yaml
steps:
  - name: 'gcr.io/cloud-builders/docker'
    args: ['build', '-t', 'gcr.io/$PROJECT_ID/phoenix-server', './phoenix/']
  - name: 'gcr.io/cloud-builders/gcloud'
    args: ['run', 'deploy', 'phoenix-server',
           '--image', 'gcr.io/$PROJECT_ID/phoenix-server',
           '--port', '6006', '--allow-unauthenticated', '--region', 'us-central1']

  - name: 'gcr.io/cloud-builders/docker'
    args: ['build', '-t', 'gcr.io/$PROJECT_ID/airen-ui', './ui/']
  - name: 'gcr.io/cloud-builders/gcloud'
    args: ['run', 'deploy', 'airen-ui',
           '--image', 'gcr.io/$PROJECT_ID/airen-ui',
           '--port', '8080', '--allow-unauthenticated', '--region', 'us-central1']

  - name: 'gcr.io/cloud-builders/python'
    args: ['python', 'deploy_agents.py']
```

### Vertex AI Agent Engine Deployment

```python
import vertexai
from vertexai.preview import reasoning_engines

vertexai.init(project=PROJECT_ID, location="us-central1")

install_script = """
#!/bin/bash
pip install arize-phoenix arize-phoenix-otel arizeai-phoenix-mcp
pip install psycopg2-binary pandas pyarrow mlflow confluent-kafka slack-sdk
npm install -g @modelcontextprotocol/server-github
"""

airen_app = reasoning_engines.ReasoningEngine.create(
    AirenAgent(),
    requirements=[
        "arize-phoenix>=8.0", "arizeai-phoenix-mcp",
        "psycopg2-binary", "pandas", "pyarrow", "mlflow",
        "confluent-kafka", "slack-sdk",
    ],
    extra_packages=["./airen"],
    build_options={"installation_scripts": ["/tmp/install_airen.sh"]}
)
print(f"Airen deployed: {airen_app.resource_name}")
```

---

## 14. Full Code Scaffolding

### Repository Structure

```
airen/
├── README.md
├── LICENSE                          # Apache 2.0
├── requirements.txt
├── cloudbuild.yaml
├── deploy_agents.py
│
├── core/
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── orchestrator.py
│   │   ├── sentinel.py
│   │   ├── investigator.py
│   │   ├── rca_writer.py
│   │   ├── remediation.py
│   │   └── validator.py
│   │
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── kafka.py                 # Stateless peek
│   │   ├── redshift.py              # Query runner
│   │   ├── s3.py                    # Parquet reader + feature stats
│   │   ├── mlflow.py                # Run fetcher
│   │   ├── github.py                # PR bisect + code reader
│   │   ├── phoenix.py               # Arize Phoenix MCP wrapper
│   │   ├── slack.py                 # Alerts + chat + interactive approvals
│   │   └── jira.py                  # Ticket lifecycle (Atlassian MCP)
│   │
│   └── config/
│       ├── schema.py                # Pydantic model for airen.yaml
│       └── loader.py                # Config loader + secret resolver
│
├── services/                        # One folder per ML service
│   └── tl-eta/                      # Reference implementation
│       ├── airen.yaml
│       ├── graph/
│       │   ├── graph.json
│       │   ├── graph.html
│       │   └── GRAPH_REPORT.md
│       └── queries/
│           ├── q1_haul_bucket.sql
│           ├── q3_haul_decile.sql
│           └── q4_daily_trend.sql
│
├── ui/
│   ├── Dockerfile
│   ├── app.py                       # FastAPI — read-only audit dashboard
│   ├── static/
│   │   ├── index.html
│   │   ├── style.css
│   │   └── app.js                   # SSE for live reasoning trace
│   └── webhook/
│       └── slack.py                 # Slack Events API handler
│
├── phoenix/
│   ├── Dockerfile
│   └── entrypoint.sh
│
├── cli/
│   └── airen.py                      # airen add-service / status / run
│
└── tests/
    ├── test_psi.py
    ├── test_kafka_peek.py
    └── test_agents.py
```

---

## 15. Demo Data Setup — TL ETA Reference Implementation

The demo uses a sanitized snapshot of real `tl_eta_datamart` data from Feb 14–22, 2026 (the Fritolay incident window), mirrored to BigQuery for the hackathon demo. This is far more compelling than synthetic data — it is a real incident.

### Generate Synthetic Phoenix Data (for Arize observability layer)

```python
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

np.random.seed(42)

def generate_eta_predictions(
    n: int,
    start_time: datetime,
    apply_truncation: bool = False,   # True = simulate PR #621 effect
    shipper_id: str = "fritolay",
    model_family: str = "lstm",
) -> pd.DataFrame:
    """
    apply_truncation=False: training-aligned sequences (healthy predictions)
    apply_truncation=True:  inference sees only 73 pings (degraded predictions)
    """
    records = []
    for i in range(n):
        ts = start_time + timedelta(seconds=i * 3.6)
        distance = np.random.lognormal(5.2, 0.8)
        haul_type = "long_haul" if distance > 400 else "short_haul"
        actual_eta = distance * 2.1 + np.random.normal(0, 30)

        if apply_truncation:
            # Truncated history → model is blind to early journey
            # Long-haul worst affected — prediction is systematically off
            if haul_type == "long_haul":
                predicted_eta = actual_eta * np.random.uniform(1.5, 2.5)
            else:
                predicted_eta = actual_eta * np.random.uniform(0.9, 1.3)
            seq_len = np.random.randint(65, 78)   # capped at ~73
        else:
            predicted_eta = actual_eta * np.random.uniform(0.85, 1.15)
            seq_len = np.random.randint(150, 220)  # full history

        records.append({
            "timestamp": ts.isoformat(),
            "load_id": f"load_{i:06d}",
            "shipper_id": shipper_id,
            "model_family": model_family,
            "distance_in_miles": round(distance, 2),
            "haul_type": haul_type,
            "sequence_length": seq_len,
            "predicted_eta_minutes": round(max(0, predicted_eta), 2),
            "actual_eta_minutes": round(max(0, actual_eta), 2),
            "abs_error_minutes": round(abs(predicted_eta - actual_eta), 2),
        })
    return pd.DataFrame(records)


if __name__ == "__main__":
    # 7 days healthy baseline (Feb 12–18)
    baseline_start = datetime(2026, 2, 12)
    healthy_df = generate_eta_predictions(
        n=10000, start_time=baseline_start, apply_truncation=False
    )
    log_to_phoenix(healthy_df)

    # The drift event (Feb 19, 12:32 IST onward)
    drift_start = datetime(2026, 2, 19, 7, 2)  # 12:32 IST = 07:02 UTC
    drift_df = generate_eta_predictions(
        n=3000, start_time=drift_start, apply_truncation=True
    )
    log_to_phoenix(drift_df)
    print("Demo data ready. Sentinel detects drift in next monitoring cycle.")
```

---

## 16. Web UI — Reasoning Trace Dashboard

The web UI is **read-only**. All approvals happen in Slack. The dashboard exists for audit, replay, and visibility.

### What the Dashboard Shows

```
┌──────────────────────────────────────────────────────────────────────┐
│  Airen — ML Reliability Engineer          [LIVE] Services ▾  Feb 19  │
├──────────────────────────────────────────────────────────────────────┤
│  SERVICE: tl-eta-prediction                                           │
│                                                                       │
│  AGENT STATUS                                                         │
│  ┌──────────┬───────────┬───────────┬──────────┬──────────┐         │
│  │ Sentinel │Investigator│ RCA Writer│Remediation│Validator │         │
│  │ ● DONE   │ ● DONE    │ ● DONE    │ ● DONE   │ ● ACTIVE │         │
│  └──────────┴───────────┴───────────┴──────────┴──────────┘         │
│                                                                       │
│  INCIDENT #042                                    🔴 IN PROGRESS     │
│  Fritolay LSTM — MAE doubled on 3_long                               │
│  Approved by: Akash L.  12:47 IST                                   │
│  Fix: Hotfix PR #624 — Remove api_fetch_limit                        │
│  Phase 1: ✅ PASS    Phase 2: ⏳ Waiting (actuals update tomorrow)   │
│                                                                       │
│  REASONING TRACE                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ 12:37 [Sentinel]    MAE 3_long crossed 2x threshold           │   │
│  │ 12:37 [Sentinel]    PSI sequence_length = 0.41 — CRITICAL     │   │
│  │ 12:38 [Investigator] Running Q1 haul bucket query...          │   │
│  │ 12:39 [Investigator] Q4: MAE flat until 12:32, then spikes    │   │
│  │ 12:39 [Investigator] GitHub: 3 PRs merged in window           │   │
│  │ 12:40 [Investigator] PR #621 touched helper.py (HIGH RISK)    │   │
│  │ 12:40 [Investigator] Diff: added limit={api_fetch_limit}      │   │
│  │ 12:41 [Investigator] Training code: no limit found            │   │
│  │ 12:41 [Investigator] S3: training seq len avg 184 pings       │   │
│  │ 12:41 [Investigator] Kafka: inference seq len avg 73 pings    │   │
│  │ 12:43 [RCAWriter]   Generating incident report...             │   │
│  │ 12:44 [Orchestrator] Posted to Slack #ml-eta-incidents        │   │
│  │ 12:47 [Orchestrator] Approval received: Akash L. — Option 1  │   │
│  │ 12:47 [Orchestrator] Jira ETA-1821 created                    │   │
│  │ 12:48 [Remediation] Opening hotfix PR...                      │   │
│  │ 12:55 [Remediation] PR #624 opened                            │   │
│  │ 13:02 [Remediation] Deploy confirmed                          │   │
│  │ 13:02 [Validator]   Phase 1 starting (Kafka peek)             │   │
│  │ 13:12 [Validator]   Phase 1 PASS ✅                           │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                       │
│  AUDIT TRAIL  [Export CSV]  [View in BigQuery]                       │
│  47 events logged · Jira: ETA-1821 · Slack: #ml-eta-incidents       │
└──────────────────────────────────────────────────────────────────────┘
```

### FastAPI Backend

```python
from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import asyncio, json

app = FastAPI(title="Airen Dashboard")
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def dashboard():
    return FileResponse("static/index.html")

@app.get("/api/services")
async def list_services():
    return load_all_service_configs()

@app.get("/api/services/{service}/state")
async def get_service_state(service: str):
    return get_airen_state(service)

@app.get("/api/services/{service}/stream")
async def stream_events(service: str):
    async def event_generator():
        last_len = 0
        while True:
            trace = get_reasoning_trace(service)
            if len(trace) > last_len:
                for event in trace[last_len:]:
                    yield f"data: {json.dumps(event)}\n\n"
                last_len = len(trace)
            await asyncio.sleep(0.5)
    return StreamingResponse(event_generator(), media_type="text/event-stream")

# Slack webhook — interactive button handler
@app.post("/webhook/slack/actions")
async def slack_actions(request: Request, background_tasks: BackgroundTasks):
    payload = json.loads((await request.form())["payload"])
    action = payload["actions"][0]
    incident_id = action["value"]
    action_id = action["action_id"]
    user = payload["user"]["name"]

    if action_id in ("approve_option_1", "approve_option_2"):
        option = "1" if action_id == "approve_option_1" else "2"
        background_tasks.add_task(
            run_remediation, incident_id=incident_id,
            option=option, approver=user
        )
    elif action_id == "create_jira":
        background_tasks.add_task(create_jira_ticket, incident_id=incident_id)
    elif action_id == "reject":
        background_tasks.add_task(escalate_incident, incident_id=incident_id)

    return {"response_action": "clear"}

# Slack Events API — chat messages
@app.post("/webhook/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    if "challenge" in payload:
        return {"challenge": payload["challenge"]}
    event = payload.get("event", {})
    if event.get("type") in ("message", "app_mention"):
        background_tasks.add_task(handle_slack_chat, event=event)
    return {}
```

---

## 17. Technical Deep Dive — Concepts & Build Guide

> **How to use this section:** Read it top to bottom before writing a single line of code. Every concept here maps directly to a file you will create. If a concept feels unclear, re-read it, then ask. The goal is that by the time you touch a keyboard you can explain every moving part without looking at this document.

---

### 17.1 Google ADK — The Agent Framework

**What it is:** Google's Agent Development Kit (ADK) is a Python library that lets you define, run, and connect AI agents. Think of it as the wiring harness — it handles all the plumbing between the LLM, your tools, and the conversation state, so you can focus on what the agent actually does.

**The three things you define for every agent:**

```python
from google.adk.agents import LlmAgent

agent = LlmAgent(
    name="my_agent",
    model="gemini-2.5-pro",    # which LLM brain to use
    instruction="You are...",  # the system prompt — what this agent IS
    tools=[...],               # what this agent CAN DO
    sub_agents=[...],          # agents this agent CAN DELEGATE TO
)
```

- `instruction` is the permanent system prompt. It defines the agent's personality, scope, and decision rules. Think of it as the job description on day one.
- `tools` are the agent's hands — Python functions it can call to interact with the world.
- `sub_agents` are the agents it manages. If a sub_agent is listed, the parent can delegate entire tasks to it.

**Runner — the execution engine:**
```python
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

session_service = InMemorySessionService()
runner = Runner(agent=orchestrator, app_name="airen", session_service=session_service)

# Send a message to start the agent loop
for event in runner.run(user_id="sentinel", session_id="incident-001", new_message=content):
    # events stream in as the agent thinks and acts
    print(event)
```

The Runner owns the conversation loop. It sends your message to the agent, the agent decides whether to call a tool or respond, the tool result comes back, the agent thinks again, and so on until it's done. `InMemorySessionService` keeps conversation state in RAM (fine for dev; swap to Firestore for prod).

**The key mental model:** An agent is not a function that runs once. It is a loop that keeps going — think, act, observe result, think again — until it decides it's done. The LLM is the thinker. Your tools are the actors.

---

### 17.2 FunctionTool — Wrapping Your Code for Agents

**The problem:** Agents only call tools — they cannot directly call Python functions. FunctionTool bridges the gap.

**The pattern:**

```python
from google.adk.tools import FunctionTool

def query_redshift(sql: str) -> dict:
    """
    Run a SQL query against the TL ETA Redshift datamart.
    Returns rows as a list of dicts.
    """
    conn = psycopg2.connect(...)
    cursor = conn.cursor()
    cursor.execute(sql)
    return {"rows": cursor.fetchall()}

redshift_tool = FunctionTool(func=query_redshift)
```

The agent sees `query_redshift`'s docstring as the tool description. The function signature (parameter names and types) tells the agent what arguments to pass. **This is why docstrings and type hints matter** — they are not just for humans, they are the API contract the LLM reads to decide how to call the tool.

**Three rules for good FunctionTools:**
1. Return dicts or strings — never raw objects the LLM can't read
2. Docstring = tool description — write it for the LLM, not just humans
3. Fail loudly with a clear error message — agents use error messages to retry or escalate

---

### 17.3 MCP — Model Context Protocol

**What it is:** MCP is a standard protocol that lets agents call tools hosted anywhere — in another process, on another server, or in the cloud. It decouples "where the tool lives" from "how the agent uses it." You don't write MCP from scratch; you use existing MCP servers and connect to them.

**Two transport types you'll use:**

**stdio transport** — the MCP server runs as a subprocess you spawn. Communication happens via stdin/stdout. Tight coupling, fast, same machine.

```python
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StdioServerParameters

github_mcp = MCPToolset(
    connection_params=StdioServerParameters(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env={"GITHUB_PERSONAL_ACCESS_TOKEN": token}
    ),
    tool_filter=["search_code", "get_file_contents", "list_commits"]
)
```

When you add `github_mcp` to an agent's tools, ADK spawns that `npx` subprocess and the agent calls GitHub tools through it.

**HTTP transport** — the MCP server runs somewhere independently (another process, Cloud Run, etc.). The agent connects via HTTP. Loose coupling, survives restarts.

```python
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, SseServerParams

phoenix_mcp = MCPToolset(
    connection_params=SseServerParams(url="http://localhost:6006/mcp"),
    tool_filter=["get_spans", "get_traces", "get_datasets"]
)
```

**tool_filter — why it matters:** MCP servers expose many tools. Without a filter, your agent sees all of them and gets confused. Always filter to only the tools that agent actually needs.

**The key mental model:** FunctionTool = tool that lives in your Python process. MCPToolset = tool that lives somewhere else. The agent doesn't know or care which is which — it just calls tools.

---

### 17.4 Why Hierarchical Agents — Not One Giant Agent

**The temptation:** Put everything in one big agent with 20 tools and a 10,000-word system prompt. It feels simpler.

**Why it fails:**
- LLMs degrade in quality with very long system prompts
- A 20-tool agent has no focus — it might try to rollback when it should just monitor
- Debugging is impossible — you can't tell which "brain" made a wrong decision
- Specialization is lost — a good investigator prompt is nothing like a good validator prompt

**The hierarchical pattern:**

```
Orchestrator (coordinator, no direct tools)
├── Sentinel (scheduled monitor — 1 tool: read Phoenix spans)
├── Investigator (root cause — many tools: Phoenix, S3, MLflow, GitHub, Graphify)
├── Validator (post-deploy checks — tools: Kafka peek, Redshift query)
├── Communicator (human interface — tools: Slack, Jira)
└── Executor (safe actions — tools: rollback script, model swap)
```

Each agent has one job. The Orchestrator never touches tools directly — it only decides which agent to delegate to next, and assembles the final narrative. This is called the **orchestrator pattern**.

**How delegation works in ADK:**
When the Orchestrator has `sub_agents=[investigator, validator, ...]`, the LLM can choose to "call" a sub-agent by name, passing it a task description. ADK handles the actual invocation. The sub-agent runs its own loop, finishes, and returns a result to the Orchestrator. The Orchestrator then decides what to do next.

---

### 17.5 Session and State — How Agents Share Context

**The problem:** The Orchestrator kicks off the Investigator. The Investigator finds a root cause and finishes. Now the Communicator needs to write the Slack message. How does the Communicator know what the Investigator found?

**Answer: Session state.** Every agent run has a session. State is a dict stored in the session. Agents can read and write to it.

```python
# Inside Investigator — write findings to session state
tool_context.state["root_cause"] = "api_fetch_limit mismatch: inference=73, training=184"
tool_context.state["pr_link"] = "https://github.com/org/repo/pull/621"
tool_context.state["confidence"] = "HIGH"

# Inside Communicator — read from session state
root_cause = tool_context.state.get("root_cause", "unknown")
```

The session service (InMemorySessionService in dev, Firestore in prod) persists this state across all agent invocations within the same incident session. Every agent in the same `session_id` shares the same state dict.

**Important:** State is per-incident. A new incident starts a new session with a fresh state. This is why session IDs matter — use `incident-{timestamp}` or `incident-{load_id}` to keep incidents separate.

---

### 17.6 Arize Phoenix — ML Observability and the OTEL Connection

**What Phoenix is:** Arize Phoenix is an open-source ML observability platform. It stores traces and spans from your ML inference code and provides tools to analyze them — PSI drift detection, latency percentiles, error rates, custom metrics.

**What OTEL is:** OpenTelemetry (OTEL) is the industry standard for observability instrumentation. You add OTEL instrumentation to your inference code, and it emits spans (records of work) to a backend. Phoenix is one such backend.

**How the Fritolay data gets into Phoenix (inference side):**
```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

# Set up once at app startup
provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:6006/v1/traces")))
trace.set_tracer_provider(provider)

tracer = trace.get_tracer("tl-eta-model")

def predict(checkcall_sequence):
    with tracer.start_as_current_span("tl_eta_prediction") as span:
        span.set_attribute("model.version", MODEL_VERSION)
        span.set_attribute("input.sequence_length", len(checkcall_sequence))
        result = model.predict(checkcall_sequence)
        span.set_attribute("prediction.eta_minutes", result)
        span.set_attribute("shipper.id", "fritolay")
        return result
```

Every call to `predict()` emits a span. Phoenix collects these spans. The Sentinel agent queries Phoenix for these spans to detect when MAE jumps, sequence length drops, or null rates spike.

**The Sentinel → Phoenix query flow:**
```
Cloud Scheduler → Sentinel agent (every 5 min)
  → phoenix_mcp.get_spans(model="tl-eta", time_window="last_5m")
  → phoenix_mcp.compute_psi(current=last_5m_spans, baseline=last_7d_spans)
  → if PSI > 0.25: trigger incident → hand off to Orchestrator
```

**PSI (Population Stability Index):**
- < 0.10 → No significant change — keep monitoring
- 0.10–0.25 → Moderate shift — log a warning
- > 0.25 → Critical drift — trigger incident

---

### 17.7 Graphify — Code as a Knowledge Graph

**The problem it solves:** When the Investigator needs to understand the codebase, how does it "read" 10,000 lines of Python? It can't call `cat` on every file. It needs a structured summary it can query.

**What Graphify does:** It parses the entire codebase using tree-sitter (a fast parser that works offline), builds a semantic knowledge graph, and outputs:
- `graph.json` — machine-queryable: nodes (functions, classes, files) + edges (calls, imports, inherits)
- `graph.html` — interactive visual for the demo video
- `GRAPH_REPORT.md` — natural language summary of the repo structure and key relationships

**How the Investigator uses it:**

Step 1: The Investigator calls the Graphify MCP to find functions related to the drift.
```
Agent: "Find all functions related to checkcall fetching and sequence building in the inference path"
Graphify MCP: returns node list → [fetch_checkcalls(), build_sequence(), InferenceEngine.predict()]
```

Step 2: It calls the GitHub MCP to read those specific functions.
```
Agent: "Get the content of fetch_checkcalls() in inference/engine.py at commit abc123"
GitHub MCP: returns the actual function code
```

Step 3: It compares inference code vs training code, finds the `api_fetch_limit=73` vs training unlimited.

This is the magic: Graphify narrows the search space from 10,000 lines to 3 relevant functions. GitHub MCP reads the exact code. The LLM compares them. No custom AST code needed.

**Confidence levels:**
- `EXTRACTED` — explicitly in the code (function signature, class definition)
- `INFERRED` — deduced from usage patterns (this function probably reads from that table)
- `AMBIGUOUS` — uncertain (multiple candidates, can't determine without runtime info)

When the Investigator finds an AMBIGUOUS relationship, it knows to go deeper — read the actual source rather than trusting the graph.

---

### 17.8 FastAPI as State Bridge

**The problem:** Streamlit (your demo UI) is a different Python process from your ADK agents. They can't share Python objects directly. How does the UI know what the agents are doing?

**Answer:** FastAPI is a lightweight web server running in the same process as your agents (or alongside them). It exposes an HTTP API that Streamlit polls.

```
ADK agents → write events to shared state dict
FastAPI → reads that dict, exposes it at /api/events and /api/state
Streamlit → polls /api/state every 2 seconds via requests.get()
```

**The endpoints you'll build:**

```python
from fastapi import FastAPI
app = FastAPI()

incident_state = {}  # shared in-memory store

@app.get("/api/state")
def get_state():
    return incident_state

@app.post("/api/event")
def add_event(event: dict):
    incident_state["events"].append(event)
    return {"ok": True}
```

Agents call `POST /api/event` when they complete a step. Streamlit calls `GET /api/state` to refresh the UI. Simple, zero additional dependencies.

---

### 17.9 Streamlit — The Demo Dashboard

**What it is:** Streamlit converts a Python script into a web UI. You write Python top-to-bottom, add `st.` widgets, and Streamlit renders it as a web page. No HTML/CSS/JavaScript required.

**The two patterns you'll use:**

**Pattern 1: Auto-refresh polling**
```python
import streamlit as st
import requests
import time

placeholder = st.empty()  # a container you can update in place

while True:
    state = requests.get("http://localhost:8000/api/state").json()
    with placeholder.container():
        st.metric("MAE", state["mae_current"], delta=state["mae_delta"])
        for event in state["events"]:
            st.write(f"**{event['agent']}:** {event['message']}")
    time.sleep(2)
    st.rerun()  # re-runs the entire script from top — Streamlit's update mechanism
```

**Pattern 2: Embedding Graphify's HTML graph**
```python
import streamlit.components.v1 as components

with open("services/tl-eta/graph/graph.html") as f:
    graph_html = f.read()

components.html(graph_html, height=600, scrolling=True)
```

`components.html()` renders arbitrary HTML in an iframe inside your Streamlit page. This is how you show the interactive Graphify graph without any JavaScript work.

---

### 17.10 Slack Bot Mechanics

**The two sides of Slack integration:**

**Outgoing (Airen → Slack):** Use the Slack SDK to post messages. Simple HTTP calls.
```python
from slack_sdk import WebClient
client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
client.chat_postMessage(channel="#ml-ops", text="Incident detected")
```

**Incoming (Slack → Airen):** Users click buttons or type messages. Slack sends HTTP POST requests to your webhook URL. You need a web server to receive these.

```
User clicks "Approve Rollback" in Slack
→ Slack POST → https://your-server/slack/actions
→ Your FastAPI handler receives the payload
→ You extract the action_id and user_id
→ You update the incident state: approved=True, approved_by="akash"
→ Agents polling state detect approval → proceed with rollback
```

**For local development:** You can't expose localhost to Slack directly. Use `ngrok http 8000` to create a public tunnel. Slack sends to `https://abc123.ngrok.io/slack/actions` → ngrok forwards to `localhost:8000/slack/actions`.

**Block Kit — interactive Slack messages:**
```python
blocks = [
    {"type": "section", "text": {"type": "mrkdwn", "text": "*Incident Detected*\nMAE spiked 109%"}},
    {"type": "actions", "elements": [
        {"type": "button", "text": {"type": "plain_text", "text": "Approve Rollback"},
         "action_id": "approve_rollback", "style": "danger"},
        {"type": "button", "text": {"type": "plain_text", "text": "Investigate More"},
         "action_id": "more_info"}
    ]}
]
client.chat_postMessage(channel="#ml-ops", blocks=blocks)
```

Buttons generate `action_id` payloads when clicked — this is how you distinguish "approve" from "investigate more."

---

### 17.11 The Build Order — 16 Steps with Learning Objectives

This is the exact sequence to build Airen. Each step has one concrete deliverable and one learning objective. Don't skip steps; each one teaches you something the next step depends on.

| Step | What to Build | Deliverable | What You'll Learn |
|------|--------------|-------------|-------------------|
| **1** | GCP project + APIs | All APIs enabled, Service Account JSON | How GCP project-level permissions work |
| **2** | Arize Phoenix local | Phoenix UI at localhost:6006 | What a trace/span looks like in the UI |
| **3** | OTEL instrumentation | Synthetic ETA predictions appear in Phoenix | How OTEL spans are emitted from application code |
| **4** | First ADK agent | "Hello, agent" loop runs in terminal | LlmAgent + Runner + InMemorySessionService wiring |
| **5** | First FunctionTool | Agent calls a Python function and uses its result | FunctionTool pattern, docstrings as API |
| **6** | Phoenix MCP connection | Agent calls `get_spans` via Phoenix MCP | stdio vs HTTP transport, MCPToolset, tool_filter |
| **7** | Sentinel agent | Sentinel detects synthetic MAE spike | Real agent with real detection logic |
| **8** | Redshift adapter | Agent queries tl_eta_datamart | psycopg2 connection, FunctionTool with SQL |
| **9** | GitHub MCP connection | Agent reads PR diffs via GitHub MCP | GitHub MCP tools, npx subprocess, token setup |
| **10** | Graphify setup | graph.json + graph.html generated for demo repo | tree-sitter parsing, how knowledge graphs work |
| **11** | Investigator agent | Agent finds the api_fetch_limit mismatch in code | Multi-tool agents, Graphify + GitHub combined |
| **12** | FastAPI state bridge | Agents write events; `/api/state` returns them | FastAPI basics, how to wire agents to HTTP |
| **13** | Streamlit UI | Live-updating reasoning trace in browser | Streamlit polling, st.empty(), components.html() |
| **14** | Slack outgoing | Agent posts Block Kit message to Slack | Slack SDK, Block Kit message structure |
| **15** | Slack incoming | Button click triggers agent action | Webhook handler, ngrok, approval flow |
| **16** | Jira integration | Auto ticket created + commented + closed | Atlassian MCP, Jira lifecycle |

After Step 16, you have a complete working Airen. Steps after that are polish: Cloud Run deploy, Vertex AI Agent Engine, demo data setup, video recording.

---

### 17.12 Key Files — What Each One Does

Understanding the purpose of every file before you create it eliminates confusion mid-build.

```
airen/
├── airen.yaml                         # service config — what Phoenix model, Redshift schema, Kafka topic
├── main.py                           # entry point — wires all agents, starts Runner
├── agents/
│   ├── orchestrator.py               # coordinator — no tools, only sub_agents; assembles narrative
│   ├── sentinel.py                   # monitoring loop — queries Phoenix, detects PSI drift
│   ├── investigator.py               # root cause — Phoenix + S3 + MLflow + GitHub + Graphify
│   ├── validator.py                  # post-deploy checks — Kafka peek + Redshift MAE delta
│   ├── communicator.py               # human interface — Slack messages, Jira tickets
│   └── executor.py                   # safe actions — rollback, model swap (human-approved only)
├── tools/
│   ├── redshift_tool.py              # FunctionTool wrapping psycopg2 queries
│   ├── s3_tool.py                    # FunctionTool wrapping boto3 S3 reads
│   ├── mlflow_tool.py                # FunctionTool wrapping MLflow REST API
│   ├── kafka_tool.py                 # FunctionTool wrapping stateless Kafka peek
│   └── audit_tool.py                 # FunctionTool writing to BigQuery audit log
├── mcp/
│   ├── phoenix_mcp.py                # MCPToolset for Arize Phoenix (HTTP transport)
│   ├── github_mcp.py                 # MCPToolset for GitHub (stdio transport via npx)
│   └── graphify_mcp.py               # MCPToolset for Graphify (stdio transport)
├── api/
│   ├── server.py                     # FastAPI app — /api/state, /api/event, /slack/actions
│   └── slack_handler.py              # Slack webhook handler, Block Kit builder
├── ui/
│   └── dashboard.py                  # Streamlit app — reasoning trace, Graphify graph, metrics
├── services/
│   └── tl-eta/
│       ├── airen.yaml                 # TL ETA-specific config
│       ├── queries/
│       │   ├── mae_baseline.sql      # SELECT MAE for last 30d pre-incident
│       │   └── psi_features.sql      # SELECT feature distributions for PSI
│       └── graph/
│           ├── graph.json            # Graphify output — machine-queryable
│           ├── graph.html            # Graphify output — interactive visual for demo
│           └── GRAPH_REPORT.md       # Graphify output — natural language repo summary
└── demo/
    ├── synthetic_data.py             # generates fake ETA predictions and logs to Phoenix
    ├── inject_incident.py            # replays the Fritolay MAE spike for demo
    └── freight-eta-service/          # minimal public repo with the Fritolay bug in git history
```

---

### 17.13 Three Core Concepts to Internalize Before Writing Code

**Concept 1: The agent is the LLM loop, not the tool.** Your Python functions (FunctionTools, MCP connections) are just the hands. The LLM (Gemini) is the brain. The agent's value comes from the LLM's ability to decide *which tool to call, in what order, given what it's already seen*. If you think of an agent as a fancy function call, you'll design it wrong. Think of it as a reasoning loop that can act.

**Concept 2: MCP is just a standard for calling remote tools.** Don't fear MCP. It's HTTP (or stdin/stdout) under the hood. The ADK `MCPToolset` handles all the protocol details. Your job is: pick the right MCP server, configure the transport, add a tool_filter. That's it. The agent doesn't know it's MCP — it just sees more tools.

**Concept 3: The hardest part is not the code — it's the system prompts.** You can write all the tools and wiring in a week. Making the agents *reason correctly* — detecting real incidents vs noise, blaming the right PR, writing readable Slack messages — that requires carefully written `instruction` strings and iteration. Plan to spend as much time tuning system prompts as writing code.

---

## 18. 17-Day Build Plan

| Day | Date | What to Build | Definition of Done |
|---|---|---|---|
| **1** | May 26 | Environment setup + Arize Phoenix running locally | Phoenix UI at localhost:6006 |
| **2** | May 27 | Log synthetic TL ETA predictions to Phoenix | 1000 predictions visible in Phoenix traces |
| **3** | May 28 | Arize Phoenix MCP connected to ADK | `get_spans` returns data from an ADK agent |
| **4** | May 29 | Sentinel Agent complete (Phoenix + Redshift) | PSI + MAE alert working on Phoenix data |
| **5** | May 30 | Investigator Agent — Redshift + S3 + MLflow | Full investigation report from one anomaly |
| **6** | May 31 | Graphify setup on demo repo + GitHub MCP + Investigator complete | Graphify graph.json built; PR bisect identifies PR #621; Graphify query returns api_fetch_limit gap |
| **7** | June 1 | RCA Writer Agent complete | Plain-English RCA output with PR link |
| **8** | June 2 | Kafka Adapter — stateless peek | Phase 1 validation working on live topic |
| **9** | June 3 | Remediation + Validator Agents complete | End-to-end cycle: detect → diagnose → fix → validate |
| **10** | June 4 | Orchestrator wires all 6 agents + config system | Full agent loop driven by `airen.yaml` |
| **11** | June 5 | Slack integration — alerts + approval buttons + chat | Full Slack flow: alert → approve → thread updates |
| **12** | June 6 | Jira integration — full ticket lifecycle | ETA-1821 style lifecycle in demo |
| **13** | June 7 | Deploy to Vertex AI Agent Engine + Cloud Run | Live public URL accessible |
| **14** | June 8 | End-to-end demo rehearsal | Full Fritolay scenario in < 3 minutes |
| **15** | June 9 | Reasoning trace UI polish + audit dashboard | All agent decisions visible in real-time |
| **16** | June 10 | Demo video — record, edit, upload | 3:00 YouTube video unlisted, ready |
| **17** | June 11 | README, architecture diagram, Devpost submission | Submitted by 1:00pm PDT |

**Pull-the-plug threshold:** If Vertex AI Agent Engine isn't working by June 5, fall back to Cloud Run-hosted ADK. Still rules-compliant.

---

## 19. 3-Minute Demo Script

> **Pre-requisite:** Run `generate_eta_predictions.py` to populate Phoenix with healthy baseline + drift data. Demo starts with system monitoring healthy data.

---

### SCENE 1 — The Pain (0:00–0:20)

**[Screen: Black. Text fades in.]**

> *"In February 2026, our Truck Load ETA model for Fritolay degraded silently."*
> *"A bundled performance PR capped inference inputs. Training had no such cap."*
> *"MAE doubled. Customers got bad ETAs. Nobody noticed for days."*
> *"This is that story — and how Airen stops it in 25 minutes."*

---

### SCENE 2 — The Drift Event (0:20–0:50)

**[Live: Click "Trigger Demo Event" in the dashboard]**

> **Voiceover:** "It's 12:32 IST. PR #621 just deployed. Inference now sees only the last 73 checkcalls — training saw 184. The Sentinel agent checks every 5 minutes."

**[Screen: Sentinel fires. PSI gauge for `sequence_length` climbs from 0.08 → 0.41, turns red. MAE on 3_long crosses 2x threshold.]**

> **Voiceover:** "Sequence length PSI hits 0.41. Critical threshold is 0.25. MAE on long-haul is double its 7-day average. The Investigator takes over."

---

### SCENE 3 — Investigation (0:50–1:25)

**[Screen: Investigator activates. Reasoning trace scrolls live.]**

```
12:38  [Investigator]  Q1: 3_long MAE 610min → 1277min (+109%)
12:39  [Investigator]  Q3: decile 1 gap +565min (early journey worst)
12:39  [Investigator]  Q4: MAE stable until 12:32, then spikes
12:39  [Investigator]  GitHub: 3 PRs merged in last 24h
12:40  [Investigator]  PR #621 touched helper.py — HIGH RISK FILE
12:40  [Investigator]  Diff: added &limit={api_fetch_limit} to API URL
12:41  [Investigator]  Training code: no equivalent limit found
12:41  [Investigator]  S3 training data: avg sequence 184 pings
12:41  [Investigator]  Kafka inference: avg sequence 73 pings
12:43  [Investigator]  Confirmed: training-inference mismatch via PR #621
```

> **Voiceover:** "The Investigator cross-referenced five sources simultaneously — Arize Phoenix, Redshift, S3 training data, MLflow model lineage, and GitHub commits. In 5 minutes, it found what took us days manually."

---

### SCENE 4 — The RCA in Slack (1:25–1:50)

**[Screen: Slack message appears in #ml-eta-incidents]**

> *"PR #621 (merged 12:32 IST) capped checkcall history to 73 pings at inference. Training used full history averaging 184 pings. Model has never seen truncated inputs. Long-haul early-journey predictions are worst affected — MAE doubled. Recommended fix: remove the cap. Takes 10 minutes."*

**[Buttons: Approve Option 1 | Approve Option 2 | Create Jira | Reject]**

> **Voiceover:** "Plain English. No statistics degree required. The engineer reads 4 sentences."

**[Dramatically click Approve Option 1]**

> **Voiceover:** "One button. That's it."

---

### SCENE 5 — Jira + Remediation (1:50–2:20)

**[Screen: Jira ticket ETA-1821 auto-created. Slack thread shows Jira link.]**

> **Voiceover:** "Jira ticket created automatically with the full investigation as the description. Hotfix PR opened."

**[Screen: Slack thread replies — PR opened, deploy confirmed, Phase 1 validation starting]**

> **Voiceover:** "Remediation logs every step as a Slack thread reply and a Jira comment. The team sees everything without asking."

---

### SCENE 6 — Validation + Resolution (2:20–2:50)

**[Screen: Phase 1 Kafka peek results]**

```
Phase 1 — Kafka (T+30 min)
Predictions:  847 | Null rate: 1.2% | ✅ PASS
Distribution: normal | Model identity: ✅ confirmed
```

**[Time-lapse to next morning — Phase 2 Redshift results]**

```
Phase 2 — Redshift (T+24h)
3_long MAE:  641 min (baseline: 610 min) ✅ within threshold
All shippers nominal ✅
Jira ETA-1821: RESOLVED
```

**[Dashboard goes all green. Slack thread final message.]**

---

### SCENE 7 — The Close (2:50–3:00)

**[Screen: Audit trail — 47 timestamped entries]**

> **Voiceover:** "47 automated decisions. 1 human button press. 25 minutes to resolution. The same incident took us days to find manually in February."

**[Screen: Airen logo + tagline]**

> *"While your team sleeps, Airen watches."*

> **Voiceover:** "Built with Arize Phoenix, Gemini 2.5, Google Cloud Agent Builder, Slack, and Jira. Any ML team onboards in an afternoon."

---

## 20. Devpost Submission Checklist

| Item | Status | Notes |
|---|---|---|
| Hosted URL | ⬜ | Cloud Run URL for Airen dashboard |
| Public GitHub repo | ⬜ | Apache 2.0 license at repo root |
| ~3 min YouTube video | ⬜ | Unlisted OK, must be accessible to judges |
| Devpost form completed | ⬜ | Track: Arize |
| Primary partner MCP | ⬜ | Arize Phoenix MCP |
| Supporting MCPs | ⬜ | Atlassian MCP (Jira) + Slack MCP |
| Google Cloud Agent Builder | ⬜ | Vertex AI Agent Engine + ADK |
| Gemini model used | ⬜ | `gemini-2.5-pro` (Orchestrator/Investigator/RCA/Remediation/Validator) |
| Architecture diagram | ⬜ | In README.md |
| Project started after May 5, 2026 | ⬜ | Verified by first commit date |
| Real war story documented | ⬜ | Fritolay incident — Feb 19, 2026 |
| Multi-service onboarding demo | ⬜ | Show `airen.yaml` + `airen add-service` |

**README must include:**
- One-paragraph pitch
- The Fritolay war story (why this exists)
- Architecture diagram
- How to run (exact commands)
- How to onboard a new service (`airen add-service`)
- Demo steps
- Which Google Cloud + Arize + Slack + Jira components are used

---

## 21. Judge Talking Points

---

**"Why Arize and not just Grafana/Datadog?"**
> Arize Phoenix is built specifically for ML model behaviour — it understands PSI, embedding drift, prediction distribution shift, and cohort-level performance degradation. Grafana tells you your CPU is high. Arize tells you your Fritolay model started failing specifically on long-haul early-journey predictions because inference is seeing truncated input sequences. Remove Arize and the Sentinel is blind.

---

**"How does the agent actually USE Arize Phoenix?"**
> The Sentinel Agent calls `get_spans` via the Arize Phoenix MCP to pull the last hour of predictions, computes PSI in-code, and escalates to the Investigator when thresholds are breached. The agent doesn't look at a dashboard — it reads the raw observability data, reasons over it, and cross-references it with 4 other data sources (Redshift, S3, MLflow, GitHub). Remove Arize and we have no real-time signal.

---

**"What makes the Investigator special?"**
> It's the only component we know of that does automated PR-bisect tied to model drift. When MAE spikes, it searches GitHub for commits merged in the drift window, reads diffs, reads training code, reads inference code, and finds the mismatch. In the Fritolay incident, this took engineers two days manually. Airen does it in 5 minutes.

---

**"Why is Slack the primary interface and not the web UI?"**
> Because engineers don't live in dashboards — they live in Slack. By moving all approvals to Slack and adding a conversational chat interface, Airen becomes a teammate, not a tool. You can ask it "what happened with Fritolay last week?" at 9am and get a plain-English answer without opening anything. The web UI exists for audit and replay.

---

**"What does the Jira integration actually add?"**
> Without Airen, incident documentation is a mix of Slack messages, someone's memory, and a post-mortem written days later. With Airen, the Jira ticket is created automatically the moment an incident is approved, commented at every stage by the agents, and closed with a machine-generated post-mortem when Phase 2 validation passes. The full story is in Jira without any human writing a single word.

---

**"This is a FourKites-specific tool. Can it work for other ML services?"**
> That was the original concern — so we made it config-driven. Any ML team onboards by writing one `airen.yaml` file and dropping in their diagnostic SQL queries. The agents are fully generic — they read the config and adapt. The same 6 agents that monitor Truck Load ETA would monitor a recommendation engine, a pricing model, or a churn prediction system. TL ETA is the reference implementation, not the only implementation.

---

**"What's the business impact?"**
> The Fritolay incident ran undetected for days. Airen catches the same class of failure in 5 minutes and resolves it in 25. For a logistics company running hundreds of shipper models, that gap — days vs minutes — is the difference between a near-miss and a customer-facing SLA breach. And it scales: one Airen deployment monitors every ML service across the company.

---

---

## Day 1 Kickoff — May 28, 2026

**Before writing any code:**
- Join Arize Discord: discord.gg/7Dqk5ebCD4
- Session: *"Getting from Zero to a Traced Agent in 5 minutes with Phoenix MCP"*
- Time: 10:00 AM PDT / **10:30 PM IST** — May 28

**Day 1 build order:**
1. Create `mlre` conda env + install all packages
2. Get Arize Phoenix running locally → `localhost:6006`
3. Write `demo/synthetic_data.py` — log fake TL ETA predictions to Phoenix
4. Confirm spans are flowing in Phoenix UI

**Deliverable:** Phoenix UI showing synthetic ETA prediction traces. Nothing else. Don't touch agents yet.

**GCP credits:** Request via the form on rapid-agent.devpost.com/resources — first-come-first-served, running low.

---

*End of Airen Technical Blueprint*
*Updated: May 27, 2026*
*Built for Google Cloud Rapid Agent Hackathon · Arize Track · June 2026*
*Primary use case: FourKites Truck Load ETA Prediction*
