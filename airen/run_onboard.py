"""Guided onboarding — describe a target ML application and generate a validated
`services/<name>/airen.yaml`.

Two modes:

    # Agentic — graphify a repo, auto-detect model/frameworks/APIs, interview gaps
    python -m airen.run_onboard --repo owner/repo
    python -m airen.run_onboard --path /local/checkout
    python -m airen.run_onboard --repo owner/repo --name my-service --force

    # Manual — pure interview, no repo (the deterministic fallback)
    python -m airen.run_onboard
    python -m airen.run_onboard --minimal      # only the required questions

This is the *front door* for pointing Airen at an ML app it has never seen.
Both modes emit the same validated yaml via airen.onboarding.io.

Design notes:
  - Every prompt has a sensible default in [brackets]; Enter takes it.
  - Only `service.name` and `phoenix.project_name` are strictly required.
  - The config is validated against AirenServiceConfig BEFORE anything is
    written — a bad answer fails before touching disk.
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from airen.config import (
    SERVICES_DIR,
    AirenServiceConfig,
    list_available_services,
)
from airen.onboarding import io


def _flag_value(argv: list[str], flag: str) -> str | None:
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            return argv[i + 1]
    return None


# ───────────────────────────────────────────────────────────────────────
#  Manual interview (no repo)
# ───────────────────────────────────────────────────────────────────────
def interview(minimal: bool = False) -> dict:
    cfg: dict = {}

    io.section("Service identity", "What is this ML application Airen will monitor?")
    name = io.ask_required("Service name (short id, kebab-case, e.g. 'tl-eta')")
    cfg["service"] = {"name": name}
    desc = io.ask("One-line description", None)
    if desc:
        cfg["service"]["description"] = desc
    owner = io.ask("Owner team / Slack handle", None)
    if owner:
        cfg["service"]["owner_team"] = owner
    ctx = io.ask("Operator context for the agents? (deps, known issues, retrain policy — blank to skip)", None)
    if ctx:
        cfg["service"]["context"] = ctx

    io.section("Observability — Phoenix",
               "Where does the model emit prediction spans? (this is how Airen sees health)")
    cfg["phoenix"] = {
        "project_name": io.ask_required("Phoenix project name", f"{name}-prediction"),
        "collector_endpoint": io.ask("Phoenix collector endpoint", "http://localhost:6006"),
    }

    io.section("Code source — GitHub",
               "Where does the model/feature code live? Investigator searches it for the cause.")
    if io.ask_bool("Configure a GitHub repo for root-cause investigation?", default=not minimal):
        repo = io.ask_required("Repo (owner/repo)")
        github: dict = {"repo": repo, "default_branch": io.ask("Default branch to investigate", "main")}
        attrs = io.ask_list("  Feature attributes Investigator should grep for (e.g. api_fetch_limit, seq_len)")
        if attrs:
            github["attributes_of_interest"] = attrs
        cfg["github"] = github

    io.section("Reporting — Slack", "Where should Airen post incident alerts?")
    slack: dict = {"enable": io.ask_bool("Post alerts to Slack?", default=True)}
    if slack["enable"]:
        slack["alert_channel"] = io.ask(
            "Alert channel (e.g. #airen-alerts); blank = use global SLACK_CHANNEL env", None)
    cfg["slack"] = slack

    io.section("Reporting — Jira", "Should Airen file an incident ticket?")
    jira_enable = io.ask_bool("File incidents in Jira?", default=False)
    jira: dict = {"enable": jira_enable}
    if jira_enable:
        jira["project_key"] = io.ask_required("Jira project key (e.g. ETAI)")
        jira["issue_type"] = io.ask("Issue type (must exist in the project)", "Task")
        if not minimal:
            for field, label in (
                ("transition_on_alert", "Transition when alert posts"),
                ("transition_on_pass", "Transition when validation PASSes"),
                ("transition_on_fail", "Transition when validation FAILs"),
            ):
                t = io.ask(f"{label} (blank = skip / comment-only)", None)
                if t:
                    jira[field] = t
    cfg["jira"] = jira

    io.section("Health thresholds — Sentinel",
               "What does 'healthy' mean for this model, and when should Airen alarm?")
    sentinel: dict = {
        "baseline_mae_minutes": io.ask_float("Healthy baseline error (MAE, in minutes)", 150.0),
        "warning_ratio": io.ask_float("WARNING when current/baseline ratio ≥", 1.2),
        "critical_ratio": io.ask_float("CRITICAL when current/baseline ratio ≥", 2.0),
        "default_lookback_minutes": io.ask_int("Analysis lookback window (minutes)", 2880),
    }
    if not minimal:
        print("\n  Drift baselines (PSI): training-time expected value per attribute.\n"
              "  Enter as key=value pairs, e.g.  api_fetch_limit=184, seq_len=48")
        raw = input("  attribute baselines: ").strip()
        if raw:
            baselines: dict[str, str] = {}
            for pair in raw.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    baselines[k.strip()] = v.strip()
            if baselines:
                sentinel["attribute_baselines"] = baselines
                sentinel["psi_warning"] = io.ask_float("PSI WARNING threshold", 0.10)
                sentinel["psi_critical"] = io.ask_float("PSI CRITICAL threshold", 0.25)
    cfg["sentinel"] = sentinel

    if not minimal:
        io.section("Model registry (optional)",
                   "Map each production model_family → its MLflow experiment + training repo.")
        if io.ask_bool("Add model-registry entries?", default=False):
            registry: list[dict] = []
            while True:
                fam = io.ask("model_family (blank to finish)", None)
                if not fam:
                    break
                entry: dict = {"model_family": fam, "experiment_id": io.ask_required("  MLflow experiment id")}
                entry["expected_repo"] = io.ask(
                    "  Expected training repo (owner/repo; blank = flag as ungoverned)", None)
                notes = io.ask("  Notes", None)
                if notes:
                    entry["notes"] = notes
                registry.append(entry)
            if registry:
                cfg["model_registry"] = registry

    return cfg


# ───────────────────────────────────────────────────────────────────────
#  Entry point
# ───────────────────────────────────────────────────────────────────────
def main() -> None:
    argv = sys.argv[1:]
    minimal = "--minimal" in argv
    force = "--force" in argv
    repo = _flag_value(argv, "--repo")
    path = _flag_value(argv, "--path")
    name = _flag_value(argv, "--name")
    branch = _flag_value(argv, "--branch")

    print()
    print("═" * 78)
    print("  AIREN ONBOARDING  ·  describe an ML application for Airen to monitor")
    print("═" * 78)
    existing = list_available_services()
    if existing:
        print(f"  Already onboarded: {', '.join(existing)}")

    # ── Agentic path: graphify a repo ──
    if repo or path:
        from airen.onboarding.agent import onboard_from_repo

        try:
            onboard_from_repo(
                repo or path,            # type: ignore[arg-type]
                service_name=name,
                is_local=bool(path),
                branch=branch,
                interactive=True,
                force=force,
            )
        except (KeyboardInterrupt, EOFError):
            print("\n\n✗ Onboarding cancelled. Nothing written.\n")
            sys.exit(130)
        return

    # ── Manual path: pure interview ──
    print("  Press Enter to accept the [default] for any question.")
    try:
        cfg = interview(minimal=minimal)
    except (KeyboardInterrupt, EOFError):
        print("\n\n✗ Onboarding cancelled. Nothing written.\n")
        sys.exit(130)

    service_name = cfg["service"]["name"]
    try:
        AirenServiceConfig.model_validate(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"\n❌ Generated config failed validation: {type(e).__name__}: {e}")
        print("   Nothing was written. Please re-run and adjust your answers.\n")
        sys.exit(1)

    out_dir = SERVICES_DIR / service_name
    out_path = out_dir / "airen.yaml"
    if out_path.exists() and not force:
        print(f"\n⚠️  {out_path} already exists.")
        if not io.ask_bool("Overwrite it?", default=False):
            print("✗ Left existing config untouched. (Use --force to skip this prompt.)\n")
            sys.exit(0)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(io.render_yaml(cfg, service_name))

    print()
    print("═" * 78)
    print(f"  ✅ Wrote {out_path}")
    print("═" * 78)

    from airen.onboarding.preflight import preflight_report

    preflight_report(AirenServiceConfig.model_validate(cfg), scaffold_env=True)

    print("\n  ── Next steps " + "─" * 64)
    print(f"    1. Review / tweak the config: {out_path}")
    print(f"    2. Fill in your secrets:      .env  (the keys listed above)")
    print(f"    3. Try it with NO LLM/creds:  AIREN_LLM_MODE=mock python -m airen.run_orchestrator {service_name}")
    print(f"    4. Run for real:              python -m airen.run_orchestrator {service_name}\n")


if __name__ == "__main__":
    main()
