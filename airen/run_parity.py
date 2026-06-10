"""Run the ParityAuditor agent — audit train↔serve preprocessing parity per model.

The agent reads the SERVING repo (github.repo) and the TRAINING repo
(github.training_repo, often separate) via code tools, pairs each served model to
its training counterpart, and reports per-model skew. Every run is traced to Phoenix.

Usage:
    python -m airen.run_parity <service>
    AIREN_LLM_BACKEND=azure python -m airen.run_parity tl-eta-prediction-production
    python -m airen.run_parity --list
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from airen.instrumentation import setup_tracing

setup_tracing()

from opentelemetry import trace

from airen.agents.parity_auditor import root_agent
from airen.config import AirenServiceConfig, list_available_services, load_service_config
from airen.llm_factory import extract_json
from airen.schemas import ParityReport

_tracer = trace.get_tracer("airen.parity")


def _served_model_hints(cfg: AirenServiceConfig) -> str:
    hints: list[str] = []
    for e in (cfg.model_registry or []):
        if e.model_family:
            hints.append(e.model_family)
    for s in (cfg.observation.segments or []):
        if "model" in s.lower():
            hints.append(s)
    seen = list(dict.fromkeys(hints))
    return ", ".join(seen) if seen else "(none known — discover the served models from the serving repo)"


def _build_message(cfg: AirenServiceConfig) -> str:
    gh = cfg.github
    serving_repo = gh.repo if gh else None
    serving_branch = (gh.default_branch if gh else None) or "default"
    training_repo = gh.training_repo if gh else None
    training_branch = (gh.training_branch if gh else None) or "default"
    training_line = (
        f"TRAINING repo: {training_repo} (branch: {training_branch})"
        if training_repo
        else "TRAINING repo: NONE CONFIGURED — if you find no training code in the serving "
             "repo, report each model as NO_TRAINING_CODE and recommend setting github.training_repo."
    )
    return (
        f"Audit training↔serving preprocessing parity for service '{cfg.service.name}'.\n"
        f"SERVING repo: {serving_repo} (branch: {serving_branch})\n"
        f"{training_line}\n"
        f"Model families hinted by the live feed / model_registry: {_served_model_hints(cfg)}\n\n"
        f"Discover the served models, pair each to its training counterpart, diff the "
        f"preprocessing, and produce the ParityReport JSON."
    )


async def run_parity(cfg: AirenServiceConfig) -> ParityReport | None:
    from airen.agents.orchestrator import _run_agent_with_retry  # retry/backoff reuse

    if not (cfg.github and cfg.github.repo):
        print("❌ This service has no github.repo configured — nothing to audit.")
        return None

    msg = _build_message(cfg)
    print("\n" + "═" * 78)
    print(f"  PARITY AUDIT  ·  service={cfg.service.name}")
    print(f"  serving={cfg.github.repo}  training={cfg.github.training_repo or '(none configured)'}")
    print("═" * 78)
    print("  🔎 agent reading both repos (this calls the LLM + clones repos on first run)…\n")

    span_id = None
    with _tracer.start_as_current_span("airen.parity_audit") as span:
        span_id = format(span.get_span_context().span_id, "016x")
        raw = await _run_agent_with_retry(root_agent, msg)
    try:
        report = ParityReport.model_validate_json(extract_json(raw))
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ couldn't parse a ParityReport: {type(e).__name__}: {e}")
        print("  Raw agent output:\n" + raw[:2000])
        return None

    _print_report(report)
    _log_to_phoenix(report, span_id)
    out = Path(__file__).resolve().parent.parent / "logs" / f"parity-{cfg.service.name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.model_dump(mode="json"), indent=2))
    print(f"\n  Full ParityReport JSON → {out}\n")
    return report


_STATUS_EMOJI = {
    "PARITY_OK": "🟢", "SKEW_RISK": "🟡", "SKEW_CONFIRMED": "🔴",
    "UNVERIFIABLE": "⚪️", "NO_TRAINING_CODE": "⛔",
}


def _print_report(r: ParityReport) -> None:
    print("─" * 78)
    print(f"  Served models discovered ({len(r.served_models)}): {', '.join(r.served_models) or '—'}")
    print("─" * 78)
    for f in r.findings:
        em = _STATUS_EMOJI.get(f.status, "•")
        print(f"\n  {em} {f.model_family}  [{f.status}]  (type={f.model_type}, conf {f.confidence:.2f})")
        print(f"     served:  {f.served_at or '—'}")
        print(f"     trained: {f.trained_at or '—'}  (paired={f.paired})")
        for s in f.skews:
            print(f"       ⚠ {s}")
        if f.notes:
            print(f"     ↳ {f.notes}")
    print("\n" + "═" * 78)
    print(f"  SUMMARY: {r.summary}")
    print("═" * 78)


def _log_to_phoenix(r: ParityReport, span_id: str | None) -> None:
    """Attach each model's parity finding to the audit span as an annotation."""
    if not span_id:
        return
    try:
        import os

        from phoenix.client import Client

        client = Client(base_url=os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006"))
        for f in r.findings:
            client.spans.add_span_annotation(
                span_id=span_id,
                annotation_name=f"parity.{f.model_family}",
                annotator_kind="LLM",
                label=f.status,
                score=f.confidence,
                explanation=(" | ".join(f.skews) or f.notes)[:1000],
            )
    except Exception:
        pass


def main() -> None:
    argv = sys.argv[1:]
    if "--list" in argv or not argv:
        print("Usage: python -m airen.run_parity <service>")
        print(f"Onboarded services: {list_available_services() or '(none)'}")
        return
    try:
        cfg = load_service_config(argv[0])
    except FileNotFoundError as e:
        print(f"❌ {e}")
        sys.exit(1)
    asyncio.run(run_parity(cfg))


if __name__ == "__main__":
    main()
