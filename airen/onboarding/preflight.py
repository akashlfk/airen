"""Preflight — the secrets side of onboarding.

Principle: airen.yaml holds connection *topology* (hosts, topics, tables,
buckets, endpoints); .env holds ONLY secrets (passwords, tokens, keys). This
module computes which secret env vars a given config needs, reports which are
already set, and (optionally) scaffolds the missing ones into .env as blank
keys for the user to fill — never overwriting anything that already exists.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from airen.config import AirenServiceConfig

ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"


# (env var, why it's needed) — only declared when the relevant block is active.
def required_secret_vars(config: AirenServiceConfig) -> list[tuple[str, str]]:
    """The SECRET env vars this config needs. Non-secret config lives in yaml."""
    out: list[tuple[str, str]] = []

    if config.github is not None:
        out.append(("GITHUB_TOKEN", "clone/search the model repo (graphify + Investigator)"))

    if config.slack.enable:
        out.append(("SLACK_BOT_TOKEN", "post incident alerts to Slack"))

    if config.jira.enable:
        out.append(("AIREN_JIRA_API_TOKEN", "create/transition Jira incidents"))

    # MLflow: FK's tracking server is no-auth and the URI lives in yaml, so no
    # secret is required by default. (Add MLFLOW_TOKEN manually if yours needs auth.)

    if config.s3 is not None and config.s3.enable:
        out.append(("AWS_ACCESS_KEY_ID", "read training baselines from S3"))
        out.append(("AWS_SECRET_ACCESS_KEY", "read training baselines from S3"))

    if config.redshift is not None and config.redshift.enable:
        out.append(("REDSHIFT_PASSWORD", "connect to Redshift for actuals/rolling baseline"))

    if config.kafka is not None and config.kafka.enable:
        out.append(("KAFKA_SASL_USERNAME", "authenticate to the Kafka cluster"))
        out.append(("KAFKA_SASL_PASSWORD", "authenticate to the Kafka cluster"))

    # de-dup, keep first reason
    seen: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for var, why in out:
        if var not in seen:
            seen.add(var)
            deduped.append((var, why))
    return deduped


def _mode(var: str, default: str = "mock") -> str:
    return os.environ.get(var, default).strip().lower()


def _has(var: str) -> bool:
    return bool(os.environ.get(var, "").strip())


def runtime_warnings(config: AirenServiceConfig) -> list[str]:
    """Mode-aware readiness check, run right before the orchestrator starts.

    Returns human-readable warnings for every ENABLED source whose required
    detail (a secret in .env, or a connection field in yaml) is missing. It is
    mode-aware: a block whose adapter is in mock mode needs nothing, so it is
    not flagged. These are warnings, not errors — the run proceeds and the
    affected step is skipped or degrades.
    """
    w: list[str] = []
    llm_mock = _mode("AIREN_LLM_MODE", "real") == "mock"

    # ── LLM (the one that actually stops everything) ──
    if not llm_mock:
        backend = os.environ.get("AIREN_LLM_BACKEND", "gemini").strip().lower()
        if backend == "gemini" and not _has("GOOGLE_API_KEY"):
            w.append("LLM: GOOGLE_API_KEY is missing (AIREN_LLM_BACKEND=gemini). "
                     "Agents will fail — set it in .env, or run with AIREN_LLM_MODE=mock.")
        if backend == "azure" and not _has("AZURE_API_KEY"):
            w.append("LLM: AZURE_API_KEY is missing (AIREN_LLM_BACKEND=azure).")

    # ── GitHub (Investigator) — only used in real LLM mode ──
    if not llm_mock and config.github is not None and not _has("GITHUB_TOKEN"):
        w.append("GitHub: GITHUB_TOKEN is missing — the Investigator can't clone/search "
                 f"{config.github.repo!r}. Set it in .env.")

    # ── Slack (posts even in mock LLM mode) ──
    if config.slack.enable and not _has("SLACK_BOT_TOKEN"):
        w.append("Slack: alerts are enabled but SLACK_BOT_TOKEN is missing — "
                 "the incident will NOT be posted to Slack.")

    # ── Jira (only when its adapter is real) ──
    if config.jira.enable and _mode("AIREN_JIRA_MODE") == "real":
        if not (_has("AIREN_JIRA_API_TOKEN") or _has("AIREN_JIRA_API_KEY")):
            w.append("Jira: real mode but AIREN_JIRA_API_TOKEN is missing — ticket creation will fail.")
        if not (config.jira.base_url or _has("AIREN_JIRA_BASE_URL")):
            w.append("Jira: no base_url — set jira.base_url in airen.yaml or AIREN_JIRA_BASE_URL in .env.")

    # ── S3 baselines (only when its adapter is real) ──
    if config.s3 is not None and config.s3.enable and _mode("AIREN_S3_MODE") == "real":
        if not (config.s3.bucket or _has("S3_BASELINES_BUCKET")):
            w.append("S3: enabled but no bucket — set s3.bucket in airen.yaml or S3_BASELINES_BUCKET in .env.")
        if not _has("AWS_ACCESS_KEY_ID") or not _has("AWS_SECRET_ACCESS_KEY"):
            w.append("S3: AWS credentials missing — can't read training baselines "
                     "(Sentinel will fall back to the yaml attribute_baselines).")

    # ── Redshift (only when its adapter is real) ──
    if config.redshift is not None and config.redshift.enable and _mode("AIREN_REDSHIFT_MODE") == "real":
        if not _has("REDSHIFT_PASSWORD"):
            w.append("Redshift: real mode but REDSHIFT_PASSWORD is missing in .env.")
        if not (config.redshift.host or _has("REDSHIFT_HOST") or _has("REDSHIFT_DSN")):
            w.append("Redshift: no host — set redshift.host in airen.yaml or REDSHIFT_HOST/REDSHIFT_DSN in .env.")

    # ── MLflow (only when its adapter is real) ──
    if config.mlflow is not None and config.mlflow.enable and _mode("AIREN_MLFLOW_MODE") == "real":
        if not (config.mlflow.tracking_uri or _has("MLFLOW_TRACKING_URI")):
            w.append("MLflow: real mode but no tracking_uri — set mlflow.tracking_uri in airen.yaml "
                     "or MLFLOW_TRACKING_URI in .env.")

    return w


def print_runtime_warnings(config: AirenServiceConfig) -> list[str]:
    """Print the readiness warnings as a clear block. Returns the warnings."""
    warns = runtime_warnings(config)
    if not warns:
        return []
    print()
    print("  " + "━" * 74)
    print("  ⚠️  MISSING RESOURCES — some enabled sources are missing details:")
    print("  " + "━" * 74)
    for msg in warns:
        print(f"     • {msg}")
    print("  " + "─" * 74)
    print("     The run will continue, but the steps above may be skipped or fail.")
    print("     Fix: edit .env (secrets) or services/<svc>/airen.yaml (hosts/topics/tables).")
    print("  " + "━" * 74)
    print()
    return warns


def _env_keys_present(env_path: Path) -> set[str]:
    """Keys that already appear in .env (even if blank)."""
    keys: set[str] = set()
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                keys.add(line.split("=", 1)[0].strip())
    return keys


def preflight_report(
    config: AirenServiceConfig,
    *,
    scaffold_env: bool = True,
    env_path: Path | None = None,
) -> list[tuple[str, str, bool]]:
    """Print a ✓/✗ report of required secrets and (optionally) scaffold the
    missing ones into .env as blank keys. Returns [(var, why, is_set)].

    'is_set' means the var has a non-empty value in the current environment.
    Keys already declared in .env (even blank) are NOT re-added.
    """
    env_path = env_path or ENV_PATH
    required = required_secret_vars(config)
    if not required:
        print("\n  Preflight: no secrets required for this config (all sources disabled).")
        return []

    declared = _env_keys_present(env_path)
    rows: list[tuple[str, str, bool]] = []
    to_add: list[tuple[str, str]] = []

    print("\n── Preflight: required secrets (.env) " + "─" * 38)
    print("   Non-secret config (hosts, topics, tables…) is in airen.yaml.")
    for var, why in required:
        is_set = bool(os.environ.get(var, "").strip())
        mark = "✓ set" if is_set else ("• in .env (blank)" if var in declared else "✗ missing")
        print(f"   [{mark:>16}]  {var:<24} — {why}")
        rows.append((var, why, is_set))
        if not is_set and var not in declared:
            to_add.append((var, why))

    missing_set = [v for v, _, is_set in rows if not is_set]

    if scaffold_env and to_add:
        _append_blank_keys(env_path, to_add, config.service.name)
        print()
        print("   " + "━" * 70)
        print("   ⚠️  ACTION REQUIRED — fill in your secrets before running Airen")
        print("   " + "━" * 70)
        print(f"   I added {len(to_add)} blank key(s) to {env_path}.")
        print("   Open that file and paste the real value after each '=' :")
        for var, _ in to_add:
            print(f"       {var}=<your value here>")
        print("   (Leave any you don't use blank — Airen skips disabled sources.)")
    elif missing_set:
        # not scaffolding, but some are still unset
        print("\n   ⚠️  Set these in your .env before running Airen:")
        for var in missing_set:
            print(f"       {var}=<your value here>")
    else:
        print("\n   ✓ All required secret keys are already set. You're good to go.")

    return rows


def _append_blank_keys(env_path: Path, keys: list[tuple[str, str]], service: str) -> None:
    """Append blank `KEY=` lines under a labeled header. Never overwrites."""
    header = f"\n# ── added by airen onboard for '{service}' on {date.today().isoformat()} ──\n"
    # Comment on its own line above each key — unambiguous for all .env parsers.
    body = "".join(f"# {why}\n{var}=\n" for var, why in keys)
    with env_path.open("a", encoding="utf-8") as f:
        f.write(header + body)
