"""Pydantic models for agent inputs/outputs.

Each specialist agent declares its verdict schema here. These schemas are
passed to ADK as `output_schema=...` to force Gemini to emit valid JSON
matching the model — no regex parsing of LLM prose.

When Orchestrator (Day 8) consumes a Sentinel verdict, it does:
    verdict = SentinelVerdict.model_validate_json(agent_response_text)
and gets a typed object with autocomplete in the IDE.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


# ───────────────────────────────────────────────────────────────────────
#  Shared enums
# ───────────────────────────────────────────────────────────────────────
class HealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class RecommendedAction(str, Enum):
    MONITOR = "MONITOR"
    INVESTIGATE = "INVESTIGATE"
    ALERT_HUMAN = "ALERT_HUMAN"


# ───────────────────────────────────────────────────────────────────────
#  Sentinel verdict — what Sentinel returns at the end of each check
# ───────────────────────────────────────────────────────────────────────
class Anomaly(BaseModel):
    """One specific deviation Sentinel found."""

    metric: str = Field(description="e.g., 'mae', 'volume', 'null_rate'")
    segment: str = Field(
        description="Which slice — 'overall', 'shipper=Fritolay', 'api_fetch_limit=73'"
    )
    current_value: float
    baseline_value: float
    ratio: float = Field(description="current / baseline; >1 means worse than baseline")
    description: str = Field(description="One-sentence human-readable explanation")


class SentinelVerdict(BaseModel):
    """Top-level Sentinel output. Orchestrator routes downstream based on this."""

    project_name: str
    n_spans_analyzed: int
    status: HealthStatus
    severity_score: float = Field(
        ge=0.0, le=1.0, description="0=healthy, 1=worst-case critical"
    )
    anomalies: list[Anomaly] = Field(
        default_factory=list, description="All deviations found; empty if HEALTHY"
    )
    recommended_action: RecommendedAction
    summary: str = Field(description="1-2 sentence executive summary for humans")


# ───────────────────────────────────────────────────────────────────────
#  Training-code provenance — links a deployed model back to its source
# ───────────────────────────────────────────────────────────────────────
class TrainingProvenance(BaseModel):
    """How a deployed model was trained. Derived from MLflow run tags.

    Returned by MLflowAdapter.get_training_provenance(). Investigator uses
    this to fetch the actual training code (via Graphify at the commit) and
    to compare declared training features vs observed production features.

    Sentinel uses `has_git_traceability` and `reproducibility_warnings` to
    emit a GOVERNANCE_RISK signal when a model in production lacks the
    provenance needed to audit or reproduce it.
    """

    experiment_name: str
    experiment_id: str
    run_id: str | None = None
    run_name: str | None = None

    # Source-code provenance — every field may be None
    source_path: str | None = Field(
        default=None,
        description="mlflow.source.name — the file path that emitted the run. "
                    "Might be a local-disk path on the trainer's machine; not always a repo file.",
    )
    git_commit: str | None = Field(
        default=None,
        description="Full SHA. Resolvable via git fetch <sha>. Unset for Jupyter / "
                    "local-script training that wasn't run inside a git checkout.",
    )
    git_branch: str | None = None
    git_repo: str | None = Field(
        default=None,
        description="owner/repo if we resolved which GitHub repo the commit belongs to. "
                    "Set externally (not by MLflow); see model_registry config.",
    )
    user: str | None = Field(default=None, description="mlflow.user — who ran the training")
    runtime: str | None = Field(default=None, description="mlflow.source.type — LOCAL / NOTEBOOK / PROJECT / JOB")

    # Computed flags / warnings
    has_git_traceability: bool = Field(
        default=False,
        description="True iff git_commit is set AND we can clone the commit",
    )
    reproducibility_warnings: list[str] = Field(
        default_factory=list,
        description="Specific issues: 'no git commit logged', 'source is a Jupyter cell', "
                    "'commit not in expected repo', etc. Empty list = healthy provenance.",
    )


# ───────────────────────────────────────────────────────────────────────
#  Investigator verdict — Day 4
# ───────────────────────────────────────────────────────────────────────
class CodeEvidence(BaseModel):
    """A specific piece of code-level evidence linking the symptom to a cause."""

    commit_sha: str = Field(description="Suspect commit SHA, first 8 chars OK")
    commit_author: str | None = None
    commit_date: str | None = Field(default=None, description="ISO 8601 date")
    commit_message: str | None = Field(default=None, description="First line of commit message")
    file_path: str | None = Field(default=None, description="File where the pattern was found")
    matched_pattern: str | None = Field(
        default=None, description="The literal string that linked this commit to the anomaly"
    )
    pr_number: int | None = None
    pr_url: str | None = None
    diff_snippet: str | None = Field(
        default=None, description="A few lines of the diff that introduced the bug"
    )


class InvestigatorVerdict(BaseModel):
    """Top-level Investigator output. RCA Writer consumes this to draft the incident report."""

    triggering_anomaly: Anomaly = Field(
        description="The Sentinel anomaly that triggered this investigation"
    )
    root_cause_hypothesis: str = Field(
        description="1-3 sentences: the agent's best explanation of why the anomaly is happening"
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="How confident the agent is in the hypothesis; 0.9+ = strong evidence",
    )
    code_evidence: list[CodeEvidence] = Field(
        default_factory=list,
        description="GitHub-side evidence supporting the hypothesis",
    )
    data_evidence: list[str] = Field(
        default_factory=list,
        description="Phoenix-side observations (e.g. 'segment X has 4.2x MAE since 2026-02-19')",
    )
    recommended_fix: str = Field(
        description="What a human engineer should do — concrete steps, e.g. 'revert helper.py:42'"
    )
    further_investigation_needed: list[str] = Field(
        default_factory=list,
        description="Questions the agent could not answer with available tools",
    )


# ───────────────────────────────────────────────────────────────────────
#  Incident report — RCA Writer's output, Day 5
# ───────────────────────────────────────────────────────────────────────
# ───────────────────────────────────────────────────────────────────────
#  Orchestrator state machine — Day 6
# ───────────────────────────────────────────────────────────────────────
class OrchestratorState(str, Enum):
    """The state machine the Orchestrator runs through during one cycle.

    Reachable today:        IDLE → MONITORING → (HEALTHY | ANOMALY_DETECTED)
                                  → INVESTIGATING → RCA_DRAFTING
                                  → NOTIFYING → RESOLVED
    Reachable on FK VM:     + REMEDIATING + VALIDATING (needs Remediation/Validator agents)
    Always reachable:       FAILED
    """

    IDLE = "IDLE"
    MONITORING = "MONITORING"
    HEALTHY_NO_ACTION = "HEALTHY_NO_ACTION"
    ANOMALY_DETECTED = "ANOMALY_DETECTED"
    INVESTIGATING = "INVESTIGATING"
    RCA_DRAFTING = "RCA_DRAFTING"
    NOTIFYING = "NOTIFYING"
    REMEDIATION_PREP = "REMEDIATION_PREP"    # build branch + apply fix + open PR (ready)
    AWAITING_APPROVAL = "AWAITING_APPROVAL"  # block on Slack approve/reject
    MERGING = "MERGING"                      # merge the approved PR (gated)
    DEPLOYING = "DEPLOYING"                  # wait for CI/CD to ship the merge
    REMEDIATING = "REMEDIATING"
    VALIDATING = "VALIDATING"                # poll live feed until recovered / timeout
    REOPENED = "REOPENED"                    # fix didn't work → loop back to investigate
    AWAITING_HUMAN = "AWAITING_HUMAN"        # Airen stops auto-acting, waits for human
    RESOLVED = "RESOLVED"
    FAILED = "FAILED"


class IncidentEvent(BaseModel):
    """One state transition or notable action during an Orchestrator run."""

    timestamp: str = Field(description="ISO 8601 UTC")
    state: OrchestratorState
    agent: str | None = Field(default=None, description="Which sub-agent was active, if any")
    message: str = Field(description="Human-readable description of what happened")
    duration_ms: int | None = Field(default=None, description="How long this step took")
    metadata: dict = Field(default_factory=dict)


class IncidentRun(BaseModel):
    """The full record of one Orchestrator cycle. Used by audit log + UI."""

    run_id: str = Field(description="e.g. INC-A3F2B7 — also the incident id")
    project_name: str
    started_at: str
    finished_at: str | None = None
    final_state: OrchestratorState
    events: list[IncidentEvent] = Field(default_factory=list)
    # Sub-agent outputs, populated as we progress
    sentinel_verdict: "SentinelVerdict | None" = None
    investigator_verdict: "InvestigatorVerdict | None" = None
    incident_report: "IncidentReport | None" = None
    remediation_plan: "RemediationPlan | None" = None
    remediation_result: "RemediationResult | None" = None
    validator_phase1: "ValidatorVerdict | None" = None
    validator_phase2: "ValidatorVerdict | None" = None
    slack_permalink: str | None = None
    slack_message_ts: str | None = Field(
        default=None,
        description="Parent message timestamp. Used to (a) thread Concierge replies, "
                    "(b) link thread replies back to this incident.",
    )
    slack_channel: str | None = None
    jira_issue_key: str | None = None
    jira_issue_url: str | None = None
    approval: dict | None = Field(
        default=None,
        description="Recorded human decision from Slack button click: {decision, by, at}",
    )
    error: str | None = None


class IncidentReport(BaseModel):
    """Human-readable incident report. Same content fans out to Slack + Jira + UI.

    Tone: professional, concise, calibrated. Don't overclaim. Cite evidence.
    Format: each section is Markdown (renders cleanly in Slack, Jira, and our UI).
    """

    title: str = Field(
        description="One-line incident headline, suitable for Slack alert + Jira summary. ≤ 80 chars."
    )
    severity: HealthStatus = Field(description="Carried forward from Sentinel verdict.")
    tldr: str = Field(
        description=(
            "2-3 sentence executive summary. The on-call engineer or manager skimming on "
            "their phone reads ONLY this and gets the gist."
        )
    )
    what_happened: str = Field(
        description=(
            "Markdown. The symptom side — what the model did wrong, when, how bad, who's "
            "affected. NO jargon. Reference exact metrics from Sentinel."
        )
    )
    root_cause: str = Field(
        description=(
            "Markdown. The agent's hypothesis with appropriate hedging. If confidence is "
            "<0.7, lead with 'likely' / 'possible'; if >0.9, lead with 'confirmed'."
        )
    )
    evidence_bullets: list[str] = Field(
        default_factory=list,
        description="Concise bullet points combining data evidence + code evidence with citations (PR URLs, span counts).",
    )
    recommended_fix: str = Field(
        description="Markdown. Concrete actionable steps. What the human should do, in order."
    )
    open_questions: list[str] = Field(
        default_factory=list,
        description="Things the agent could not verify. The human needs to follow up on these.",
    )


# ───────────────────────────────────────────────────────────────────────
#  Remediation — Day 7
# ───────────────────────────────────────────────────────────────────────
class RemediationActionType(str, Enum):
    """The kind of fix Remediation proposes."""

    REVERT_COMMIT = "REVERT_COMMIT"        # most common: undo the offending commit via PR
    HOTFIX_PR = "HOTFIX_PR"                # targeted small PR (e.g., flip a config flag)
    RETRAIN_MODEL = "RETRAIN_MODEL"        # future: kick MLflow training run
    ROLLBACK_DEPLOY = "ROLLBACK_DEPLOY"    # future: revert deployed model artifact
    NO_ACTION = "NO_ACTION"                # confidence too low or symptom is infra-side


class RemediationPlan(BaseModel):
    """Proposed fix — produced BEFORE any side effects. Always safe to display."""

    action_type: RemediationActionType
    target_commit_sha: str | None = Field(
        default=None, description="The commit we'd revert / fix around (short SHA OK)"
    )
    target_pr_number: int | None = Field(
        default=None, description="The PR that originally introduced the regression"
    )
    target_pr_url: str | None = None
    proposed_branch_name: str = Field(
        description="Branch name we'd push to, e.g. 'airen/revert-d9de2cc6-INC-A3F2'"
    )
    proposed_pr_title: str = Field(description="Title for the remediation PR (≤ 80 chars)")
    proposed_pr_body: str = Field(
        description="Markdown body for the PR — explains what + why, links the incident"
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="How safe this remediation is to auto-apply"
    )
    rationale: str = Field(
        description="1-2 sentences why THIS action vs others (revert vs hotfix vs no-op)"
    )


# ───────────────────────────────────────────────────────────────────────
#  Validator — Day 7+
# ───────────────────────────────────────────────────────────────────────
class ValidatorStatus(str, Enum):
    """Did the remediation actually work?"""

    PASS = "PASS"                  # all anomalies resolved
    FAIL = "FAIL"                  # anomalies still critical
    INCONCLUSIVE = "INCONCLUSIVE"  # partial improvement, not back to baseline
    DEFERRED = "DEFERRED"          # phase 2 needs FK VM resources we don't have yet
    NO_DATA = "NO_DATA"            # no fresh predictions in the validation window


class SegmentComparison(BaseModel):
    """How one anomaly's ratio changed from incident time → now."""

    segment: str
    before_ratio: float = Field(description="The ratio Sentinel originally flagged")
    after_ratio: float | None = Field(default=None, description="Current ratio for the same segment")
    improvement: float | None = Field(
        default=None, description="before - after; positive = improved"
    )
    resolved: bool = Field(
        default=False,
        description="True when after_ratio < warning_threshold (1.2x)",
    )


class ValidatorVerdict(BaseModel):
    """Top-level Validator output. One per phase."""

    phase: int = Field(description="1 = live Phoenix recheck; 2 = actuals at T+24h (FK VM)")
    status: ValidatorStatus
    summary: str = Field(description="1-2 sentences for humans")
    lookback_minutes: int = Field(description="Window we sampled for the recheck")
    n_spans_observed: int = 0
    overall_mae_before: float | None = None
    overall_mae_after: float | None = None
    comparisons: list[SegmentComparison] = Field(default_factory=list)
    recommended_next_step: str = Field(
        description="'monitor' | 're-investigate' | 'manual intervention' | 'schedule phase 2'"
    )


class RemediationResult(BaseModel):
    """Outcome of executing a plan. Created only when --execute is requested."""

    plan: RemediationPlan
    executed: bool
    executed_at: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    branch_created: str | None = None
    merged: bool = False
    merge_sha: str | None = None
    merged_at: str | None = None
    error: str | None = None


# Forward-reference resolution for IncidentRun (declared above IncidentReport).
IncidentRun.model_rebuild()
