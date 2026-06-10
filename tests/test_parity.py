"""Tests for the parity-audit plumbing — the repo file tools, the report schema,
and the runner's message/hint builders. The agent's LLM reasoning runs live
(needs creds), so it's not exercised here; everything around it is."""

from __future__ import annotations

import pytest

from airen.schemas import ModelParityFinding, ParityReport


# ── repo file tools (against a temp 'repo', no real clone) ──────────────
@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    (tmp_path / "predict" / "lstm").mkdir(parents=True)
    (tmp_path / "predict" / "lstm" / "features.py").write_text(
        "FINAL_FEATURES = ['a', 'b']\nscaler.transform(x)\n"
    )
    (tmp_path / "train").mkdir()
    (tmp_path / "train" / "model.py").write_text("model.fit(X, y)\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "junk.py").write_text("ignore me\n")
    import airen.tools.repo_files as rf

    monkeypatch.setattr(rf, "_repo_root", lambda repo, branch=None: tmp_path.resolve())
    return tmp_path


def test_list_repo_tree_skips_git(fake_repo):
    from airen.tools.repo_files import list_repo_tree

    out = list_repo_tree("x/y", subdir="predict")
    assert out["n_files"] == 1
    assert out["files"] == ["predict/lstm/features.py"]
    assert not any(".git" in f for f in out["files"])


def test_read_repo_file(fake_repo):
    from airen.tools.repo_files import read_repo_file

    out = read_repo_file("x/y", "predict/lstm/features.py")
    assert "FINAL_FEATURES" in out["content"]
    assert out["truncated"] is False


def test_read_repo_file_path_escape_guarded(fake_repo):
    from airen.tools.repo_files import read_repo_file

    out = read_repo_file("x/y", "../../etc/passwd")
    assert "error" in out  # escape blocked or not found


def test_grep_repo(fake_repo):
    from airen.tools.repo_files import grep_repo

    out = grep_repo("x/y", r"\.fit\(")
    assert out["n_hits"] == 1
    assert "train/model.py" in out["hits"][0]


# ── schema ──────────────────────────────────────────────────────────────
def test_parity_report_schema():
    rep = ParityReport(
        service="svc", serving_repo="o/serve", training_repo="o/train",
        served_models=["LSTM", "PatchTST"],
        findings=[
            ModelParityFinding(model_family="LSTM", paired=True, status="SKEW_RISK",
                               confidence=0.7, skews=["hardcoded feature list @ x.py:10"]),
            ModelParityFinding(model_family="PatchTST", paired=False, status="NO_TRAINING_CODE",
                               confidence=0.9),
        ],
        summary="2 models; 1 skew risk, 1 untrainable.",
    )
    j = rep.model_dump_json()
    back = ParityReport.model_validate_json(j)
    assert len(back.findings) == 2 and back.findings[0].status == "SKEW_RISK"


# ── runner message/hint builders ─────────────────────────────────────────
def _cfg(training_repo=None):
    from airen.config import AirenServiceConfig

    return AirenServiceConfig.model_validate({
        "service": {"name": "svc"},
        "phoenix": {"project_name": "svc-prediction"},
        "github": {"repo": "o/serve", "default_branch": "production",
                   "training_repo": training_repo, "training_branch": "main"},
        "observation": {"segments": ["input.model_family", "input.region"]},
        "model_registry": [{"model_family": "LSTM", "experiment_id": "E1"}],
    })


def test_hint_builder_pulls_models():
    from airen.run_parity import _served_model_hints

    hints = _served_model_hints(_cfg())
    assert "LSTM" in hints and "model_family" in hints


def test_message_mentions_repos_and_training_fallback():
    from airen.run_parity import _build_message

    with_train = _build_message(_cfg(training_repo="o/train"))
    assert "o/serve" in with_train and "o/train" in with_train
    no_train = _build_message(_cfg(training_repo=None))
    assert "NONE CONFIGURED" in no_train and "NO_TRAINING_CODE" in no_train


def test_agent_constructs():
    # default backend = gemini string, no creds needed to build the Agent object
    from airen.agents.parity_auditor import root_agent

    assert root_agent.name == "parity_auditor"
    assert len(root_agent.tools) >= 4
