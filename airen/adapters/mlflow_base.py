"""Abstract MLflow adapter — the contract every implementation honors.

Reads model lineage from MLflow:
  - latest run for an experiment
  - run params (training-time settings — e.g. was `api_fetch_limit` set?)
  - run metrics (training-time MAE — baseline for Sentinel)
  - registered model versions

Used primarily by Investigator (to cross-reference training-time vs
production-time attribute values) and Validator (to confirm deployed model
version matches what we expect).
"""

from __future__ import annotations

from typing import Any, Protocol


class MLflowAdapter(Protocol):
    """Read-only MLflow tracking client."""

    def get_latest_run(self, experiment_name: str) -> dict[str, Any]:
        """Newest run in the experiment. Returns dict with run_id, params, metrics, tags."""
        ...

    def get_run(self, run_id: str) -> dict[str, Any]:
        """One specific run by id."""
        ...

    def get_latest_model_version(self, model_name: str) -> dict[str, Any]:
        """Latest version of a registered model (run_id, version, current_stage)."""
        ...

    def list_recent_runs(
        self, experiment_name: str, max_results: int = 10
    ) -> list[dict[str, Any]]:
        """Recent runs newest-first."""
        ...

    def get_training_provenance(
        self, experiment_name: str, expected_repo: str | None = None
    ) -> dict[str, Any]:
        """Return source-code provenance for the experiment's latest run.

        Shape matches `airen.schemas.TrainingProvenance` (returned as dict so
        adapters don't need to import the schema). Includes derived flags:
        `has_git_traceability` and `reproducibility_warnings` (list of strings).

        Used by Investigator to fetch the training script and by Sentinel to
        emit a GOVERNANCE_RISK signal for ungoverned models.
        """
        ...

    def close(self) -> None:
        ...
