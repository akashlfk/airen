"""Real MLflow adapter — uses mlflow-skinny tracking client.

Auth model at FK: none. VPN-gated. Direct REST hits to MLFLOW_TRACKING_URI.
Same VPN routing constraint as Kafka/Redshift — only reachable from FK VM.

Standard env vars consumed by the mlflow client:
    MLFLOW_TRACKING_URI       (required)
    MLFLOW_TRACKING_USERNAME  (optional — only if FK adds basic auth later)
    MLFLOW_TRACKING_PASSWORD  (optional — same)
"""

from __future__ import annotations

import os
from typing import Any


class RealMLflowAdapter:
    def __init__(self) -> None:
        try:
            from mlflow.tracking import MlflowClient  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "mlflow-skinny not installed. Install with:\n"
                "    conda run -n mlre pip install 'mlflow-skinny>=2.16'"
            ) from e

        uri = os.environ.get("MLFLOW_TRACKING_URI", "").strip()
        if not uri:
            raise RuntimeError(
                "MLFLOW_TRACKING_URI not set. Add it to .env "
                "(e.g. https://mlflow-dev.fourkites.com). "
                "VPN must be active to reach internal MLflow hosts."
            )
        from mlflow.tracking import MlflowClient

        self._client = MlflowClient(tracking_uri=uri)
        self.tracking_uri = uri

    def _resolve_experiment_id(self, name_or_id: str) -> str | None:
        # If it's already a numeric id, accept it directly
        if name_or_id.isdigit():
            return name_or_id
        exp = self._client.get_experiment_by_name(name_or_id)
        return exp.experiment_id if exp else None

    def get_latest_run(self, experiment_name: str) -> dict[str, Any]:
        exp_id = self._resolve_experiment_id(experiment_name)
        if exp_id is None:
            return {"error": f"experiment {experiment_name!r} not found"}
        runs = self._client.search_runs(
            experiment_ids=[exp_id],
            order_by=["attributes.start_time DESC"],
            max_results=1,
        )
        if not runs:
            return {"error": f"no runs in experiment {experiment_name!r}"}
        return _run_to_dict(runs[0])

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self._client.get_run(run_id)
        return _run_to_dict(run)

    def get_latest_model_version(self, model_name: str) -> dict[str, Any]:
        try:
            versions = self._client.search_model_versions(f"name='{model_name}'")
        except Exception as e:
            return {"error": f"search_model_versions failed: {e}"}
        if not versions:
            return {"error": f"no versions for model {model_name!r}"}
        latest = max(versions, key=lambda v: int(v.version))
        return {
            "model_name": model_name,
            "version": latest.version,
            "run_id": latest.run_id,
            "current_stage": latest.current_stage,
            "source": latest.source,
        }

    def list_recent_runs(
        self, experiment_name: str, max_results: int = 10
    ) -> list[dict[str, Any]]:
        exp_id = self._resolve_experiment_id(experiment_name)
        if exp_id is None:
            return []
        runs = self._client.search_runs(
            experiment_ids=[exp_id],
            order_by=["attributes.start_time DESC"],
            max_results=max_results,
        )
        return [_run_to_dict(r) for r in runs]

    def get_training_provenance(
        self, experiment_name: str, expected_repo: str | None = None
    ) -> dict[str, Any]:
        """Build a TrainingProvenance-shape dict from the latest run's tags."""
        exp_id = self._resolve_experiment_id(experiment_name)
        if exp_id is None:
            return {
                "experiment_name": experiment_name,
                "experiment_id": "",
                "has_git_traceability": False,
                "reproducibility_warnings": [f"experiment {experiment_name!r} not found in MLflow"],
            }
        runs = self._client.search_runs(
            experiment_ids=[exp_id],
            order_by=["attributes.start_time DESC"],
            max_results=1,
        )
        if not runs:
            return {
                "experiment_name": experiment_name,
                "experiment_id": exp_id,
                "has_git_traceability": False,
                "reproducibility_warnings": ["no runs in this experiment"],
            }
        run = runs[0]
        tags = dict(run.data.tags)
        return _build_provenance(experiment_name, exp_id, run, tags, expected_repo)

    def close(self) -> None:
        # MlflowClient holds no persistent connection
        return


def _build_provenance(
    experiment_name: str,
    experiment_id: str,
    run: Any,
    tags: dict[str, str],
    expected_repo: str | None,
) -> dict[str, Any]:
    """Pure function — turns MLflow run + tags into the provenance dict + warnings.

    Extracted so it's testable without an MLflow server. Used by both real and
    mock adapters.
    """
    source_path = tags.get("mlflow.source.name")
    git_commit = tags.get("mlflow.source.git.commit") or None
    git_branch = tags.get("mlflow.source.git.branch") or None
    runtime = tags.get("mlflow.source.type")
    user = tags.get("mlflow.user")
    run_name = tags.get("mlflow.runName")
    run_id = run.info.run_id if hasattr(run, "info") else None

    warnings: list[str] = []
    if not git_commit:
        warnings.append("no git commit logged — training source is not in version control")

    # Jupyter-cell sources show up as ipykernel_launcher paths
    if source_path and "ipykernel" in source_path:
        warnings.append("source is a Jupyter kernel — not reproducible from a script")
    if source_path and source_path.startswith("/home/") and not git_commit:
        warnings.append("source is a local-disk path on a personal/training machine")
    if not source_path:
        warnings.append("no source path tagged — provenance is unknown")

    has_traceability = bool(git_commit) and len(warnings) == 0
    if has_traceability and expected_repo:
        # If we have a commit AND an expected_repo, that's the strongest signal
        pass

    return {
        "experiment_name": experiment_name,
        "experiment_id": experiment_id,
        "run_id": run_id,
        "run_name": run_name,
        "source_path": source_path,
        "git_commit": git_commit,
        "git_branch": git_branch,
        "git_repo": expected_repo,  # set externally; MLflow rarely tags repoURL at FK
        "user": user,
        "runtime": runtime,
        "has_git_traceability": has_traceability,
        "reproducibility_warnings": warnings,
    }


def _run_to_dict(run: Any) -> dict[str, Any]:
    """Flatten an mlflow Run into a plain dict (params, metrics, tags, info)."""
    info = run.info
    data = run.data
    return {
        "run_id": info.run_id,
        "experiment_id": info.experiment_id,
        "status": info.status,
        "start_time_ms": info.start_time,
        "end_time_ms": info.end_time,
        "artifact_uri": info.artifact_uri,
        "params": dict(data.params),
        "metrics": dict(data.metrics),
        "tags": dict(data.tags),
        "run_name": data.tags.get("mlflow.runName"),
    }
