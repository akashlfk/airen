"""Remediation agent — generates the fix plan and (gated) executes it.

Design (see [[agent-architecture-pattern]]):
  Deterministic Python decides:
    - action_type (REVERT / HOTFIX / NO_ACTION)
    - target commit SHA (from InvestigatorVerdict.code_evidence)
    - branch naming
    - safety guards (confidence threshold, allowlist of repos)
  LLM writes:
    - PR title (concise, action-oriented)
    - PR body (markdown narrative tying the incident to the fix)

By default, `plan(...)` is the ONLY public method. It returns a RemediationPlan
with zero side effects — safe to display in the UI and Slack. To actually
open the PR you must explicitly call `execute(plan)` with the right flags.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any

from google.adk.agents import Agent
from google.adk.tools import FunctionTool
from pydantic import BaseModel

from airen.adapters import github as gh
from airen.config import AirenServiceConfig
from airen.llm_factory import extract_json, get_model_for, schema_appendix, supports_strict_output_schema
from airen.schemas import (
    HealthStatus,
    IncidentReport,
    InvestigatorVerdict,
    RemediationActionType,
    RemediationPlan,
    RemediationResult,
    SentinelVerdict,
)

# Confidence floors for each action type — set conservatively so we don't
# auto-execute on weak evidence.
MIN_CONFIDENCE_FOR_REVERT = 0.7
MIN_CONFIDENCE_FOR_HOTFIX = 0.85


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ───────────────────────────────────────────────────────────────────────
#  Pydantic helper schema for the LLM narrative step
# ───────────────────────────────────────────────────────────────────────
class _PRNarrative(BaseModel):
    """What the LLM produces for the human-facing PR copy."""

    pr_title: str
    pr_body: str
    rationale: str


_PR_WRITER_INSTRUCTION = """
You are the PR drafter inside Airen's Remediation step.

INPUT (via the user message):
  - the SentinelVerdict (symptom)
  - the InvestigatorVerdict (root cause hypothesis with commit / PR / file)
  - the proposed action_type (REVERT_COMMIT | HOTFIX_PR | NO_ACTION)
  - the incident_id and run_id

OUTPUT (strict JSON only — no prose, no preamble):
{
  "pr_title": "<= 80 chars; action-oriented, e.g. 'Revert PR #741: api_fetch_limit=73 truncating ping history'",
  "pr_body":  "Markdown. 5-12 lines. Sections: Why, What this changes, Risk, Linked incident.",
  "rationale": "1-2 sentences: why this action vs alternatives. Plain English."
}

Writing rules:
  - NEVER invent commit hashes, PR numbers, or filenames. Use only what's in the verdicts.
  - Hedge proportionally: if Investigator confidence is below 0.85, the PR body should
    say "I believe / suspect" and ask the reviewer to verify before merge.
  - Reference the incident_id in the PR body so the audit trail is two-way clickable.
  - DO NOT include emoji or status badges — keep it engineer-readable.
""".strip()


def _build_pr_writer_agent() -> Agent:
    """Build the PR-writer ADK agent. Same conditional-schema pattern as RCA Writer."""
    use_schema = supports_strict_output_schema()
    instruction = _PR_WRITER_INSTRUCTION if use_schema else _PR_WRITER_INSTRUCTION + schema_appendix(_PRNarrative)
    kwargs: dict[str, Any] = {
        "model": get_model_for("REMEDIATION"),
        "name": "remediation_pr_writer",
        "description": "PR-writer sub-agent — drafts the title/body/rationale for a remediation PR.",
        "instruction": instruction,
    }
    if use_schema:
        kwargs["output_schema"] = _PRNarrative
    return Agent(**kwargs)


# ───────────────────────────────────────────────────────────────────────
#  The Remediator — pure Python orchestrator + thin LLM step
# ───────────────────────────────────────────────────────────────────────
class Remediator:
    """Produces RemediationPlan; executes it only on explicit request."""

    def __init__(
        self,
        config: AirenServiceConfig | None = None,
        allow_execute_envvar: str = "AIREN_ALLOW_REMEDIATION_EXECUTE",
    ) -> None:
        self.config = config
        self.allow_execute_envvar = allow_execute_envvar

    # ─── planning (pure, side-effect-free) ───
    async def plan(
        self,
        sentinel: SentinelVerdict,
        investigator: InvestigatorVerdict,
        incident_report: IncidentReport | None,
        incident_id: str,
    ) -> RemediationPlan:
        action_type, target = self._decide_action(sentinel, investigator)
        if action_type == RemediationActionType.NO_ACTION:
            return RemediationPlan(
                action_type=action_type,
                proposed_branch_name="",
                proposed_pr_title="No remediation proposed",
                proposed_pr_body=(
                    "Airen did not find sufficient code-level evidence to recommend a "
                    "code change. Confidence below threshold or the symptom is "
                    "infrastructure-side (e.g. zero traffic). "
                    f"Incident: {incident_id}."
                ),
                confidence=investigator.confidence,
                rationale=(
                    f"Investigator confidence {investigator.confidence:.2f} is below the "
                    f"{MIN_CONFIDENCE_FOR_REVERT} threshold needed for an auto-proposed "
                    "revert; or the anomaly does not map to a specific commit."
                ),
            )

        branch_name = self._propose_branch_name(target.commit_sha, incident_id)
        narrative = await self._draft_pr_copy(
            sentinel=sentinel,
            investigator=investigator,
            action_type=action_type,
            incident_id=incident_id,
            incident_report=incident_report,
        )

        return RemediationPlan(
            action_type=action_type,
            target_commit_sha=target.commit_sha,
            target_pr_number=target.pr_number,
            target_pr_url=target.pr_url,
            proposed_branch_name=branch_name,
            proposed_pr_title=narrative.pr_title[:80],
            proposed_pr_body=narrative.pr_body,
            confidence=investigator.confidence,
            rationale=narrative.rationale,
        )

    # ─── execution (gated — side effects!) ───
    def execute(
        self,
        plan: RemediationPlan,
        repo_full_name: str,
        force: bool = False,
    ) -> RemediationResult:
        """Open the remediation PR. Multiple guards before any GitHub call."""
        # Guard 1: the env-var allowlist
        if os.environ.get(self.allow_execute_envvar, "").strip().lower() not in {"1", "true", "yes"}:
            if not force:
                return RemediationResult(
                    plan=plan,
                    executed=False,
                    error=(
                        f"Execution blocked by safety guard. Set {self.allow_execute_envvar}=1 "
                        "and pass force=True (or run with --execute) to actually open the PR."
                    ),
                )
        # Guard 2: action types we know how to execute
        if plan.action_type != RemediationActionType.REVERT_COMMIT:
            return RemediationResult(
                plan=plan,
                executed=False,
                error=f"Execution not implemented for action_type={plan.action_type.value}",
            )
        # Guard 3: confidence floor
        if plan.confidence < MIN_CONFIDENCE_FOR_REVERT:
            return RemediationResult(
                plan=plan,
                executed=False,
                error=(
                    f"Confidence {plan.confidence:.2f} below revert threshold "
                    f"{MIN_CONFIDENCE_FOR_REVERT}. Refusing to auto-revert."
                ),
            )

        # Defer the actual GitHub PR creation to a helper. Wrapped in try/except
        # so a partial failure still produces a RemediationResult with an error.
        try:
            pr_url, pr_number, branch = _open_revert_pr(
                repo_full_name=repo_full_name,
                commit_sha=plan.target_commit_sha or "",
                branch_name=plan.proposed_branch_name,
                title=plan.proposed_pr_title,
                body=plan.proposed_pr_body,
            )
            return RemediationResult(
                plan=plan,
                executed=True,
                executed_at=_now(),
                pr_url=pr_url,
                pr_number=pr_number,
                branch_created=branch,
            )
        except Exception as e:  # noqa: BLE001
            return RemediationResult(
                plan=plan,
                executed=False,
                error=f"{type(e).__name__}: {e}",
            )

    # ─── helpers ───
    def _decide_action(
        self,
        sentinel: SentinelVerdict,
        investigator: InvestigatorVerdict,
    ) -> tuple[RemediationActionType, "_Target"]:
        # Healthy / no anomaly → no fix
        if sentinel.status == HealthStatus.HEALTHY:
            return RemediationActionType.NO_ACTION, _Target()

        # Need code evidence with a commit SHA to revert
        if not investigator.code_evidence:
            return RemediationActionType.NO_ACTION, _Target()

        ce = investigator.code_evidence[0]
        if not ce.commit_sha:
            return RemediationActionType.NO_ACTION, _Target()

        # Below confidence floor → don't propose a code change
        if investigator.confidence < MIN_CONFIDENCE_FOR_REVERT:
            return RemediationActionType.NO_ACTION, _Target()

        return RemediationActionType.REVERT_COMMIT, _Target(
            commit_sha=ce.commit_sha,
            pr_number=ce.pr_number,
            pr_url=ce.pr_url,
        )

    @staticmethod
    def _propose_branch_name(commit_sha: str | None, incident_id: str) -> str:
        sha8 = (commit_sha or "unknown")[:8]
        # Lowercase, no whitespace
        return f"airen/revert-{sha8}-{incident_id.lower()}"

    async def _draft_pr_copy(
        self,
        sentinel: SentinelVerdict,
        investigator: InvestigatorVerdict,
        action_type: RemediationActionType,
        incident_id: str,
        incident_report: IncidentReport | None,
    ) -> _PRNarrative:
        # Mock path — when AIREN_LLM_MODE=mock we generate a deterministic narrative
        # so the pipeline runs offline and tests are stable.
        from airen.mocks import is_mock_mode

        if is_mock_mode():
            return _mock_pr_narrative(investigator, action_type, incident_id)

        # Real LLM path — small ADK agent, no tools
        from google.adk.runners import InMemoryRunner
        from google.genai import types as genai_types

        agent = _build_pr_writer_agent()
        runner = InMemoryRunner(agent=agent, app_name="airen")
        session_id = secrets.token_hex(8)
        await runner.session_service.create_session(
            app_name="airen", user_id="akash", session_id=session_id
        )

        msg = (
            f"Draft the PR copy for this remediation.\n\n"
            f"action_type: {action_type.value}\n"
            f"incident_id: {incident_id}\n\n"
            f"SentinelVerdict JSON:\n{sentinel.model_dump_json(indent=2)}\n\n"
            f"InvestigatorVerdict JSON:\n{investigator.model_dump_json(indent=2)}\n\n"
            + (
                f"IncidentReport summary:\n{incident_report.tldr}\n"
                if incident_report
                else ""
            )
        )

        final = ""
        async for event in runner.run_async(
            user_id="akash",
            session_id=session_id,
            new_message=genai_types.Content(
                role="user", parts=[genai_types.Part(text=msg)]
            ),
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if getattr(part, "text", None):
                        final += part.text
        if not final.strip():
            # LLM hiccup — fall back to a deterministic narrative
            return _mock_pr_narrative(investigator, action_type, incident_id)
        try:
            return _PRNarrative.model_validate_json(extract_json(final))
        except Exception:
            return _mock_pr_narrative(investigator, action_type, incident_id)


# ───────────────────────────────────────────────────────────────────────
#  Internal types
# ───────────────────────────────────────────────────────────────────────
class _Target:
    """Tiny holder for commit/PR target — avoids reaching into ce repeatedly."""

    def __init__(
        self,
        commit_sha: str | None = None,
        pr_number: int | None = None,
        pr_url: str | None = None,
    ) -> None:
        self.commit_sha = commit_sha
        self.pr_number = pr_number
        self.pr_url = pr_url


def _mock_pr_narrative(
    investigator: InvestigatorVerdict,
    action_type: RemediationActionType,
    incident_id: str,
) -> _PRNarrative:
    ce = investigator.code_evidence[0] if investigator.code_evidence else None
    sha = (ce.commit_sha[:8] if ce and ce.commit_sha else "????????")
    pr_ref = f"PR #{ce.pr_number}" if ce and ce.pr_number else "the suspect PR"
    file_ref = ce.file_path if ce and ce.file_path else "the affected file"
    return _PRNarrative(
        pr_title=f"Revert {pr_ref}: api_fetch_limit causing long-haul prediction error",
        pr_body=(
            f"## Why\n"
            f"Airen detected a critical regression on the `tl-eta` model and traced it "
            f"to commit `{sha}` (introduced via {pr_ref}). See incident **{incident_id}**.\n\n"
            f"## What this changes\n"
            f"Reverts the change in `{file_ref}` so the inference path no longer truncates "
            f"the ping history.\n\n"
            f"## Risk\n"
            f"Low — this restores prior behavior already validated by training-time stats. "
            f"Reviewer please verify no downstream code depends on the new behavior before merging.\n\n"
            f"## Linked incident\n"
            f"- Incident `{incident_id}`\n"
            f"- Investigator confidence: {investigator.confidence:.2f}\n"
        ),
        rationale=(
            f"Revert is the right action because Investigator localized a specific commit "
            f"with {investigator.confidence:.2f} confidence. Hotfix would require additional "
            f"code analysis we don't yet have."
        ),
    )


# ───────────────────────────────────────────────────────────────────────
#  GitHub-side execution (only called from Remediator.execute under guards)
# ───────────────────────────────────────────────────────────────────────
def _open_revert_pr(
    repo_full_name: str,
    commit_sha: str,
    branch_name: str,
    title: str,
    body: str,
) -> tuple[str, int, str]:
    """Clone the repo, run `git revert <commit_sha>`, push, and open a draft PR.

    Flow:
        1. Shallow-clone into a tempdir using a token-authenticated HTTPS URL.
        2. Set local commit identity to "Airen" so authorship is clear.
        3. `git checkout -b <branch_name>` from the default branch tip.
        4. `git revert --no-edit <commit_sha>`.
        5. `git push --set-upstream origin <branch_name>`.
        6. Open a DRAFT PR via PyGithub.

    The tempdir is cleaned up via context manager — works on success and failure.
    Token is scrubbed from any error message before it propagates.
    """
    import subprocess
    import tempfile

    from github import Auth, Github

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GITHUB_TOKEN not set; cannot open PR")
    if not commit_sha:
        raise RuntimeError("No commit_sha to revert")

    # Token-embedded URL for clone+push. We never log this — only the public form.
    auth_url = f"https://x-access-token:{token}@github.com/{repo_full_name}.git"

    with tempfile.TemporaryDirectory(prefix="airen-revert-") as tmpdir:
        # Full clone — typical ML repos are small (<200MB) and we need history
        # for the revert to find the target commit.
        _git_run(["clone", "--quiet", auth_url, tmpdir], token=token)

        # Authorship: prevent commits showing up as the human whose PAT we used.
        # The no-reply form means the commit doesn't get attributed to any user.
        _git_run(["-C", tmpdir, "config", "user.email", "airen-bot@users.noreply.github.com"], token=token)
        _git_run(["-C", tmpdir, "config", "user.name", "Airen (autonomous ML reliability)"], token=token)

        # New branch from the default-branch tip (clone already checked out default).
        _git_run(["-C", tmpdir, "checkout", "-b", branch_name], token=token)

        # The actual revert. If it conflicts on a hot file we abort cleanly —
        # better to surface "revert needs human" than to push a half-baked branch.
        revert_proc = subprocess.run(
            ["git", "-C", tmpdir, "revert", "--no-edit", commit_sha],
            capture_output=True, text=True, timeout=120,
        )
        if revert_proc.returncode != 0:
            # Try to abort cleanly; ignore failure of the abort itself.
            subprocess.run(
                ["git", "-C", tmpdir, "revert", "--abort"],
                capture_output=True, text=True, timeout=30,
            )
            raise RuntimeError(
                f"git revert {commit_sha[:8]} failed — likely a merge conflict that "
                f"requires human resolution. stderr: {revert_proc.stderr.strip()[:300]}"
            )

        _git_run(["-C", tmpdir, "push", "--set-upstream", "origin", branch_name], token=token)

    # Tempdir cleaned up. Now the branch exists on the remote with a real revert
    # commit — open the draft PR pointing at it.
    client = Github(auth=Auth.Token(token))
    repo = client.get_repo(repo_full_name)
    pr = repo.create_pull(
        title=title,
        body=body,
        head=branch_name,
        base=repo.default_branch,
        draft=True,
    )
    return pr.html_url, pr.number, branch_name


def _git_run(args: list[str], token: str = "", timeout: int = 300) -> None:
    """Run `git <args>` and raise on failure. Scrubs the GitHub token from errors."""
    import subprocess

    proc = subprocess.run(
        ["git"] + args,
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        stderr = proc.stderr or ""
        if token:
            stderr = stderr.replace(token, "<TOKEN_REDACTED>")
        # Show just the git verb (args[0] or args[2] after -C <dir>)
        verb = next((a for a in args if not a.startswith("-") and a != args[0]), args[0])
        raise RuntimeError(
            f"git {verb} failed (exit {proc.returncode}). stderr: {stderr.strip()[:400]}"
        )
