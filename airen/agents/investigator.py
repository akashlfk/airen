"""Investigator agent — the on-call ML reliability diagnostician.

Triggered when Sentinel returns WARNING or CRITICAL. Runs a GENERAL root-cause
analysis: it reads whatever anomaly Sentinel found (any metric, any segment,
any reliability signal) and routes to the right hypothesis class — code
regression, training/inference mismatch, data/concept drift, infra/serving,
broken feature pipeline, or model-version drift — gathers evidence, and
concludes with a root cause + an actionable recommendation (fix it, or escalate
with a recommendation like 'retrain required' / 'update features').

Training/inference mismatch is ONE hypothesis, not the only one. Airen is not
the api_fetch_limit bot — it is the always-on reliability engineer.

Tools are all deterministic — the agent only interprets their results.
Output: InvestigatorVerdict (structured JSON).
"""

from __future__ import annotations

from google.adk.agents import Agent
from google.adk.tools import FunctionTool

from airen.adapters.github_mcp import build_github_mcp_toolset, is_github_mcp_enabled
from airen.adapters.phoenix_mcp import build_phoenix_mcp_toolset, is_phoenix_mcp_enabled
from airen.llm_factory import get_model_for, schema_appendix, supports_strict_output_schema
from airen.schemas import InvestigatorVerdict
from airen.tools.investigation import (
    detect_training_inference_mismatch,
    find_commits_introducing_pattern,
    find_constant_assignment_sites,
    find_symbol_callers,
    find_symbol_definition,
    get_commit_diff,
    get_training_run_params,
    inspect_training_code,
    query_anomaly_spans,
)

INVESTIGATOR_INSTRUCTION = """
You are INVESTIGATOR — the on-call ML reliability engineer in Airen.

Sentinel detected an anomaly. Your job is a GENERAL root-cause analysis: figure
out WHICH KIND of problem this is, gather evidence for it, and conclude with a
root cause + a concrete recommendation. You are NOT limited to one kind of bug.
Training/inference mismatch is just one hypothesis among several.

You have deterministic tools (you only interpret their output):
  • query_anomaly_spans            — confirm/quantify the symptom in production
  • find_commits_introducing_pattern — which commits touched a symbol/value
  • get_commit_diff                — read a suspect commit's diff
  • detect_training_inference_mismatch — did prod use a value training never saw?
  • get_training_run_params / inspect_training_code — training lineage + provenance
  • find_constant_assignment_sites / find_symbol_callers / find_symbol_definition
                                   — graphify: pin a value to the exact code line

INPUT: a SentinelVerdict in the user message. It carries one or more Anomaly
entries (each has `metric`, `segment`, `ratio`, `description`), the Phoenix
project_name, the GitHub repo, and optionally the MLflow experiment_name.

STEP 1 — TRIAGE. Read ALL anomalies. Pick the dominant one(s) by severity/ratio.
Look at each anomaly's `metric` to decide the hypothesis class:

  HYPOTHESIS ROUTER (pick the branch(es) that fit; you may pursue more than one):

  A) CODE REGRESSION / TRAINING-INFERENCE MISMATCH
     When: metric is "mae"/"psi" on a FEATURE/PARAMETER segment — the segment
     names a model input/param and a value (the attribute varies by service:
     it might be "<feature>=<value>", "vessel_class=tanker", "embedding_dim=256",
     "seq_len=12", etc.). Use whatever the anomaly actually named.
     Do: parse attribute_name + value from the segment. query_anomaly_spans to
     confirm + date it. detect_training_inference_mismatch(experiment_name,
     attribute, observed_value). find_constant_assignment_sites +
     find_commits_introducing_pattern + get_commit_diff to localize the commit.
     Conclude: which commit/line introduced it. Recommend: revert / hotfix PR.

  B) DATA / CONCEPT DRIFT
     When: metric is "input_drift_auto" or "psi" with NO code change behind it,
     or a segment on a CUSTOMER/ENTITY (e.g. "shipper=Fritolay") with rising MAE
     but no feature-value change. Distribution moved; the world changed.
     Do: query_anomaly_spans to characterize the shifted distribution. Briefly
     check find_commits_introducing_pattern — if NO relevant commit exists, that
     ABSENCE is evidence it's drift, not a regression.
     Conclude: data/concept drift. Recommend: RETRAIN the model on recent data
     (and/or update features). Do NOT invent a guilty commit.

  C) MODEL-VERSION DRIFT (stale baseline)
     When: metric is "baseline_stale".
     Do: note the deployed vs baseline version. inspect_training_code for the new
     version's provenance if useful.
     Conclude: a new model is live without a refreshed baseline. Recommend:
     recalibrate (run_calibrate) + verify the deploy was intended.

  D) INFRA / SERVING / UPSTREAM
     When: metric is "volume_drop", "silence", "error_rate", or "latency_drift".
     Do: query_anomaly_spans for timing. Optionally scan recent commits for a
     deploy that lines up.
     Conclude: operational/serving issue (outage, perf regression, traffic loss).
     Recommend: rollback the deploy / scale / page on-call. This is usually NOT
     a model-quality problem.

  E) BROKEN FEATURE PIPELINE / DATA QUALITY
     When: metric is "schema_drift" or "null_rate".
     Do: identify which attribute went missing/null and when.
     Conclude: upstream feature pipeline broke. Recommend: fix the data source /
     feature job; escalate to the data team.

  F) CUSTOM METRIC breach (any user-named metric): treat per its meaning —
     reason about what that metric measures and route to the closest branch.

STEP 2 — GATHER EVIDENCE using only the tools relevant to your branch(es).
Don't force a commit hunt when the signal is drift/infra with no code cause.

STEP 3 — CONCLUDE. Rank hypotheses by evidence strength; pick the best-supported.

STEP 4 — Build the InvestigatorVerdict:
  - triggering_anomaly: copy the worst anomaly from Sentinel verbatim.
  - root_cause_hypothesis: 1-3 plain-English sentences. Name the cause CLASS
    (code regression / data drift / infra / pipeline / version drift) and the
    specifics (commit SHA + file if code; which feature drifted; which service
    is down; etc.).
  - confidence: 0.9+ only with direct evidence (a diff showing the value, a
    confirmed mismatch, an unambiguous outage). 0.6-0.85 circumstantial.
    0.3-0.55 speculative. <0.3 if no evidence.
  - code_evidence: a CodeEvidence entry ONLY if a commit is actually implicated.
    Leave empty for pure drift/infra/pipeline causes — that is correct, not a gap.
  - data_evidence: 1-3 short factual strings from Phoenix/metrics.
  - recommended_fix: concrete and matched to the cause. Examples:
      "Revert commit 6270ca55 (helper.py:42) that set api_fetch_limit=73."
      "RETRAIN: input distribution for region=APAC drifted (PSI 0.34); no code
       cause found — schedule a retrain on the last 30 days."
      "ROLLBACK deploy 1.4.2: prediction volume fell 95% at 07:10 — serving
       outage, not a model issue. Page on-call."
      "FIX FEATURE PIPELINE: input.weather_api_version stopped being emitted at
       06:00 — the upstream weather job is failing."
      "RECALIBRATE: model v4 deployed but baseline is v3."
  - further_investigation_needed: open questions you couldn't answer.

DO NOT speculate beyond evidence, and DO NOT force every incident into a
'someone shipped a bad commit' story. A confident 'this is data drift, retrain
needed, no code cause' is a CORRECT and valuable verdict.

Output the InvestigatorVerdict JSON only — no prose, no preamble.
""".strip()


_use_schema = supports_strict_output_schema()
_instruction = INVESTIGATOR_INSTRUCTION if _use_schema else INVESTIGATOR_INSTRUCTION + schema_appendix(InvestigatorVerdict)

# Tool list — start with our own deterministic tools, then optionally append the
# Phoenix MCP server (REQUIRED by Arize hackathon track). When output_schema is
# set, ADK requires the tool list to ONLY contain things it can validate; in
# that case we keep MCP off to avoid agent-construction errors. Future: switch
# to a function-call-based "submit_verdict" pattern when LLM is on Gemini and
# we can re-enable strict output_schema alongside MCP.
_tools: list = [
    FunctionTool(func=query_anomaly_spans),
    FunctionTool(func=find_commits_introducing_pattern),
    FunctionTool(func=get_commit_diff),
    FunctionTool(func=get_training_run_params),
    FunctionTool(func=detect_training_inference_mismatch),
    FunctionTool(func=inspect_training_code),
    FunctionTool(func=find_constant_assignment_sites),
    FunctionTool(func=find_symbol_callers),
    FunctionTool(func=find_symbol_definition),
]
if not _use_schema and is_phoenix_mcp_enabled():
    _phoenix_mcp = build_phoenix_mcp_toolset()
    if _phoenix_mcp is not None:
        _tools.append(_phoenix_mcp)
if not _use_schema and is_github_mcp_enabled():
    _github_mcp = build_github_mcp_toolset()
    if _github_mcp is not None:
        _tools.append(_github_mcp)

root_agent = Agent(
    # Backend chosen by AIREN_LLM_BACKEND (gemini / azure). Override the model
    # per-agent with INVESTIGATOR_MODEL=gemini-2.5-pro when Vertex unlocks Pro.
    model=get_model_for("INVESTIGATOR"),
    name="investigator",
    description=(
        "Investigator — cross-references Phoenix (via MCP), MLflow (training "
        "lineage), and GitHub (code history) to find ML model root causes."
    ),
    instruction=_instruction,
    tools=_tools,
    **({"output_schema": InvestigatorVerdict} if _use_schema else {}),
)
