"""Airen Orchestrator — the state machine that conducts the agent team.

Design philosophy (see [[agent-architecture-pattern]]):
  - Orchestration logic is DETERMINISTIC Python, not LLM-driven.
  - Each specialist agent (Sentinel, Investigator, RCA Writer) is an ADK Agent
    with its own model, tools, and structured output.
  - The Orchestrator picks which one to call when, based on the previous
    agent's verdict — no LLM routing decisions.

State flow (reachable today):
    IDLE → MONITORING
         ├── HEALTHY     → HEALTHY_NO_ACTION → RESOLVED   (no anomaly, stop)
         └── WARNING/CRITICAL → ANOMALY_DETECTED
              → INVESTIGATING → RCA_DRAFTING → NOTIFYING → RESOLVED

Future states (when Remediation + Validator land):
    NOTIFYING → AWAITING_APPROVAL → REMEDIATING → VALIDATING → RESOLVED

Each transition records an IncidentEvent. The full IncidentRun is returned to
the caller and can be persisted to the web UI store.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# logs/ directory used for incremental run persistence (SSE consumer reads this)
_LOGS_DIR = Path(__file__).resolve().parent.parent.parent / "logs"

from google.adk.runners import InMemoryRunner
from google.genai import types
from opentelemetry import trace

from airen.agents.investigator import root_agent as investigator_agent
from airen.agents.rca_writer import root_agent as rca_writer_agent
from airen.agents.remediation import Remediator
from airen.agents.sentinel import root_agent as sentinel_agent
from airen.agents.validator import Validator
from airen.config import AirenServiceConfig
from airen.llm_factory import extract_json
from airen.mocks import (
    is_mock_mode,
    mock_incident_report,
    mock_investigator_verdict,
    mock_sentinel_verdict,
)
from airen.schemas import (
    HealthStatus,
    IncidentEvent,
    IncidentReport,
    IncidentRun,
    InvestigatorVerdict,
    OrchestratorState,
    RemediationActionType,
    RemediationPlan,
    SentinelVerdict,
    ValidatorStatus,
)

APP_NAME = "airen"
USER_ID = "akash"

# OTEL tracer for orchestrator-level spans (the agent meta-trace in Phoenix)
_tracer = trace.get_tracer("airen.orchestrator")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_run_id() -> str:
    return f"INC-{secrets.token_hex(3).upper()}"


def _confidence_band(c: float) -> str:
    if c >= 0.85:
        return "high"
    if c >= 0.6:
        return "medium"
    return "low"


class AirenOrchestrator:
    """The state machine conductor. One instance per incident cycle.

    Usage:
        orch = AirenOrchestrator(config=load_service_config("<service>"))
        run = await orch.run()
        print(run.final_state, run.run_id)
        # Inspect run.events for the full state trace.
    """

    def __init__(
        self,
        config: AirenServiceConfig | None = None,
        # legacy / ad-hoc params (used when no config is provided)
        project_name: str | None = None,
        lookback_min: int | None = None,
        repo: str | None = None,
        cooldown_sec: int | None = None,
        post_to_slack: bool | None = None,
        # remediation — plan is always generated, execute is gated:
        execute_remediation: bool = False,
    ) -> None:
        # Resolve every field, preferring the explicit kwarg over the config.
        if config is None:
            if project_name is None:
                raise ValueError("Either `config=` or `project_name=` is required")
            self.project_name = project_name
            self.repo = repo or ""
            self.lookback_min = lookback_min or 2880
            self.alert_channel: str | None = None  # falls back to SLACK_CHANNEL
            self.post_to_slack = post_to_slack if post_to_slack is not None else True
            self.service_name = project_name
        else:
            self.project_name = project_name or config.phoenix.project_name
            self.repo = repo or (config.github.repo if config.github else "")
            self.lookback_min = lookback_min or config.sentinel.default_lookback_minutes
            self.alert_channel = config.slack.alert_channel
            self.post_to_slack = (
                post_to_slack if post_to_slack is not None else config.slack.enable
            )
            self.service_name = config.service.name
        self.config = config
        self.execute_remediation = execute_remediation
        self._remediator = Remediator(config=config)
        self._validator = Validator(config=config)
        # Free-tier rate limit recovery; set to 0 for mock mode (no LLM calls).
        if cooldown_sec is None:
            cooldown_sec = (
                0 if is_mock_mode() else int(os.environ.get("PIPELINE_COOLDOWN_SEC", "65"))
            )
        self.cooldown_sec = cooldown_sec
        # Mutable state — single source of truth for the run
        self.state: OrchestratorState = OrchestratorState.IDLE
        self.run: IncidentRun = IncidentRun(
            run_id=_new_run_id(),
            project_name=self.project_name,
            started_at=_now(),
            final_state=OrchestratorState.IDLE,
        )

    # ─────────────────────────────────────────────────────────────────
    #  Top-level run
    # ─────────────────────────────────────────────────────────────────
    async def run_cycle(self) -> IncidentRun:
        """Execute one full Orchestrator cycle. Returns the IncidentRun."""
        with _tracer.start_as_current_span("airen.orchestrator.cycle") as span:
            span.set_attribute("airen.run_id", self.run.run_id)
            span.set_attribute("airen.project", self.project_name)
            try:
                self._refresh_repo_understanding()
                self._ensure_baseline()
                await self._monitor_step()
                if self.state == OrchestratorState.HEALTHY_NO_ACTION:
                    self._transition(OrchestratorState.RESOLVED, "No action needed — system healthy.")
                else:
                    await self._cooldown("after Sentinel")
                    # The full fix → approve → merge → deploy → validate → reopen
                    # loop. Sets its own terminal state (RESOLVED or AWAITING_HUMAN).
                    await self._remediation_cycle()
            except Exception as e:
                self._transition(
                    OrchestratorState.FAILED,
                    f"Cycle failed: {type(e).__name__}: {e}",
                )
                self.run.error = f"{type(e).__name__}: {e}"
                span.record_exception(e)
            finally:
                self.run.finished_at = _now()
                self.run.final_state = self.state
                span.set_attribute("airen.final_state", self.state.value)
                # Cross-run learning: remember this incident (with its validated
                # flag) so future investigations of the same class can recall it.
                try:
                    from airen.evals import learning

                    learning.record_incident(self.run)
                except Exception:
                    pass
        return self.run

    def _refresh_repo_understanding(self) -> None:
        """Poll-on-cycle freshness hook: if the monitored repo's HEAD moved
        since we last graphified it, re-graphify before investigating.

        Fully best-effort — never breaks a cycle (offline / mock / no git all
        no-op). See airen.onboarding.freshness for the rationale (no webhook
        server needed; understanding is refreshed exactly when it matters).
        """
        try:
            from airen.onboarding.freshness import maybe_refresh_manifest

            status = maybe_refresh_manifest(self.config)
            if status:
                print(f"  🔄 GRAPHIFY                {status}")
        except Exception:
            pass

    def _ensure_baseline(self) -> None:
        """Auto-establish a baseline from live telemetry when none exists, so
        Airen computes its own reference — no manual extract/S3 upload needed.
        Best-effort; skipped in mock mode (no real feed) and never breaks a cycle.
        """
        if is_mock_mode():
            return
        try:
            from airen.onboarding.calibrate import ensure_baseline

            status = ensure_baseline(self.config) if self.config is not None else None
            if status:
                print(f"  📊 BASELINE                {status}")
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────
    #  State helpers
    # ─────────────────────────────────────────────────────────────────
    def _transition(
        self,
        new_state: OrchestratorState,
        message: str,
        agent: str | None = None,
        duration_ms: int | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.state = new_state
        evt = IncidentEvent(
            timestamp=_now(),
            state=new_state,
            agent=agent,
            message=message,
            duration_ms=duration_ms,
            metadata=metadata or {},
        )
        self.run.events.append(evt)
        # Pretty terminal log
        sigil = _STATE_SIGIL.get(new_state, "•")
        tail = f"  ({duration_ms} ms)" if duration_ms is not None else ""
        print(f"  {sigil} {new_state.value:<22} {message}{tail}")
        # Incremental save — the SSE consumer in the web UI watches this file
        # and streams new events to /live in real time.
        self._save_run_to_disk()

    def _save_run_to_disk(self) -> None:
        """Persist current IncidentRun JSON. Called after every state transition.

        IMPORTANT: pulls the `approval` field from disk first if our in-memory
        copy doesn't have one. This is the cross-process handshake — the
        /slack/actions handler (FastAPI process) writes approval to disk; we
        (orchestrator process) must not clobber it on the next heartbeat write.
        """
        try:
            self.run.final_state = self.state  # keep in sync mid-flight
            _LOGS_DIR.mkdir(parents=True, exist_ok=True)
            path = _LOGS_DIR / f"{self.run.run_id}.json"

            # Pull disk-side approval into memory so we don't overwrite it.
            if self.run.approval is None and path.exists():
                try:
                    disk_data = json.loads(path.read_text())
                    disk_approval = disk_data.get("approval")
                    if disk_approval:
                        self.run.approval = disk_approval
                except Exception:
                    pass

            path.write_text(json.dumps(self.run.model_dump(mode="json"), indent=2))
        except Exception:
            # Don't let disk hiccups break the agent flow
            pass

    async def _cooldown(self, label: str) -> None:
        if self.cooldown_sec <= 0:
            return
        print(f"\n⏳ Cooldown {self.cooldown_sec}s ({label})…")
        await asyncio.sleep(self.cooldown_sec)

    # ─────────────────────────────────────────────────────────────────
    #  Steps
    # ─────────────────────────────────────────────────────────────────
    async def _monitor_step(self) -> None:
        self._transition(
            OrchestratorState.MONITORING,
            f"Checking '{self.project_name}' health (lookback {self.lookback_min} min)…",
            agent="sentinel",
        )
        t0 = time.perf_counter()
        if is_mock_mode():
            verdict = mock_sentinel_verdict(project_name=self.project_name)
        else:
            sentinel_msg = (
                f"Check the health of Phoenix project '{self.project_name}' over the last "
                f"{self.lookback_min} minutes. Return the SentinelVerdict."
            )
            raw = await _run_agent_with_retry(sentinel_agent, sentinel_msg)
            verdict = SentinelVerdict.model_validate_json(extract_json(raw))
        dt_ms = int((time.perf_counter() - t0) * 1000)
        self.run.sentinel_verdict = verdict

        if verdict.status == HealthStatus.HEALTHY:
            self._transition(
                OrchestratorState.HEALTHY_NO_ACTION,
                f"Sentinel reports HEALTHY (severity {verdict.severity_score:.2f}). {verdict.summary}",
                agent="sentinel",
                duration_ms=dt_ms,
            )
        else:
            top = verdict.anomalies[0] if verdict.anomalies else None
            summary = (
                f"Sentinel: {verdict.status.value} (severity {verdict.severity_score:.2f}). "
                f"Worst: {top.metric}@{top.segment} ratio {top.ratio:.2f}x"
                if top
                else f"Sentinel: {verdict.status.value} (severity {verdict.severity_score:.2f})"
            )
            self._transition(
                OrchestratorState.ANOMALY_DETECTED,
                summary,
                agent="sentinel",
                duration_ms=dt_ms,
                metadata={"severity": verdict.severity_score, "n_anomalies": len(verdict.anomalies)},
            )

    async def _investigate_step(self) -> None:
        verdict = self.run.sentinel_verdict
        if verdict is None:
            raise RuntimeError("Investigator step called but no SentinelVerdict in run state")

        self._transition(
            OrchestratorState.INVESTIGATING,
            f"Investigating against repo {self.repo}…",
            agent="investigator",
        )

        # Cross-run learning: recall this anomaly class's confirmed precedents and
        # seed the Investigator with them (self-improvement across incidents).
        exemplars = ""
        try:
            from airen.evals import learning

            similar = learning.recall_similar(verdict)
            exemplars = learning.exemplars_block(similar)
            if similar:
                self._transition(
                    OrchestratorState.INVESTIGATING,
                    f"Recalled {len(similar)} prior CONFIRMED incident(s) of this class as precedent.",
                    agent="investigator",
                )
        except Exception:
            pass

        t0 = time.perf_counter()
        inv, eval_result, reflections = await self._investigate_with_reflection(verdict, exemplars)
        dt_ms = int((time.perf_counter() - t0) * 1000)
        self.run.investigator_verdict = inv
        self.run.eval_reflections = reflections
        self.run.investigator_eval = eval_result.to_dict() if eval_result else None

        top_commit = inv.code_evidence[0] if inv.code_evidence else None
        commit_str = (
            f"commit {top_commit.commit_sha[:8]} by {top_commit.commit_author or '?'}"
            if top_commit
            else "no code evidence"
        )
        self._transition(
            OrchestratorState.ANOMALY_DETECTED,  # stay in anomaly state, but record progress
            f"Investigator: hypothesis confidence {inv.confidence:.2f} — {commit_str}",
            agent="investigator",
            duration_ms=dt_ms,
            metadata={"confidence": inv.confidence, "n_evidence": len(inv.code_evidence)},
        )
        # Surface the self-eval verdict as its own event (visible in the UI + logs).
        if eval_result is not None:
            self._transition(
                OrchestratorState.ANOMALY_DETECTED,
                f"Self-eval: RCA scored {eval_result.overall:.2f} "
                f"({'PASS' if eval_result.passed else 'FAIL'}) "
                f"after {reflections} reflection(s) — {eval_result.critique()[:120]}",
                agent="evaluator",
                metadata={"eval": eval_result.to_dict(), "reflections": reflections},
            )

    async def _investigate_with_reflection(self, verdict, exemplars: str):
        """Run the Investigator, grade the verdict with Airen's own evals, and — if
        it scores below the bar — feed it its OWN critique and re-investigate
        (bounded). Returns (best_verdict, eval_result, n_reflections). Each attempt
        is traced + graded so the score lift is visible in Phoenix.

        In mock mode there's no LLM judge or reflection; the code evaluators still
        grade the canned verdict so the eval surface is exercised offline."""
        from airen.evals import learning
        from airen.evals.rca_eval import evaluate_investigation, log_to_phoenix

        base_msg = (
            f"Investigate the following SentinelVerdict against GitHub repo `{self.repo}`. "
            f"The Phoenix project is `{self.project_name}`. Find the root cause and return "
            f"an InvestigatorVerdict.\n\nSentinelVerdict JSON:\n"
            f"{verdict.model_dump_json(indent=2)}"
        )
        max_ref = 0 if is_mock_mode() else learning.max_reflections()
        feedback = ""
        best_inv = None
        best_eval = None
        reflections = 0

        for attempt in range(max_ref + 1):
            if is_mock_mode():
                inv = mock_investigator_verdict(sentinel=verdict)
                span_id = None
            else:
                msg = base_msg + exemplars + feedback + self._context_block()
                raw, span_id = await self._run_investigator_traced(msg)
                inv = InvestigatorVerdict.model_validate_json(extract_json(raw))

            result = evaluate_investigation(verdict, inv, use_llm=not is_mock_mode())
            try:
                log_to_phoenix(result, span_id=span_id, project_name=self.project_name)
            except Exception:
                pass

            if best_eval is None or result.overall > best_eval.overall:
                best_inv, best_eval = inv, result

            if not learning.should_reflect(result) or attempt >= max_ref:
                break
            feedback = learning.reflection_feedback(result)
            reflections += 1
            self._transition(
                OrchestratorState.INVESTIGATING,
                f"RCA scored {result.overall:.2f} (< {result.pass_threshold:.2f}) — "
                f"reflecting on its own critique and re-investigating (#{reflections})…",
                agent="evaluator",
            )
            await self._cooldown("before reflection retry")

        return best_inv, best_eval, reflections

    async def _run_investigator_traced(self, msg: str) -> tuple[str, str]:
        """Run the Investigator inside an explicit span so we have a stable span_id
        to attach eval annotations to. Returns (raw_output, span_id_hex)."""
        with _tracer.start_as_current_span("airen.investigator") as span:
            span_id = format(span.get_span_context().span_id, "016x")
            raw = await _run_agent_with_retry(investigator_agent, msg)
            return raw, span_id

    def _context_block(self) -> str:
        """Operator-provided service context (from service.context in the yaml),
        appended to agent prompts so the LLM has out-of-band knowledge the repo
        doesn't contain (upstream deps, known issues, retrain/approval policy)."""
        ctx = self.config.service.context if self.config else None
        if not ctx:
            return ""
        return (
            "\n\n--- OPERATOR CONTEXT (out-of-band knowledge about this service; "
            "weigh it in your reasoning) ---\n" + ctx.strip()
        )

    async def _rca_step(self) -> None:
        verdict = self.run.sentinel_verdict
        inv = self.run.investigator_verdict
        if verdict is None or inv is None:
            raise RuntimeError("RCA step called but missing upstream verdicts")

        self._transition(
            OrchestratorState.RCA_DRAFTING,
            "Drafting incident report for humans…",
            agent="rca_writer",
        )
        t0 = time.perf_counter()
        if is_mock_mode():
            report = mock_incident_report(sentinel=verdict, investigator=inv)
        else:
            band = _confidence_band(inv.confidence)
            msg = (
                f"Draft an IncidentReport from the SentinelVerdict and InvestigatorVerdict below.\n\n"
                f"confidence_band: {band}\n"
                f"report_date: {_now()}\n"
                f"service_name: {self.project_name}\n\n"
                f"SentinelVerdict JSON:\n{verdict.model_dump_json(indent=2)}\n\n"
                f"InvestigatorVerdict JSON:\n{inv.model_dump_json(indent=2)}"
                f"{self._context_block()}"
            )
            raw = await _run_agent_with_retry(rca_writer_agent, msg)
            report = IncidentReport.model_validate_json(extract_json(raw))
        dt_ms = int((time.perf_counter() - t0) * 1000)
        self.run.incident_report = report
        self._transition(
            OrchestratorState.RCA_DRAFTING,
            f"Report drafted: '{report.title[:80]}…' ({report.severity.value})",
            agent="rca_writer",
            duration_ms=dt_ms,
        )

    async def _remediation_plan_step(self) -> None:
        sentinel = self.run.sentinel_verdict
        inv = self.run.investigator_verdict
        if sentinel is None or inv is None:
            return  # nothing to remediate against

        self._transition(
            OrchestratorState.AWAITING_APPROVAL,
            "Drafting remediation plan…",
            agent="remediation",
        )
        t0 = time.perf_counter()
        plan = await self._remediator.plan(
            sentinel=sentinel,
            investigator=inv,
            incident_report=self.run.incident_report,
            incident_id=self.run.run_id,
        )
        dt_ms = int((time.perf_counter() - t0) * 1000)
        self.run.remediation_plan = plan

        if plan.action_type == RemediationActionType.NO_ACTION:
            msg = "No remediation proposed — confidence below threshold or symptom is infra-side."
        else:
            sha8 = (plan.target_commit_sha or "")[:8]
            msg = (
                f"Plan: {plan.action_type.value} commit {sha8} "
                f"(confidence {plan.confidence:.2f}). "
                f"Branch: {plan.proposed_branch_name}"
            )
        self._transition(
            OrchestratorState.AWAITING_APPROVAL,
            msg,
            agent="remediation",
            duration_ms=dt_ms,
            metadata={
                "action_type": plan.action_type.value,
                "confidence": plan.confidence,
            },
        )

    @staticmethod
    def _is_code_fix(plan) -> bool:
        """Only these action types produce a branch + PR Airen can merge."""
        return plan is not None and plan.action_type in (
            RemediationActionType.REVERT_COMMIT,
            RemediationActionType.HOTFIX_PR,
        )

    async def _remediation_prepare_step(self) -> bool:
        """Build the branch, apply the fix, push, and open a READY (draft) PR —
        BEFORE asking for approval, so the Slack message links a ready PR.
        Returns True iff a PR is ready. Approval later gates the MERGE."""
        plan = self.run.remediation_plan
        if plan is None or not self._is_code_fix(plan) or not self.repo:
            return False

        self._transition(
            OrchestratorState.REMEDIATION_PREP,
            f"Preparing fix ({plan.action_type.value}) — branch + PR…",
            agent="remediation",
        )
        t0 = time.perf_counter()
        result = await asyncio.to_thread(
            self._remediator.execute, plan, repo_full_name=self.repo, force=True,
        )
        dt_ms = int((time.perf_counter() - t0) * 1000)
        self.run.remediation_result = result
        if result.executed:
            self._transition(
                OrchestratorState.REMEDIATION_PREP,
                f"PR ready for review: {result.pr_url} (#{result.pr_number})",
                agent="remediation",
                duration_ms=dt_ms,
            )
            await self._jira_on_remediation_executed()
            return True
        self._transition(
            OrchestratorState.REMEDIATION_PREP,
            f"Could not prepare a PR: {result.error}",
            agent="remediation",
            duration_ms=dt_ms,
        )
        return False

    async def _merge_step(self) -> bool:
        """Merge the approved PR. Gated upstream by --execute + approval +
        remediation.auto_merge. Returns True iff merged."""
        result = self.run.remediation_result
        if result is None or not result.pr_number:
            return False
        self._transition(OrchestratorState.MERGING, f"Merging PR #{result.pr_number}…", agent="remediation")
        method = self.config.remediation.merge_method if self.config else "squash"
        from airen.adapters import github

        res = await asyncio.to_thread(github.merge_pull_request, self.repo, result.pr_number, method)
        if res.get("merged"):
            result.merged = True
            result.merge_sha = res.get("sha")
            result.merged_at = _now()
            self._transition(
                OrchestratorState.MERGING,
                f"Merged PR #{result.pr_number} ({(res.get('sha') or '')[:8]}) — CI/CD will deploy.",
                agent="remediation",
            )
            return True
        self._transition(
            OrchestratorState.MERGING,
            f"Merge failed: {res.get('error', 'unknown')}",
            agent="remediation",
        )
        return False

    async def _deploy_wait_step(self) -> None:
        grace = self.config.remediation.deploy_grace_minutes if self.config else 5
        self._transition(
            OrchestratorState.DEPLOYING,
            f"Merged — waiting ~{grace} min for CI/CD to deploy before validating…",
            agent="orchestrator",
        )
        self._thread_reply(f"🔀 Merged. Waiting ~{grace} min for CI/CD to ship, then I'll watch the live feed.")
        if not is_mock_mode():
            await asyncio.sleep(grace * 60)

    async def _validation_loop(self) -> str:
        """Poll the live feed (via the prediction-source dispatcher) until the
        flagged metric recovers for `recovery_window_polls` in a row, or we hit
        `validation_max_attempts`. Returns 'PASS' or 'FAIL'."""
        sentinel = self.run.sentinel_verdict
        rem = self.config.remediation if self.config else None
        max_attempts = rem.validation_max_attempts if rem else 10
        need = rem.recovery_window_polls if rem else 2
        interval = rem.validation_poll_interval_sec if rem else 120

        self._transition(OrchestratorState.VALIDATING, "Polling the live feed to confirm the fix…", agent="validator")
        consecutive = 0
        for i in range(max_attempts):
            phase1 = await self._validator.validate_phase_1(
                sentinel=sentinel, project_name=self.project_name, lookback_minutes=10,
            )
            self.run.validator_phase1 = phase1
            status = phase1.status.value
            self._transition(
                OrchestratorState.VALIDATING,
                f"Validation poll {i + 1}/{max_attempts}: {status} — {phase1.summary[:90]}",
                agent="validator",
                metadata={"status": status},
            )
            if status == "PASS":
                consecutive += 1
                if consecutive >= need:
                    return "PASS"
            else:
                consecutive = 0
            if not is_mock_mode() and i < max_attempts - 1:
                await asyncio.sleep(interval)
        return "FAIL"

    def _thread_reply(self, text: str) -> None:
        """Post a reply in the incident's Slack thread (continuity)."""
        if not self.post_to_slack or not self.run.slack_message_ts:
            return
        try:
            from airen.adapters.slack import post_text

            post_text(self.run.slack_channel or self.alert_channel, text, thread_ts=self.run.slack_message_ts)
        except Exception:
            pass

    def _await_human(self, reason: str) -> None:
        """Terminal-ish state: Airen stops auto-acting and waits for a human."""
        self._thread_reply(f"⏸ {reason}")
        self._transition(OrchestratorState.AWAITING_HUMAN, reason, agent="orchestrator")

    async def _remediation_cycle(self) -> None:
        """investigate → RCA → prepare PR → notify → approve → merge → deploy →
        validate; on FAIL loop back to investigate (bounded). Sets the terminal
        state itself (RESOLVED or AWAITING_HUMAN)."""
        rem = self.config.remediation if self.config else None
        max_reinv = rem.max_reinvestigate_attempts if rem else 0
        auto_merge = bool(rem and rem.auto_merge)
        first = True

        for attempt in range(max_reinv + 1):
            await self._investigate_step()
            await self._cooldown("after Investigator")
            await self._rca_step()
            await self._remediation_plan_step()
            plan = self.run.remediation_plan

            if first:
                await self._jira_ticket_step()

            # Build the PR up front (ready) — only for executable code fixes when --execute.
            prepared = False
            if self.execute_remediation and self._is_code_fix(plan):
                prepared = await self._remediation_prepare_step()

            if first:
                await self._notify_step()
            else:
                hypo = (self.run.investigator_verdict.root_cause_hypothesis
                        if self.run.investigator_verdict else "")
                self._thread_reply(f"♻️ Re-investigating (attempt {attempt + 1}): new hypothesis — {hypo[:200]}")
            first = False

            # ── No mergeable PR: recommend or fall back to single validation ──
            if not prepared:
                if (self.execute_remediation and plan is not None
                        and plan.action_type != RemediationActionType.NO_ACTION
                        and not self._is_code_fix(plan)):
                    # e.g. RETRAIN_MODEL / ROLLBACK_DEPLOY — not auto-executable yet
                    self._await_human(
                        f"Recommended action: {plan.action_type.value}. Not auto-executable — "
                        f"human needed. See the RCA recommendation."
                    )
                    return
                # Not executing (plan-only / mock / demo): single re-check, then hand off.
                await self._validate_step()
                await self._jira_on_resolved()
                self._transition(OrchestratorState.RESOLVED, "Incident reported. Human is in the loop.")
                return

            # ── Ready PR exists → gate the MERGE on Slack approval ──
            approved = await self._await_human_approval()
            if not approved:
                self._await_human("Remediation rejected — PR left open for manual review.")
                return

            if not auto_merge:
                url = self.run.remediation_result.pr_url if self.run.remediation_result else "(PR)"
                self._await_human(f"PR ready: {url}. auto_merge is off — merge it manually; CI/CD will deploy.")
                return

            if not await self._merge_step():
                self._await_human("Merge failed (conflicts/checks) — needs manual resolution.")
                return

            await self._deploy_wait_step()
            verdict = await self._validation_loop()
            if verdict == "PASS":
                await self._jira_on_resolved()
                self._thread_reply("✅ Fix confirmed — the flagged metric recovered after deploy. Closing the incident.")
                self._transition(OrchestratorState.RESOLVED, "Merged, deployed, and validated. Incident resolved.")
                return

            # FAIL → loop back to investigate if budget remains, else hand off.
            if attempt < max_reinv:
                self._thread_reply("⚠️ The fix did NOT restore health. Re-investigating for another cause…")
                await self._cooldown("before re-investigation")
                continue
            self._await_human("Fix didn't restore health and re-investigation budget is exhausted. Human intervention needed.")
            return

    async def _validate_step(self) -> None:
        sentinel = self.run.sentinel_verdict
        if sentinel is None or not sentinel.anomalies:
            return  # nothing to validate against

        self._transition(
            OrchestratorState.VALIDATING,
            "Phase 1 — re-checking Phoenix to confirm remediation took effect…",
            agent="validator",
        )
        t0 = time.perf_counter()
        # Short lookback so we look at POST-incident traffic, not the original anomaly
        phase1 = await self._validator.validate_phase_1(
            sentinel=sentinel,
            project_name=self.project_name,
            lookback_minutes=10,
        )
        dt_ms = int((time.perf_counter() - t0) * 1000)
        self.run.validator_phase1 = phase1

        emoji = _VALIDATOR_EMOJI.get(phase1.status, "?")
        self._transition(
            OrchestratorState.VALIDATING,
            f"Phase 1 verdict: {emoji} {phase1.status.value} — {phase1.summary}",
            agent="validator",
            duration_ms=dt_ms,
            metadata={"status": phase1.status.value},
        )
        await self._jira_on_validation(1, phase1)

        # Phase 2 stub — documents the FK-VM contract
        phase2 = self._validator.validate_phase_2_stub(sentinel)
        self.run.validator_phase2 = phase2
        self._transition(
            OrchestratorState.VALIDATING,
            f"Phase 2 verdict: ⏸ {phase2.status.value} — {phase2.summary[:80]}…",
            agent="validator",
        )
        await self._jira_on_validation(2, phase2)

    async def _jira_ticket_step(self) -> None:
        """Create a Jira ticket for the incident (if config enables it)."""
        cfg = self.config
        if cfg is None or not cfg.jira or not cfg.jira.enable or not cfg.jira.project_key:
            return  # Jira disabled for this service
        if self.run.incident_report is None:
            return

        from airen.adapters.jira import get_jira_adapter

        self._transition(
            OrchestratorState.AWAITING_APPROVAL,
            f"Creating Jira ticket in project {cfg.jira.project_key}…",
            agent="jira",
        )
        t0 = time.perf_counter()
        adapter = get_jira_adapter(
            base_url=self.config.jira.base_url if self.config else None,
            user_email=self.config.jira.user_email if self.config else None,
        )
        report = self.run.incident_report

        # Plain-text description so we don't have to deal with Atlassian Document Format
        description_lines: list[str] = [
            f"*Airen Incident Run:* {self.run.run_id}",
            f"*Service:* {self.project_name}",
            f"*Severity:* {report.severity.value}",
            "",
            "*TL;DR*",
            report.tldr,
            "",
            "*What happened*",
            report.what_happened,
            "",
            "*Root cause*",
            report.root_cause,
            "",
        ]
        if report.evidence_bullets:
            description_lines.append("*Evidence*")
            description_lines.extend(f"- {b}" for b in report.evidence_bullets)
            description_lines.append("")
        description_lines.append("*Recommended fix*")
        description_lines.append(report.recommended_fix)
        if report.open_questions:
            description_lines.append("")
            description_lines.append("*Open questions*")
            description_lines.extend(f"- {q}" for q in report.open_questions)
        description_lines.append("")
        description_lines.append(
            f"---\n_Auto-created by Airen — Autonomous ML Reliability Engineer._"
        )
        description = "\n".join(description_lines)

        result = await asyncio.to_thread(
            adapter.create_issue,
            project_key=cfg.jira.project_key,
            summary=report.title[:255],
            description=description,
            issue_type=cfg.jira.issue_type,
            labels=["airen", "ml-reliability", f"severity-{report.severity.value.lower()}"],
        )
        dt_ms = int((time.perf_counter() - t0) * 1000)

        if "error" in result:
            self._transition(
                OrchestratorState.AWAITING_APPROVAL,
                f"Jira ticket creation failed: {result.get('error')}",
                agent="jira",
                duration_ms=dt_ms,
            )
            return

        self.run.jira_issue_key = result.get("key")
        self.run.jira_issue_url = result.get("url")
        self._transition(
            OrchestratorState.AWAITING_APPROVAL,
            f"Jira ticket created: {result.get('key')} → {result.get('url')}",
            agent="jira",
            duration_ms=dt_ms,
            metadata={"issue_key": result.get("key")},
        )

    async def _notify_step(self) -> None:
        if not self.post_to_slack or not os.environ.get("SLACK_BOT_TOKEN", "").strip():
            self._transition(
                OrchestratorState.NOTIFYING,
                "Slack disabled (no SLACK_BOT_TOKEN, or post_to_slack=False)",
                agent=None,
            )
            return

        self._transition(OrchestratorState.NOTIFYING, "Posting incident to Slack…", agent="slack")
        if self.run.incident_report is None:
            raise RuntimeError("Notify step called but no incident_report in run state")
        t0 = time.perf_counter()
        from airen.adapters.slack import post_incident_report

        # Per-service channel override if airen.yaml sets one; else env default.
        result = post_incident_report(
            self.run.incident_report,
            incident_id=self.run.run_id,
            channel=self.alert_channel,
            jira_url=self.run.jira_issue_url,
            jira_key=self.run.jira_issue_key,
        )
        dt_ms = int((time.perf_counter() - t0) * 1000)
        if result.get("ok"):
            self.run.slack_permalink = result.get("permalink")
            self.run.slack_message_ts = result.get("ts")
            self.run.slack_channel = result.get("channel")
            self._transition(
                OrchestratorState.NOTIFYING,
                f"Posted: {self.run.slack_permalink}",
                agent="slack",
                duration_ms=dt_ms,
            )
            # Mirror to Jira: comment + transition "alert sent → In Progress"
            await self._jira_on_alert_sent(self.run.slack_permalink)
        else:
            self._transition(
                OrchestratorState.NOTIFYING,
                f"Slack post failed: {result.get('error')}",
                agent="slack",
                duration_ms=dt_ms,
            )

    # ─────────────────────────────────────────────────────────────────
    #  Jira lifecycle helpers — no-op when no ticket exists or Jira disabled
    # ─────────────────────────────────────────────────────────────────
    def _jira_enabled(self) -> bool:
        cfg = self.config
        return bool(
            cfg
            and cfg.jira
            and cfg.jira.enable
            and self.run.jira_issue_key
        )

    async def _jira_comment(self, body: str) -> None:
        """Add a comment to the run's Jira ticket. Silent no-op if disabled/no ticket."""
        if not self._jira_enabled():
            return
        from airen.adapters.jira import get_jira_adapter

        adapter = get_jira_adapter(
            base_url=self.config.jira.base_url if self.config else None,
            user_email=self.config.jira.user_email if self.config else None,
        )
        result = await asyncio.to_thread(
            adapter.add_comment, self.run.jira_issue_key, body
        )
        if "error" in result:
            # Don't fail the cycle on Jira hiccups — log and move on
            self._transition(
                self.state,
                f"Jira comment skipped: {result.get('error')}",
                agent="jira",
            )

    async def _jira_transition(self, transition_name: str | None) -> None:
        """Run a transition on the run's Jira ticket. None or no ticket = no-op."""
        if not transition_name or not self._jira_enabled():
            return
        from airen.adapters.jira import get_jira_adapter

        adapter = get_jira_adapter(
            base_url=self.config.jira.base_url if self.config else None,
            user_email=self.config.jira.user_email if self.config else None,
        )
        result = await asyncio.to_thread(
            adapter.transition_issue, self.run.jira_issue_key, transition_name
        )
        if "error" in result:
            # Common case: transition name doesn't exist in this project's workflow.
            # Surface the available ones so the user can fix airen.yaml.
            avail = result.get("available", [])
            self._transition(
                self.state,
                f"Jira transition {transition_name!r} skipped: {result.get('error')}"
                + (f" — available: {avail}" if avail else ""),
                agent="jira",
            )
        else:
            self._transition(
                self.state,
                f"Jira {self.run.jira_issue_key} → {transition_name}",
                agent="jira",
            )

    async def _jira_on_alert_sent(self, slack_permalink: str | None) -> None:
        """After Slack alert posts: comment with permalink + move to In Progress."""
        body_lines = [
            "*Airen update — alert dispatched*",
            "",
            f"Slack: {slack_permalink}" if slack_permalink else "Slack alert posted.",
            f"Run: `{self.run.run_id}`",
        ]
        await self._jira_comment("\n".join(body_lines))
        cfg = self.config
        if cfg and cfg.jira:
            await self._jira_transition(cfg.jira.transition_on_alert)

    async def _jira_on_remediation_executed(self) -> None:
        result = self.run.remediation_result
        if result is None:
            return
        if result.executed and result.pr_url:
            body = (
                f"*Airen update — remediation PR opened*\n\n"
                f"Branch: `{result.branch_created}`\n"
                f"PR: {result.pr_url}\n"
                f"Confidence: {result.plan.confidence:.2f}"
            )
        else:
            body = (
                f"*Airen update — remediation NOT executed*\n\n"
                f"Reason: {result.error or 'unknown'}"
            )
        await self._jira_comment(body)

    async def _jira_on_validation(self, phase: int, verdict) -> None:
        """Mirror Validator Phase 1/2 verdict into the Jira ticket."""
        body = (
            f"*Airen update — Validator Phase {phase}: {verdict.status.value}*\n\n"
            f"{verdict.summary}\n\n"
            f"Spans observed: {verdict.n_spans_observed}  ·  "
            f"Lookback: {verdict.lookback_minutes} min"
        )
        await self._jira_comment(body)

    async def _await_human_approval(
        self,
        timeout_sec: int = 300,
        poll_interval_sec: float = 3.0,
    ) -> bool:
        """Block until the human clicks Approve or Reject on the Slack alert.

        Re-reads the IncidentRun JSON each poll because /slack/actions writes
        the approval there from the FastAPI process — orchestrator and web
        server are separate processes that only share this file.

        Returns True iff approval was granted within timeout. False on REJECT
        or timeout, in which case the remediation step is skipped.

        Skipped (returns True immediately) when no Slack post happened — that
        keeps offline/mock/--no-slack runs behaving as before.
        """
        if not self.run.slack_message_ts or not self.post_to_slack:
            # No way for a human to approve via Slack; preserve old behavior
            return True

        self._transition(
            OrchestratorState.AWAITING_APPROVAL,
            f"Waiting for human Approve/Reject in Slack ({timeout_sec}s timeout)…",
            agent="orchestrator",
        )

        deadline = asyncio.get_event_loop().time() + timeout_sec
        last_logged = 0
        while asyncio.get_event_loop().time() < deadline:
            decision = self._read_approval_from_disk()
            if decision is not None:
                self.run.approval = decision
                decided = decision.get("decision")
                who = decision.get("by") or "human"
                if decided == "APPROVED":
                    self._transition(
                        OrchestratorState.AWAITING_APPROVAL,
                        f"Approved by {who} — proceeding with remediation",
                        agent="orchestrator",
                        metadata={"approval": decision},
                    )
                    return True
                self._transition(
                    OrchestratorState.AWAITING_APPROVAL,
                    f"Rejected by {who} — skipping remediation, ticket stays open",
                    agent="orchestrator",
                    metadata={"approval": decision},
                )
                return False

            # Heartbeat every ~60s so the SSE stream + UI don't look frozen
            elapsed = int(timeout_sec - (deadline - asyncio.get_event_loop().time()))
            if elapsed - last_logged >= 60:
                last_logged = elapsed
                self._transition(
                    OrchestratorState.AWAITING_APPROVAL,
                    f"Still waiting for approval… ({elapsed}s elapsed of {timeout_sec}s)",
                    agent="orchestrator",
                )
            await asyncio.sleep(poll_interval_sec)

        self._transition(
            OrchestratorState.AWAITING_APPROVAL,
            f"Approval timed out after {timeout_sec}s — skipping remediation",
            agent="orchestrator",
        )
        return False

    def _read_approval_from_disk(self) -> dict | None:
        """Re-read just the `approval` field from the run's JSON on disk.

        The /slack/actions handler writes it from a different process; we can't
        rely on in-memory state. Returns None if not yet decided or on error.
        """
        try:
            path = _LOGS_DIR / f"{self.run.run_id}.json"
            if not path.exists():
                return None
            data = json.loads(path.read_text())
            return data.get("approval")
        except Exception:
            return None

    async def _jira_on_resolved(self) -> None:
        """End-of-cycle: pick the right closing transition based on validation outcome."""
        cfg = self.config
        if not cfg or not cfg.jira:
            return
        phase1 = self.run.validator_phase1
        # PASS → close. Anything else (FAIL / INCONCLUSIVE / NO_DATA / no validation
        # at all because the action was NO_ACTION) → leave open for the human.
        from airen.schemas import ValidatorStatus

        if phase1 and phase1.status == ValidatorStatus.PASS:
            await self._jira_transition(cfg.jira.transition_on_pass)
        else:
            await self._jira_transition(cfg.jira.transition_on_fail)


# ─────────────────────────────────────────────────────────────────
#  helpers — agent invocation with retry-with-backoff
# ─────────────────────────────────────────────────────────────────
async def _run_agent_once(agent, user_msg: str) -> str:
    session_id = secrets.token_hex(8)
    runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
    await runner.session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id
    )
    final_text = ""
    event_count = 0
    saw_function_call = False
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=user_msg)]),
    ):
        event_count += 1
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    final_text += part.text
                if getattr(part, "function_call", None):
                    saw_function_call = True
    if not final_text.strip():
        raise RuntimeError(
            f"Agent {agent.name!r} returned no text output after {event_count} events "
            f"(function_calls_seen={saw_function_call})."
        )
    return final_text


async def _run_agent_with_retry(agent, user_msg: str, max_retries: int = 3) -> str:
    """Same retry-with-backoff as run_pipeline.py — handles Gemini 429/503."""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await _run_agent_once(agent, user_msg)
        except Exception as e:
            last_exc = e
            msg = str(e)
            is_429 = "RESOURCE_EXHAUSTED" in msg or "429" in msg
            is_503 = "UNAVAILABLE" in msg or "503" in msg
            if not (is_429 or is_503) or attempt == max_retries:
                raise
            hint = re.search(r"retry in (\d+(?:\.\d+)?)s", msg) or re.search(
                r"retryDelay['\":\s]+(\d+)s", msg
            )
            delay = float(hint.group(1)) + 2 if hint else 5 * (2**attempt)
            print(f"    ⏳ transient LLM error — backing off {delay:.0f}s (attempt {attempt + 1}/{max_retries})")
            await asyncio.sleep(delay)
    raise last_exc if last_exc else RuntimeError("retry loop exited")


# ─────────────────────────────────────────────────────────────────
#  Pretty-print sigils for the state-machine log
# ─────────────────────────────────────────────────────────────────
_VALIDATOR_EMOJI = {
    ValidatorStatus.PASS: "🟢",
    ValidatorStatus.FAIL: "🔴",
    ValidatorStatus.INCONCLUSIVE: "🟡",
    ValidatorStatus.DEFERRED: "⏸",
    ValidatorStatus.NO_DATA: "⚪️",
}

_STATE_SIGIL = {
    OrchestratorState.IDLE: "·",
    OrchestratorState.MONITORING: "🛡 ",
    OrchestratorState.HEALTHY_NO_ACTION: "🟢",
    OrchestratorState.ANOMALY_DETECTED: "⚠️ ",
    OrchestratorState.INVESTIGATING: "🔎",
    OrchestratorState.RCA_DRAFTING: "📝",
    OrchestratorState.NOTIFYING: "📣",
    OrchestratorState.AWAITING_APPROVAL: "⏸",
    OrchestratorState.REMEDIATING: "🔧",
    OrchestratorState.VALIDATING: "✅",
    OrchestratorState.RESOLVED: "✓ ",
    OrchestratorState.FAILED: "✗ ",
}
