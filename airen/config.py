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
SERVICES_DIR = Path(__file__).resolve().parent.parent / "services"


# ───────────────────────────────────────────────────────────────────────
#  Sub-configs (each block of airen.yaml)
# ───────────────────────────────────────────────────────────────────────
class ServiceInfo(BaseModel):
    """Identity metadata for the service."""

    name: str = Field(description="Short identifier, e.g. 'tl-eta'")
    description: str | None = None
    owner_team: str | None = Field(default=None, description="Slack handle / team name")


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
    """Where historical predictions / actuals live for batch analysis (FK VM required)."""

    table: str | None = None
    actuals_table: str | None = None
    conn_env_var: str = "REDSHIFT_DSN"
    enable: bool = False


class MLflowConfig(BaseModel):
    """Model registry / training run lineage."""

    experiment_name: str | None = None
    tracking_uri_env_var: str = "MLFLOW_TRACKING_URI"
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
    The training pipeline writes one JSON per model version with MAE +
    attribute distributions. Sentinel prefers these over hand-tuned yaml
    values when present, falling back gracefully when the object is missing
    or AWS creds aren't configured.
    """

    bucket_env_var: str = "S3_BASELINES_BUCKET"
    # Specific version to pin to; None = always fetch latest.json
    model_version: str | None = None
    enable: bool = False


class KafkaConfig(BaseModel):
    """Peek-only Kafka access for live validation (FK VM required)."""

    bootstrap_env_var: str = "KAFKA_BOOTSTRAP_SERVERS"
    input_topic_env_var: str = "KAFKA_INPUT_TOPIC"
    output_topic_env_var: str = "KAFKA_OUTPUT_TOPIC"
    enable: bool = False


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
      3. Otherwise try services/<arg>/airen.yaml relative to the repo root.

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


def list_available_services() -> list[str]:
    """Discover services by scanning services/<name>/airen.yaml."""
    if not SERVICES_DIR.exists():
        return []
    return sorted(
        d.name
        for d in SERVICES_DIR.iterdir()
        if d.is_dir() and (d / "airen.yaml").is_file()
    )


def load_all_services() -> dict[str, AirenServiceConfig]:
    """Load every services/<name>/airen.yaml. Bad configs are skipped with a warning."""
    out: dict[str, AirenServiceConfig] = {}
    for name in list_available_services():
        try:
            out[name] = load_service_config(name)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  Skipping service {name!r}: {type(e).__name__}: {e}")
    return out
