"""airen.yaml — the per-service config schema and loader.

This is what makes Airen a *platform*. Each onboarded service drops one
`services/<service-name>/airen.yaml` and Airen knows:
  - which Phoenix project to monitor
  - which GitHub repo to investigate against
  - which Slack channel to alert
  - which Jira project to file incidents in
  - which Sentinel thresholds apply
  - (eventually) which Redshift table / MLflow experiment

The agents themselves stay service-agnostic. The Orchestrator translates
this config into the right call shapes.

Validation: Pydantic. Bad config files fail at startup, not at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

# Default location: <repo-root>/services/<name>/airen.yaml
# Production registry — where the user's REAL onboarded services live. Starts
# empty; `python -m airen.run_onboard` writes here.
SERVICES_DIR = Path(__file__).resolve().parent.parent / "services"
# Demo fixtures (tl-eta, ocean-eta) live OUT of the production path so they don't
# pollute the registry. Resolvable by name as a fallback (for mock-mode demo +
# tests), but NOT listed by `--list`.
DEMO_SERVICES_DIR = Path(__file__).resolve().parent.parent / "demo" / "services"


# ───────────────────────────────────────────────────────────────────────
#  Sub-configs (each block of airen.yaml)
# ───────────────────────────────────────────────────────────────────────
class ServiceInfo(BaseModel):
    """Identity metadata for the service."""

    name: str = Field(description="Short identifier, e.g. 'tl-eta'")
    description: str | None = None
    owner_team: str | None = Field(default=None, description="Slack handle / team name")
    context: str | None = Field(
        default=None,
        description="Free-text operator context fed to the Investigator + RCA agents — "
                    "things NOT in the repo: upstream deps, known issues, retrain/approval "
                    "policy, seasonality, who to page. Markdown ok. This is how you give "
                    "the LLM out-of-band knowledge about the service.",
    )


class PhoenixConfig(BaseModel):
    """Where to read prediction observability from."""

    project_name: str = Field(description="Phoenix project the service emits spans to")
    collector_endpoint: str = Field(
        default="http://localhost:6006",
        description="Phoenix HTTP base — overridable per-service for cloud Phoenix",
    )


class GithubConfig(BaseModel):
    """Where Investigator looks for code-level evidence."""

    repo: str = Field(description="owner/repo, e.g. 'cloudqwest/dynamic_eta_prediction'")
    default_branch: str = "main"
    attributes_of_interest: list[str] = Field(
        default_factory=list,
        description="Feature-engineering attribute names that often appear as code constants",
    )


class SlackConfig(BaseModel):
    """Where Airen posts alerts. Falls back to SLACK_CHANNEL env if alert_channel is None."""

    alert_channel: str | None = None
    enable: bool = True


class JiraConfig(BaseModel):
    """Where Airen files / transitions tickets.

    Transition names are matched case-insensitively against the project's
    workflow. Setting any to None disables that transition (the ticket stays
    where it is). The defaults match a vanilla Jira "Software" workflow.
    """

    # Atlassian instance topology — plain config → yaml. The API token is the
    # only secret and lives in .env as AIREN_JIRA_API_TOKEN. Adapters fall back
    # to AIREN_JIRA_BASE_URL / AIREN_JIRA_USER_EMAIL env when these are null.
    base_url: str | None = None         # e.g. https://fourkites.atlassian.net
    user_email: str | None = None       # API user's email (not a secret)
    project_key: str | None = None
    enable: bool = False  # off until Atlassian MCP wired

    # Which issue type Airen creates. Real projects rarely have a plain "Task";
    # ETAI uses "Root Cause Analysis" / "Bug" / "Defect" / etc. Configure per-service.
    issue_type: str = Field(
        default="Task",
        description="Issue type for Airen-filed incidents. Must exist in the target "
                    "project's issue type scheme (e.g. 'Root Cause Analysis', 'Bug').",
    )

    # Lifecycle transitions — names from the project's workflow, not statuses.
    # Set any to None to skip that transition (useful when the project's
    # workflow doesn't have a clean mapping to Airen's lifecycle).
    transition_on_alert: str | None = Field(
        default=None,
        description="Run after Slack alert posts. Signals 'Airen is working on this.' "
                    "None = skip (just comment, no status change).",
    )
    transition_on_pass: str | None = Field(
        default=None,
        description="Run when Validator Phase 1 returns PASS. Auto-closes the ticket. "
                    "None = skip.",
    )
    transition_on_fail: str | None = Field(
        default=None,
        description="Run on Validator FAIL/INCONCLUSIVE. None = leave ticket open.",
    )


class RedshiftConfig(BaseModel):
    """Historical predictions / actuals for batch analysis (FK VM required).

    Connection topology lives here in yaml; ONLY the password is a secret and
    lives in .env as REDSHIFT_PASSWORD. Adapters fall back to the matching
    env vars (REDSHIFT_HOST etc.) when a field is left null, for compatibility.
    """

    host: str | None = None
    port: int = 5439
    database: str | None = None
    user: str | None = None          # a username, not a secret → yaml
    sslmode: str = "require"
    table: str | None = None
    actuals_table: str | None = None
    enable: bool = False
    # Secret (NOT here): REDSHIFT_PASSWORD lives in .env.


class MLflowConfig(BaseModel):
    """Model registry / training run lineage.

    tracking_uri + experiment are plain config → yaml. FK's MLflow is no-auth;
    set MLFLOW_TOKEN in .env only if your tracking server requires it.
    """

    tracking_uri: str | None = None
    experiment_name: str | None = None
    enable: bool = False


class ModelRegistryEntry(BaseModel):
    """Maps a production model_family (as seen in prod_ml_output.model_family or
    Phoenix span attributes) to its MLflow experiment + expected source repo.

    Lets Airen resolve any deployed model back to its training code:
      model_family → experiment_id → MLflow tags → git commit → file at commit

    Use `expected_repo` when MLflow's tags don't include the repo URL (FK's
    setup rarely does — only the commit SHA). Airen then assumes the commit
    lives in `expected_repo`. Set to None to flag the model as governance-
    risk regardless of whether MLflow has a commit (e.g. for Jupyter trainers).
    """

    model_family: str = Field(
        description="Value of model_family as it appears in production output. "
                    "Used to match incoming predictions to a training pipeline.",
    )
    experiment_id: str = Field(description="MLflow experiment id (string).")
    expected_repo: str | None = Field(
        default=None,
        description="owner/repo where the training commit lives. None = no "
                    "expected repo, which triggers a governance warning even "
                    "if MLflow has the commit.",
    )
    notes: str | None = None


class S3Config(BaseModel):
    """Where training-time baselines live.

    Layout assumed: s3://<bucket>/baselines/<service>/<version>.json
    bucket + region are plain config → yaml. AWS credentials are secrets and
    live in .env (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY). Sentinel prefers
    these baselines over hand-tuned yaml values, degrading gracefully when the
    object is missing or creds aren't configured.
    """

    bucket: str | None = None
    region: str = "us-east-1"
    # Specific version to pin to; None = always fetch latest.json
    model_version: str | None = None
    enable: bool = False
    # Secrets (NOT here): AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY live in .env.


class KafkaConfig(BaseModel):
    """Peek-only Kafka access for live validation (FK VM required).

    Connection topology (brokers, topics, group, protocol) is plain config →
    yaml. ONLY the SASL credentials are secrets and live in .env as
    KAFKA_SASL_USERNAME / KAFKA_SASL_PASSWORD.
    """

    bootstrap_servers: str | None = None
    input_topic: str | None = None
    output_topic: str | None = None
    group_id: str | None = None
    security_protocol: str | None = None
    sasl_mechanism: str | None = None
    enable: bool = False
    # Secrets (NOT here): KAFKA_SASL_USERNAME / KAFKA_SASL_PASSWORD live in .env.


class ServingConfig(BaseModel):
    """How the inference layer serves predictions — which determines *where*
    Airen should observe them.

    `mode` is descriptive; `prediction_source` is the one Sentinel acts on: it
    names the live feed Airen reads to judge health. The connection details for
    each source already live in their own blocks (phoenix / kafka / redshift),
    so this block only *selects* and annotates.

    NOTE: Sentinel currently always reads Phoenix. `prediction_source` is wired
    so the topology-aware PredictionSource dispatcher can honor it later; for
    now it documents intent and drives the onboarding preflight.
    """

    mode: str = Field(
        default="sync-api",
        description="sync-api | async-kafka | batch | unknown — the inference I/O channel",
    )
    prediction_source: str = Field(
        default="phoenix",
        description="phoenix | kafka | redshift — where the live prediction feed is read from",
    )
    endpoints: list[str] = Field(
        default_factory=list, description="Detected serving endpoints, e.g. 'POST /predict' (sync-api)"
    )
    input_topic: str | None = Field(default=None, description="Kafka topic the model consumes (async-kafka)")
    output_topic: str | None = Field(default=None, description="Kafka topic predictions land on (async-kafka)")
    scheduler: str | None = Field(default=None, description="Batch orchestrator: airflow | prefect | dagster | cron | …")


class TapFieldMap(BaseModel):
    """Maps YOUR Kafka message field names → the span roles Airen expects, so a
    new topic is onboarded by editing yaml, not kafka_tap.py. All optional; the
    tap falls back to its ETA defaults when this block is absent.

    Targets written: prediction→mlre.prediction.value, actual→mlre.ground_truth.value,
    error→mlre.eval.error (computed |prediction-actual| if `error` is null),
    model_version→mlre.model.version, timestamp→mlre.timestamp,
    each inputs[f]→mlre.input.<f> (so observation.segments = input.<f>).
    """

    prediction: str | None = None
    actual: str | None = None
    error: str | None = None          # null → compute |prediction - actual| when both present
    model_version: str | None = None
    timestamp: str | None = None
    inputs: list[str] = Field(default_factory=list, description="message fields → mlre.input.<name>; [] = copy all scalar fields")


class TapConfig(BaseModel):
    """How the Kafka→Phoenix tap turns this service's messages into spans."""

    field_map: TapFieldMap = Field(default_factory=TapFieldMap)


class RemediationConfig(BaseModel):
    """How far Airen goes on the fix → merge → deploy → validate loop.

    Merging code is irreversible and outward-facing, so `auto_merge` defaults
    OFF: out of the box Airen prepares a ready PR and asks a human to merge.
    Turn it on per-service to let Airen merge after Slack approval.
    """

    auto_merge: bool = False                  # merge the PR on approval? (3-gated: --execute + approval + this)
    merge_method: str = "squash"              # squash | merge | rebase
    deploy_grace_minutes: int = 5             # wait after merge for CI/CD before validating
    validation_poll_interval_sec: int = 120   # how often to re-check the live feed
    validation_max_attempts: int = 10         # cap on validation polls (timeout)
    recovery_window_polls: int = 2            # consecutive healthy polls required to call PASS
    max_reinvestigate_attempts: int = 2       # times to loop back to Investigator on FAIL


class MetricSpec(BaseModel):
    """A user-declared custom metric Sentinel should compute and watch.

    Augments Airen's default battery with domain knowledge. Each is computed
    over the window from one span/row attribute; a ratio vs baseline >1 always
    means 'worse' (the direction handles error- vs score-type metrics).
    """

    name: str
    attribute: str = Field(description="span/row attribute to aggregate, e.g. eval.latency_ms")
    agg: str = Field(default="mean", description="mean | p50 | p95 | p99 | sum | count | rate | null_rate")
    direction: str = Field(default="lower_is_better", description="lower_is_better | higher_is_better")
    baseline: float | None = Field(default=None, description="Explicit baseline; if null, the earlier half of the window is used")
    warn_ratio: float = 1.5
    critical_ratio: float = 2.0
    segment_by: str | None = Field(default=None, description="Optional attribute to break the metric down by")


class ReliabilityConfig(BaseModel):
    """Airen's always-on, model-agnostic reliability battery — the safety net
    UNDER the user's custom metrics. These checks run on every service with no
    config, comparing the recent half of the window to the earlier half (so they
    need no pre-computed baseline). The user can't define these away, only
    opt-out individual checks that are noisy for their app.

    Available check names: volume_drop, silence, error_rate, latency_drift,
    null_rate, input_drift_auto, output_drift, schema_drift.
    """

    enable: bool = True
    disabled_checks: list[str] = Field(
        default_factory=list,
        description="Names of default checks to turn off for this service (opt-out).",
    )
    # tunables (sensible defaults; override per-service if noisy)
    volume_drop_warn: float = 0.4        # recent rate < 60% of earlier → warn
    volume_drop_critical: float = 0.7    # < 30% of earlier → critical
    silence_warn_frac: float = 0.5       # no spans in the last 50% of the window
    null_rate_warn: float = 0.2
    error_rate_warn: float = 0.05
    error_rate_critical: float = 0.2
    latency_warn_ratio: float = 1.5      # recent p95 latency vs earlier
    latency_critical_ratio: float = 2.0
    drift_psi_warn: float = 0.10
    drift_psi_critical: float = 0.25


class ObservationConfig(BaseModel):
    """Describes the Phoenix span schema Sentinel reads — which attribute holds
    the per-prediction error/score, which attributes to segment on, and what
    KIND of metric it is. This is what lets Airen monitor ANY app's
    instrumentation.

    Defaults are generic; onboarding (graphify + the live probe) fills these in
    per service, so a real config never relies on these placeholders.
    """

    problem_type: str = Field(
        default="regression",
        description="regression | classification | ranking — informs narrative + metric handling",
    )
    error_attribute: str = Field(
        default="eval.error",
        description="Span attribute Sentinel averages into the headline metric "
                    "(e.g. eval.error, eval.abs_pct_error, eval.is_correct)",
    )
    metric_unit: str = Field(
        default="", description="Unit label for narratives — minutes, %, hours, …"
    )
    metric_direction: str = Field(
        default="lower_is_better",
        description="lower_is_better (errors) | higher_is_better (accuracy/F1). "
                    "Drives how the ratio-vs-baseline is computed.",
    )
    segments: list[str] = Field(
        default_factory=list,
        description="Span attributes to group by for per-segment health breakdowns "
                    "(set per service by onboarding; e.g. input.region)",
    )


class SentinelThresholds(BaseModel):
    """Per-service tuning of the health verdict."""

    baseline_mae_minutes: float = Field(
        default=150.0, description="Historical 'healthy' MAE for ratio comparison"
    )
    warning_ratio: float = 1.2
    critical_ratio: float = 2.0
    default_lookback_minutes: int = 2880  # 48h

    # PSI (Population Stability Index) drift detection.
    # Maps attribute name (the bare key, e.g. "api_fetch_limit") to the
    # baseline expected value. Production distribution is compared to a
    # 100%-on-baseline distribution; PSI flags drift.
    attribute_baselines: dict[str, str] = Field(
        default_factory=dict,
        description="e.g., {'api_fetch_limit': '184'} — training-time expected value per attribute",
    )
    psi_warning: float = 0.10  # moderate drift threshold
    psi_critical: float = 0.25  # major drift threshold


# ───────────────────────────────────────────────────────────────────────
#  Top-level config — what airen.yaml validates against
# ───────────────────────────────────────────────────────────────────────
class AirenServiceConfig(BaseModel):
    """One onboarded ML service. Loaded from services/<name>/airen.yaml."""

    service: ServiceInfo
    phoenix: PhoenixConfig
    serving: ServingConfig = Field(default_factory=ServingConfig)
    observation: ObservationConfig = Field(default_factory=ObservationConfig)
    reliability: ReliabilityConfig = Field(default_factory=ReliabilityConfig)
    remediation: RemediationConfig = Field(default_factory=RemediationConfig)
    tap: TapConfig = Field(default_factory=TapConfig)
    metrics: list[MetricSpec] = Field(
        default_factory=list,
        description="User-declared custom metrics Sentinel computes + watches (augments the default battery).",
    )
    github: GithubConfig | None = None
    slack: SlackConfig = Field(default_factory=SlackConfig)
    jira: JiraConfig = Field(default_factory=JiraConfig)
    redshift: RedshiftConfig | None = None
    mlflow: MLflowConfig | None = None
    kafka: KafkaConfig | None = None
    s3: S3Config | None = None
    sentinel: SentinelThresholds = Field(default_factory=SentinelThresholds)
    model_registry: list[ModelRegistryEntry] = Field(
        default_factory=list,
        description="Each production model_family Airen monitors → its training-code "
                    "provenance. Sentinel emits GOVERNANCE_RISK signals for entries "
                    "with missing/incomplete provenance. Investigator resolves any "
                    "anomalous model_family to its actual training script via MLflow.",
    )


# ───────────────────────────────────────────────────────────────────────
#  Loader + discovery
# ───────────────────────────────────────────────────────────────────────
def load_service_config(name_or_path: str | Path) -> AirenServiceConfig:
    """Resolve a service name OR explicit path to its airen.yaml and parse it.

    Resolution order:
      1. If the arg is an existing file, parse it directly.
      2. If it's an existing directory containing airen.yaml, use that.
      3. Otherwise try services/<arg>/airen.yaml (production registry),
         then demo/services/<arg>/airen.yaml (demo fixtures fallback).

    Raises FileNotFoundError if nothing resolves.
    """
    candidates: list[Path] = []
    p = Path(name_or_path)
    if p.is_file():
        candidates.append(p)
    elif p.is_dir() and (p / "airen.yaml").is_file():
        candidates.append(p / "airen.yaml")
    else:
        candidates.append(SERVICES_DIR / str(name_or_path) / "airen.yaml")
        candidates.append(DEMO_SERVICES_DIR / str(name_or_path) / "airen.yaml")

    for c in candidates:
        if c.is_file():
            raw = yaml.safe_load(c.read_text()) or {}
            return AirenServiceConfig.model_validate(raw)

    available = list_available_services()
    raise FileNotFoundError(
        f"Service config not found for {name_or_path!r}. "
        f"Tried: {[str(c) for c in candidates]}. "
        f"Available services: {available}"
    )


def list_available_services(include_demo: bool = False) -> list[str]:
    """Discover services. Production registry (services/) only by default; pass
    include_demo=True to also include the demo fixtures (used internally for
    project-name resolution, NOT shown to the user by `--list`)."""
    dirs = [SERVICES_DIR] + ([DEMO_SERVICES_DIR] if include_demo else [])
    names: set[str] = set()
    for base in dirs:
        if base.exists():
            names.update(
                d.name for d in base.iterdir()
                if d.is_dir() and (d / "airen.yaml").is_file()
            )
    return sorted(names)


def load_all_services(include_demo: bool = False) -> dict[str, AirenServiceConfig]:
    """Load every discoverable service config. Bad configs are skipped with a warning."""
    out: dict[str, AirenServiceConfig] = {}
    for name in list_available_services(include_demo=include_demo):
        try:
            out[name] = load_service_config(name)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  Skipping service {name!r}: {type(e).__name__}: {e}")
    return out
