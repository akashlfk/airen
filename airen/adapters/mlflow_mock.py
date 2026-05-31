"""Mock MLflow adapter — canned training run that mirrors the Fritolay scenario.

The mock returns a training run where `api_fetch_limit` is NOT set (because
training never had that limit). That's the smoking-gun cross-reference
Investigator uses against production spans showing `api_fetch_limit=73`.
"""

from __future__ import annotations

import time
from typing import Any


class MockMLflowAdapter:
    """Returns synthetic training-run data resembling FK's tl-eta-prod experiment."""

    def __init__(self) -> None:
        self.tracking_uri = "mock://airen-mlflow"

    def get_latest_run(self, experiment_name: str) -> dict[str, Any]:
        return _mock_run(experiment_name)

    def get_run(self, run_id: str) -> dict[str, Any]:
        return _mock_run("tl-eta-prod", run_id=run_id)

    def get_latest_model_version(self, model_name: str) -> dict[str, Any]:
        return {
            "model_name": model_name,
            "version": "3",
            "run_id": "mock_run_d9de2cc6",
            "current_stage": "Production",
            "source": "s3://mock-models/tl_eta_lstm_v3",
        }

    def list_recent_runs(
        self, experiment_name: str, max_results: int = 10
    ) -> list[dict[str, Any]]:
        return [_mock_run(experiment_name, run_id=f"mock_run_{i}") for i in range(min(3, max_results))]

    def get_training_provenance(
        self, experiment_name: str, expected_repo: str | None = None
    ) -> dict[str, Any]:
        """Canned provenance matching the demo's three production models.

        PatchTST = healthy provenance (git commit + repo set).
        CHEP variants = ungoverned (local Jupyter / personal-disk script).
        Anything else = healthy default for offline tests.
        """
        ename = experiment_name.lower()
        if "chep_classification" in ename or "chep-classification" in ename:
            return {
                "experiment_name": experiment_name,
                "experiment_id": "3835",
                "run_id": "mock_chep_class_001",
                "run_name": "chep_classification_ensemble",
                "source_path": "/home/eng1/vik/chep_classification.py",
                "git_commit": None,
                "git_branch": None,
                "git_repo": None,
                "user": "root",
                "runtime": "LOCAL",
                "has_git_traceability": False,
                "reproducibility_warnings": [
                    "no git commit logged — training source is not in version control",
                    "source is a local-disk path on a personal/training machine",
                ],
            }
        if "first" in ename and "checkcall" in ename:
            return {
                "experiment_name": experiment_name,
                "experiment_id": "3830",
                "run_id": "mock_chep_first_001",
                "run_name": "chep_first_checkcall_lgbm",
                "source_path": "/home/eng1/vik/chep_first_accuracy.py",
                "git_commit": None,
                "git_branch": None,
                "git_repo": None,
                "user": "root",
                "runtime": "LOCAL",
                "has_git_traceability": False,
                "reproducibility_warnings": [
                    "no git commit logged — training source is not in version control",
                    "source is a local-disk path on a personal/training machine",
                ],
            }
        # Default: healthy provenance (PatchTST-style)
        return {
            "experiment_name": experiment_name,
            "experiment_id": "2455",
            "run_id": "mock_run_d9de2cc6",
            "run_name": "mh_cohort_3_top_1_100",
            "source_path": "/tb-disk/dynamic_eta_prediction/training_pipeline/auto_trainer/training_pipeline",
            "git_commit": "6dd30f98ecc567e7d617ec3c59cf7ee1a9f1eb84",
            "git_branch": None,
            "git_repo": expected_repo or "cloudqwest/dynamic_eta_prediction",
            "user": "ubuntu",
            "runtime": "LOCAL",
            "has_git_traceability": True,
            "reproducibility_warnings": [],
        }

    def close(self) -> None:
        return


def _mock_run(experiment_name: str, run_id: str = "mock_run_d9de2cc6") -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    return {
        "run_id": run_id,
        "experiment_id": "42",
        "experiment_name": experiment_name,
        "status": "FINISHED",
        "start_time_ms": now_ms - 7 * 24 * 60 * 60 * 1000,  # 7 days ago
        "end_time_ms": now_ms - 7 * 24 * 60 * 60 * 1000 + 3600 * 1000,
        "artifact_uri": "s3://mock-artifacts/tl_eta_lstm_v3",
        "params": {
            # Notice: NO api_fetch_limit here. That's the whole point — production
            # introduced a parameter the training run never saw.
            "model_family": "lstm",
            "seq_len": "48",
            "hidden_size": "128",
            "num_layers": "2",
            "batch_size": "64",
            "learning_rate": "0.001",
            "max_pings": "250",
            "training_samples": "1245000",
        },
        "metrics": {
            "train_mae_minutes": 118.4,
            "val_mae_minutes": 124.7,
            "test_mae_minutes": 127.1,
        },
        "tags": {
            "mlflow.runName": "tl_eta_lstm_v3_train",
            "model_version": "v3",
            "git_sha": "abc123def456",
        },
        "run_name": "tl_eta_lstm_v3_train",
    }
