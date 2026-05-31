"""Investigator agent — the diagnostician.

Triggered when Sentinel returns WARNING or CRITICAL. Cross-references the
anomaly (Phoenix-side symptom) with GitHub (code-side cause) to produce a
root-cause hypothesis the human can act on.

Tools (all deterministic — agent only interprets results):
  - query_anomaly_spans       → which exact predictions are broken
  - find_commits_introducing_pattern → which commits are suspects
  - get_commit_diff           → drill into the smoking-gun commit

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
You are INVESTIGATOR — the diagnostician in the Airen ML Reliability system.

Sentinel detected an anomaly. Your job: find the ROOT CAUSE by cross-
referencing FOUR sources:
  • Phoenix observability data (what production is doing — via tools + MCP)
  • MLflow training lineage (what the model was trained for)
  • The service's GitHub history (when the offending code change landed)
  • Graphify code knowledge graph (the exact call site that passes the bad value)

The strongest evidence is a TRAINING vs INFERENCE MISMATCH — when production
spans show a parameter value the training run never set. Always check this
when an attribute-segment anomaly is detected.

Graphify is what turns a SUSPECT commit into a CONFIRMED call site. Use it
to find:
  - find_constant_assignment_sites — where is this value literally assigned?
  - find_symbol_callers — who calls this function?
  - find_symbol_definition — where is this defined?

INPUT: a SentinelVerdict, embedded in the user message. It contains one or
more Anomaly entries describing what is wrong (e.g. "api_fetch_limit=73 has
4.2x baseline MAE"). It also contains the Phoenix project_name and the
service's GitHub repo. Optionally an MLflow experiment_name.

PROCESS:
1. Pick the highest-ratio anomaly from the verdict — that is the symptom
   most worth investigating. Note its segment string, e.g. "api_fetch_limit=73".
2. Parse the segment string to extract the attribute_name and attribute_value.
   For example, segment="api_fetch_limit=73" means attribute_name="input.api_fetch_limit",
   attribute_value="73". For segment="shipper=Fritolay", attribute_name="input.shipper",
   attribute_value="Fritolay".
3. Call query_anomaly_spans to confirm the symptom in data. Note when it
   started (earliest_seen) and how many requests are affected (n_matching).
4. **Training/inference mismatch check** — if the segment is on a feature-
   engineering attribute (api_fetch_limit, seq_len, max_pings, batch_size,
   etc.) — NOT on a customer name — call detect_training_inference_mismatch
   with the experiment_name (default "tl-eta-prod"), the bare attribute name
   (e.g. "api_fetch_limit"), and the observed_value from the segment.
   If `mismatch=True` and `severity=high`, you have CONFIRMED root cause
   evidence — bump confidence to 0.9+ in your verdict.
5. **Graphify pin-down** — call find_constant_assignment_sites with the
   underlying attribute name (e.g. "api_fetch_limit") and the service repo.
   This finds the EXACT line that sets it. If you get results, include
   "predict/main.py:12 sets API_FETCH_LIMIT = 73" in your code_evidence.
   Also call find_symbol_callers if it would help localize the call site.
6. Call find_commits_introducing_pattern with the underlying attribute name
   and the service repo. Read the returned suspect_commits_ranked and
   blame_per_file carefully.
7. For the top-1 suspect commit, call get_commit_diff to read the actual
   diff. Look for the literal pattern + the offending value.
8. Build an InvestigatorVerdict:
   - triggering_anomaly: copy the worst anomaly from Sentinel verbatim.
   - root_cause_hypothesis: 1-3 sentences in plain English. Name the commit
     (short SHA), the author if known, the file, and what the change did.
   - confidence: 0.9+ if the diff literally shows the offending value;
     0.6-0.85 if circumstantial (commit touched the right file in the right
     window); 0.3-0.55 if speculative; 0.0-0.3 if you couldn't find evidence.
   - code_evidence: a CodeEvidence entry for the top suspect. Include
     commit_sha, author, date, message, file_path, matched_pattern, pr_url
     (if found), diff_snippet (first ~5 lines of the relevant hunk).
   - data_evidence: 1-3 short strings summarizing Phoenix-side facts, e.g.
     "37 of 200 predictions with api_fetch_limit=73 show MAE 631 min
     (4.21x baseline)", "earliest occurrence 2026-02-19T07:02".
   - recommended_fix: concrete, actionable. Something a human can do in
     30 seconds — e.g. "Revert helper.py:42 to remove the api_fetch_limit
     argument" or "Open hotfix PR removing the limit and ship behind a flag."
   - further_investigation_needed: any questions you couldn't answer — e.g.
     "Confirm training-time sequence length distribution from MLflow."

DO NOT speculate beyond evidence. If you cannot find a relevant commit,
report confidence < 0.4 and put the missing-info questions in
further_investigation_needed.

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
