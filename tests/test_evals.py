"""Tests for Airen's self-evaluation + self-improvement (the Arize-track core).

Mock-friendly: the code evaluators and the learning store need no LLM, so these
run offline. The LLM-as-judge path is exercised only for its backend mapping
(no live call)."""

from __future__ import annotations

import importlib

import pytest

from airen.evals.evaluators import run_code_evaluators
from airen.evals.rca_eval import evaluate_investigation
from airen.evals.types import EvalScore, RcaEvalResult
from airen.schemas import Anomaly, HealthStatus, InvestigatorVerdict, SentinelVerdict


def _sentinel(metric: str = "input_drift_auto") -> SentinelVerdict:
    return SentinelVerdict(
        project_name="p", n_spans_analyzed=100, status=HealthStatus.CRITICAL,
        severity_score=0.8, recommended_action="INVESTIGATE", summary="x",
        anomalies=[Anomaly(metric=metric, segment="input.region", current_value=0.4,
                           baseline_value=0.05, ratio=8.0, description="region drifted")],
    )


def _weak(sent: SentinelVerdict) -> InvestigatorVerdict:
    return InvestigatorVerdict(
        triggering_anomaly=sent.anomalies[0],
        root_cause_hypothesis="something is off", confidence=0.97,
        code_evidence=[], data_evidence=[], recommended_fix="", further_investigation_needed=[])


def _strong(sent: SentinelVerdict) -> InvestigatorVerdict:
    return InvestigatorVerdict(
        triggering_anomaly=sent.anomalies[0],
        root_cause_hypothesis="Data/concept drift: region distribution shifted; no code cause found.",
        confidence=0.7, code_evidence=[],
        data_evidence=["input.region PSI 0.40 vs 0.05 baseline"],
        recommended_fix="RETRAIN on the last 30 days.", further_investigation_needed=[])


# ── code evaluators discriminate good from bad ──────────────────────────
def test_code_evaluators_discriminate():
    sent = _sentinel()
    weak = evaluate_investigation(sent, _weak(sent), use_llm=False)
    strong = evaluate_investigation(sent, _strong(sent), use_llm=False)
    assert not weak.passed and weak.overall < 0.5
    assert strong.passed and strong.overall >= 0.9
    assert strong.overall > weak.overall


def test_overconfident_no_evidence_is_flagged():
    sent = _sentinel()
    scores = {s.name: s for s in run_code_evaluators(sent, _weak(sent))}
    assert scores["confidence_calibration"].label == "overconfident"
    assert scores["has_evidence"].score == 0.0
    assert scores["recommendation_actionable"].score == 0.0


def test_offtarget_anomaly_is_flagged():
    sent = _sentinel(metric="volume_drop")
    inv = _strong(_sentinel(metric="input_drift_auto"))  # triggering anomaly = input_drift_auto
    scores = {s.name: s for s in run_code_evaluators(sent, inv)}
    assert scores["addresses_anomaly"].score == 0.0


# ── RcaEvalResult helpers ───────────────────────────────────────────────
def test_result_helpers():
    r = RcaEvalResult(scores=[
        EvalScore("a", 1.0, "ok", "good"),
        EvalScore("b", 0.0, "bad", "missing X"),
    ], pass_threshold=0.6)
    assert r.overall == 0.5 and not r.passed
    assert r.weakest().name == "b"
    assert [s.name for s in r.failing()] == ["b"]
    assert "missing X" in r.critique()


def test_score_clamps():
    assert EvalScore("x", 5.0, "l", "e").score == 1.0
    assert EvalScore("x", -2.0, "l", "e").score == 0.0


# ── reflection gating ───────────────────────────────────────────────────
def test_should_reflect_gating(monkeypatch):
    from airen.evals import learning
    sent = _sentinel()
    weak = evaluate_investigation(sent, _weak(sent), use_llm=False)
    strong = evaluate_investigation(sent, _strong(sent), use_llm=False)
    monkeypatch.delenv("AIREN_EVAL_REFLECT", raising=False)
    assert learning.should_reflect(weak) is True
    assert learning.should_reflect(strong) is False
    monkeypatch.setenv("AIREN_EVAL_REFLECT", "0")
    assert learning.should_reflect(weak) is False  # disabled by env
    fb = learning.reflection_feedback(weak)
    assert "SELF-REVIEW" in fb and "FELL SHORT" in fb


# ── cross-run learning store round-trip ─────────────────────────────────
def test_learning_store_roundtrip(tmp_path, monkeypatch):
    import airen.evals.learning as learning
    importlib.reload(learning)
    monkeypatch.setattr(learning, "_STORE", tmp_path / "incidents.jsonl")
    sent = _sentinel(metric="input_drift_auto")

    class _Run:
        run_id = "INC-TEST"; project_name = "svc"
        sentinel_verdict = sent
        investigator_verdict = _strong(sent)
        validator_phase1 = type("V", (), {"status": __import__("airen.schemas", fromlist=["ValidatorStatus"]).ValidatorStatus.PASS})()

    assert learning.record_incident(_Run()) is True
    similar = learning.recall_similar(sent)
    assert len(similar) == 1 and similar[0]["metric"] == "input_drift_auto"
    assert "drift" in similar[0]["root_cause"].lower()
    # a different anomaly class recalls nothing
    assert learning.recall_similar(_sentinel(metric="latency_drift")) == []
    block = learning.exemplars_block(similar)
    assert "PRIOR CONFIRMED INCIDENTS" in block


# ── judge backend mapping (no live LLM call) ────────────────────────────
def test_judge_backend_mapping(monkeypatch):
    from airen.evals.judge import _backend_to_provider_model
    monkeypatch.setenv("AIREN_LLM_BACKEND", "azure")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    assert _backend_to_provider_model() == ("litellm", "azure/gpt-4o")
    monkeypatch.setenv("AIREN_LLM_BACKEND", "gemini")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    assert _backend_to_provider_model() == ("google", "gemini-2.5-flash")
