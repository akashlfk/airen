"""RepoManifest — the lightweight, deterministic understanding of one repo.

This is Airen's answer to "graphify the repo": not a heavyweight knowledge
graph, but a single regenerable JSON describing what the repo *is* — its
model(s), frameworks, serving APIs, I/O, observability and tracking hooks,
and the feature constants worth watching.

Two consumers:
  • the onboarding agent — prefills services/<name>/airen.yaml from it
  • the freshness poller — stores commit_sha here to know when to re-graphify

Persisted to  graphs/<owner>__<repo>.json  (or graphs/<slug>.json for local
paths). Always safe to delete and regenerate.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

# Manifests live at <repo-root>/graphs/
GRAPHS_DIR = Path(__file__).resolve().parent.parent.parent / "graphs"


class DetectedModel(BaseModel):
    """A model class / estimator found in the repo."""

    name: str = Field(description="Class or estimator name, e.g. 'PatchTST', 'RandomForestClassifier'")
    kind: str = Field(description="deep-learning | tree-ensemble | linear | timeseries | unknown")
    framework: str | None = Field(default=None, description="torch | tensorflow | sklearn | xgboost | …")
    file: str | None = None
    line: int | None = None
    confidence: float = Field(default=0.5, description="0–1 heuristic confidence")


class DetectedAPI(BaseModel):
    """A serving endpoint (FastAPI / Flask) found in the repo."""

    method: str = Field(description="GET | POST | …")
    path: str
    handler: str | None = None
    file: str | None = None
    line: int | None = None
    request_model: str | None = Field(default=None, description="Pydantic request type, if found")
    response_model: str | None = Field(default=None, description="Return annotation, if found")


class Observability(BaseModel):
    """Phoenix / OTel instrumentation found in the repo."""

    phoenix_project: str | None = Field(default=None, description="project_name literal if found in code")
    uses_phoenix: bool = False
    uses_openinference: bool = False
    uses_otel: bool = False
    files: list[str] = Field(default_factory=list)


class Tracking(BaseModel):
    """MLflow (or similar) experiment tracking found in the repo."""

    uses_mlflow: bool = False
    experiment_names: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)


class ServingTopology(BaseModel):
    """How the repo's inference layer serves predictions — the I/O channel."""

    mode: str = Field(default="unknown", description="sync-api | async-kafka | batch | unknown")
    endpoints: list[str] = Field(default_factory=list, description="'POST /predict' style, from detected APIs")
    kafka_topics: list[str] = Field(default_factory=list, description="Topic name literals found near consumers/producers")
    kafka_consumer: bool = False
    kafka_producer: bool = False
    scheduler: str | None = Field(default=None, description="airflow | prefect | dagster | luigi | cron")
    libraries: list[str] = Field(default_factory=list, description="Serving libs seen: kafka-python, confluent-kafka, …")


class ObservationSchema(BaseModel):
    """The Phoenix span schema Sentinel needs — detected from the eval/instrument code."""

    problem_type: str = Field(default="regression", description="regression | classification | ranking")
    error_attribute: str | None = Field(default=None, description="span attr holding the per-prediction error/score")
    segment_candidates: list[str] = Field(default_factory=list, description="input.* attrs that look like segmentation dims")
    eval_attributes: list[str] = Field(default_factory=list, description="all eval.* span attrs seen")


class RepoManifest(BaseModel):
    """The full deterministic understanding of one repo at one commit."""

    # provenance
    source: str = Field(description="owner/repo (remote) or absolute local path")
    is_remote: bool = True
    commit_sha: str | None = Field(default=None, description="HEAD at graphify time — drives the freshness poll")
    default_branch: str | None = None
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # what kind of repo
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list, description="Human labels: PyTorch, FastAPI, MLflow, …")
    dependencies: list[str] = Field(default_factory=list)
    application_type: str = Field(default="unknown", description="model-serving-api | batch-pipeline | library | unknown")

    # the ML specifics
    models: list[DetectedModel] = Field(default_factory=list)
    apis: list[DetectedAPI] = Field(default_factory=list)
    serving: ServingTopology = Field(default_factory=ServingTopology)
    observation: ObservationSchema = Field(default_factory=ObservationSchema)
    observability: Observability = Field(default_factory=Observability)
    tracking: Tracking = Field(default_factory=Tracking)
    feature_constants: list[str] = Field(
        default_factory=list,
        description="Candidate attributes_of_interest — module-level numeric hyperparam-like names",
    )
    # Does this repo TRAIN the models, or only SERVE them? If serving-only, the
    # training code lives elsewhere — onboarding asks for the training repo so
    # Airen can check train↔serve parity.
    has_training_code: bool = False
    training_evidence: list[str] = Field(default_factory=list)

    # one-line narrative (optional; filled by the agent, possibly via LLM)
    summary: str | None = None

    # ── persistence ──────────────────────────────────────────────────
    @staticmethod
    def slug_for(source: str, is_remote: bool) -> str:
        if is_remote:
            owner, _, name = source.partition("/")
            return f"{owner}__{name}" if name else owner
        # local path → safe slug from the last path component
        return "local__" + Path(source).name.replace(" ", "_")

    def path(self) -> Path:
        return GRAPHS_DIR / f"{self.slug_for(self.source, self.is_remote)}.json"

    def save(self) -> Path:
        GRAPHS_DIR.mkdir(parents=True, exist_ok=True)
        p = self.path()
        p.write_text(json.dumps(self.model_dump(mode="json"), indent=2))
        return p

    @classmethod
    def load(cls, source: str, is_remote: bool = True) -> "RepoManifest | None":
        p = GRAPHS_DIR / f"{cls.slug_for(source, is_remote)}.json"
        if not p.is_file():
            return None
        try:
            return cls.model_validate_json(p.read_text())
        except Exception:
            return None
