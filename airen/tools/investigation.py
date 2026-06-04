"""Investigation tools — cross-reference Phoenix anomalies with GitHub.

Architecture pattern (see [[agent-architecture-pattern]]):
  Deterministic Python does the heavy lifting (search, bisect, rank).
  The LLM only writes the narrative summary.

These tools take a Sentinel anomaly + a repo, and return suspect commits
ranked by how likely they caused the anomaly. The agent doesn't decide
the ranking — it just reports it.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from airen.adapters import github as gh
from airen.adapters.graphify import get_graphify_adapter
from airen.adapters.mlflow_adapter import get_mlflow_adapter
from airen.adapters.phoenix import flatten_mlre, get_recent_spans


# ───────────────────────────────────────────────────────────────────────
#  Phoenix-side tools — get the data backing an anomaly
# ───────────────────────────────────────────────────────────────────────
def query_anomaly_spans(
    project_name: str,
    attribute_name: str,
    attribute_value: str,
    lookback_minutes: int = 2880,
    max_rows: int = 200,
) -> dict:
    """Pull the actual spans that are part of an anomaly.

    The attribute_name/value come from the live anomaly's segment — whatever it
    was (a feature, a parameter, a customer/entity), never a fixed attribute.

    Args:
        project_name: the service's Phoenix project.
        attribute_name: the anomalous span attribute, e.g. "input.<feature>"
                        or "input.<entity>" (whatever the anomaly named).
        attribute_value: the segment value to filter to, as a string.
        lookback_minutes: time window.
        max_rows: hard cap on spans returned.
    """
    spans = get_recent_spans(project_name=project_name, lookback_minutes=lookback_minutes)
    if spans.empty:
        return {
            "project_name": project_name,
            "attribute_name": attribute_name,
            "attribute_value": attribute_value,
            "n_matching": 0,
            "note": "No spans in window — nothing to investigate.",
        }
    df = flatten_mlre(spans)
    if attribute_name not in df.columns:
        return {
            "project_name": project_name,
            "attribute_name": attribute_name,
            "attribute_value": attribute_value,
            "n_matching": 0,
            "available_attributes": [c for c in df.columns if "." in c][:30],
            "note": f"Attribute {attribute_name!r} not present on these spans.",
        }
    matched = df[df[attribute_name].astype(str) == str(attribute_value)]
    if matched.empty:
        return {
            "project_name": project_name,
            "attribute_name": attribute_name,
            "attribute_value": attribute_value,
            "n_matching": 0,
            "note": f"No spans match {attribute_name}={attribute_value}.",
        }

    earliest = matched["start_time"].min() if "start_time" in matched.columns else None
    latest = matched["start_time"].max() if "start_time" in matched.columns else None
    mae = None
    if "eval.absolute_error_minutes" in matched.columns:
        e = matched["eval.absolute_error_minutes"].dropna().astype(float)
        mae = round(float(e.mean()), 1) if len(e) else None

    return {
        "project_name": project_name,
        "attribute_name": attribute_name,
        "attribute_value": attribute_value,
        "n_matching": int(len(matched)),
        "earliest_seen": str(earliest) if earliest is not None else None,
        "latest_seen": str(latest) if latest is not None else None,
        "mae_for_segment": mae,
        "sample_request_ids": matched.get("request_id", []).head(5).tolist()
        if "request_id" in matched.columns
        else [],
    }


# ───────────────────────────────────────────────────────────────────────
#  GitHub-side tools — link the anomaly to code
# ───────────────────────────────────────────────────────────────────────
def find_commits_introducing_pattern(
    repo_full_name: str,
    pattern: str,
    days_back: int = 30,
    max_commits: int = 30,
) -> dict:
    """Find recent commits that touch files containing `pattern`.

    Strategy:
      1. Search the repo for files containing the literal pattern.
      2. For each matching file, list recent commits that modified it.
      3. Rank commits by recency; return as suspect list.

    This is how Investigator turns "the anomaly involves <attribute>=<value>"
    into "commit <sha> introduced <attribute>=<value> on <date>". The pattern is
    whatever attribute the live anomaly named — never a fixed feature.

    Args:
        repo_full_name: owner/repo of the service under investigation.
        pattern: the literal symbol from the anomaly, e.g. a feature/param name
                 like "vessel_class", "embedding_dim", "seq_len", "api_fetch_limit".
        days_back: how far back to look for commits.
        max_commits: cap on returned suspects.
    """
    # Step 1 — find files containing the pattern. Bail out early on common
    # "no-op" patterns that come from non-feature segments (e.g. an
    # 'overall' or 'prediction_volume' anomaly when the model is down).
    if not pattern or pattern.lower() in {"overall", "prediction_volume", "all", "none"}:
        return {
            "repo": repo_full_name,
            "pattern": pattern,
            "files_with_pattern": [],
            "suspect_commits_ranked": [],
            "note": (
                f"Pattern {pattern!r} is not a meaningful code identifier — "
                "this anomaly type (e.g. zero traffic) typically indicates an "
                "infrastructure issue, not a code change. Look at recent deploys "
                "or service health instead of the codebase."
            ),
        }

    code_hits = gh.search_code(repo_full_name=repo_full_name, pattern=pattern, max_results=10)
    if not code_hits or (len(code_hits) == 1 and "error" in code_hits[0]):
        return {
            "repo": repo_full_name,
            "pattern": pattern,
            "files_with_pattern": [],
            "suspect_commits_ranked": [],
            "note": code_hits[0]["error"] if code_hits and "error" in code_hits[0] else f"No code matches for {pattern!r}.",
        }
    files = [h["path"] for h in code_hits]

    # Step 2 — for each file, find the most recent commits touching it
    suspects: list[dict] = []
    for f in files[:5]:  # cap files to scan
        blame = gh.get_blame_for_line(repo_full_name=repo_full_name, file_path=f, line_number=1)
        if blame:
            blame["file_path"] = f
            suspects.append(blame)

    # Step 3 — also pull recent overall commits (in case search-code missed older edits)
    recent = gh.list_recent_commits(repo_full_name=repo_full_name, days_back=days_back, max_commits=max_commits)
    # Heuristic: prioritize commits whose message or filename mentions the pattern
    ranked: list[dict] = []
    for c in recent:
        score = 0
        if pattern.lower() in (c.get("message") or "").lower():
            score += 10
        for f in c.get("files_changed", []):
            if any(file_hit in f for file_hit in files):
                score += 5
        if score > 0:
            c["_relevance"] = score
            ranked.append(c)
    ranked.sort(key=lambda x: x["_relevance"], reverse=True)

    return {
        "repo": repo_full_name,
        "pattern": pattern,
        "files_with_pattern": files,
        "blame_per_file": suspects,
        "suspect_commits_ranked": ranked[:10],
        "n_recent_commits_scanned": len(recent),
    }


def get_commit_diff(repo_full_name: str, sha: str) -> dict:
    """Fetch a specific commit's full diff + PR info if available."""
    details = gh.get_commit_details(repo_full_name=repo_full_name, sha=sha)
    pr = gh.get_pr_for_commit(repo_full_name=repo_full_name, sha=sha)
    return {"commit": details, "pull_request": pr}


# ───────────────────────────────────────────────────────────────────────
#  MLflow tools — training-time vs inference-time cross-reference
# ───────────────────────────────────────────────────────────────────────
def inspect_training_code(model_family: str) -> dict:
    """Resolve a production model_family back to its training code.

    Use this when an anomaly names a specific `model_family` (e.g. "PatchTST",
    "CHEP_Classification") and you want to inspect the actual training script
    that produced the deployed model — not just MLflow params.

    Pipeline:
      1. Look up `model_family` in the service's `model_registry` config.
      2. Fetch MLflow training provenance for that experiment (commit + path).
      3. If the source is in a known repo (e.g. cloudqwest/dynamic_eta_prediction),
         use Graphify to read the file at the exact training commit.
      4. Surface reproducibility warnings if the training source isn't in
         version control (Jupyter cells, personal-disk scripts, etc.).

    Args:
        model_family: e.g. "PatchTST", "CHEP_Classification". Must match a
            model_registry entry in the active service's airen.yaml.

    Returns:
        Dict with: model_family, provenance (TrainingProvenance shape),
        training_code (str, the source file contents) or training_code_error,
        and governance_summary (HEALTHY | GOVERNANCE_RISK + reason).
    """
    from airen.config import list_available_services, load_service_config

    # Find which service has this model_family
    entry = None
    expected_repo = None
    for svc in list_available_services():
        try:
            cfg = load_service_config(svc)
        except Exception:
            continue
        for e in (cfg.model_registry or []):
            if e.model_family == model_family:
                entry = e
                expected_repo = e.expected_repo
                break
        if entry is not None:
            break

    if entry is None:
        return {
            "model_family": model_family,
            "error": f"model_family {model_family!r} not in any service's model_registry. "
                     "Add it to services/<svc>/airen.yaml::model_registry to enable governance checks.",
        }

    # Pull provenance from MLflow
    adapter = get_mlflow_adapter()
    try:
        prov = adapter.get_training_provenance(
            entry.experiment_id, expected_repo=expected_repo
        )
    finally:
        adapter.close()

    # Try to read the training script at the exact commit, when traceable
    training_code = None
    training_code_path = None     # actual repo path we ended up reading
    training_code_error = None
    extra_warnings: list[str] = []
    if prov.get("has_git_traceability") and expected_repo and prov.get("git_commit"):
        sha = prov["git_commit"]
        source_path = prov.get("source_path") or ""

        try:
            import subprocess

            # Use the same cache the Graphify real adapter uses.
            from airen.adapters.graphify_real import _ensure_repo

            local_path = _ensure_repo(expected_repo)

            # Strategy 1: exact repo-relative path mapping.
            candidates: list[str] = []
            repo_relative = _strip_local_prefix_to_repo_path(source_path, expected_repo)
            if repo_relative:
                candidates.append(repo_relative)

            # Strategy 2: filename-only fallback (training source was moved/renamed
            # locally before the run — the file exists in the repo at a different path).
            filename = source_path.rsplit("/", 1)[-1] if source_path else ""
            if filename.endswith(".py"):
                ls = subprocess.run(
                    ["git", "-C", str(local_path), "ls-tree", "-r", "--name-only", sha],
                    capture_output=True, text=True, timeout=30,
                )
                if ls.returncode == 0:
                    for line in ls.stdout.splitlines():
                        if line.endswith("/" + filename) and line not in candidates:
                            candidates.append(line)

            # Try each candidate in order; first success wins.
            for cand in candidates:
                proc = subprocess.run(
                    ["git", "-C", str(local_path), "show", f"{sha}:{cand}"],
                    capture_output=True, text=True, timeout=30,
                )
                if proc.returncode == 0:
                    training_code = proc.stdout
                    training_code_path = cand
                    if cand != repo_relative:
                        extra_warnings.append(
                            f"MLflow source.name path ({repo_relative or source_path!r}) does NOT "
                            f"exist at commit {sha[:8]}. Fell back to {cand!r}. This means the "
                            f"trainer had local edits not in git — partial reproducibility risk."
                        )
                    break

            if training_code is None:
                training_code_error = (
                    f"No file matching {filename!r} found at commit {sha[:8]} in "
                    f"{expected_repo}. Tried: {candidates}. Likely: the training "
                    f"source was a local-only file never committed to git."
                )
                extra_warnings.append(
                    "Training source file not found at the logged commit — "
                    "reproducibility is partial at best (logged commit doesn't "
                    "contain the training script)."
                )
        except Exception as e:
            training_code_error = f"{type(e).__name__}: {str(e)[:200]}"

    # Merge any extra warnings we discovered while trying to fetch the source
    if extra_warnings:
        prov_warnings = list(prov.get("reproducibility_warnings") or [])
        prov_warnings.extend(extra_warnings)
        prov["reproducibility_warnings"] = prov_warnings
        # If we discovered a path mismatch, downgrade traceability to "partial"
        prov["has_git_traceability"] = False

    has_trace = bool(prov.get("has_git_traceability"))
    governance = {
        "status": "HEALTHY" if has_trace else "GOVERNANCE_RISK",
        "reason": (
            "Training source is git-traceable; the deployed model can be reproduced."
            if has_trace
            else "Training source lacks git provenance. See reproducibility_warnings."
        ),
    }

    return {
        "model_family": model_family,
        "service": entry.notes or "(no notes)",
        "provenance": prov,
        "training_code": training_code,
        "training_code_path": training_code_path,
        "training_code_size_bytes": len(training_code) if training_code else 0,
        "training_code_error": training_code_error,
        "governance_summary": governance,
    }


def _strip_local_prefix_to_repo_path(source_path: str, expected_repo: str) -> str | None:
    """MLflow's mlflow.source.name is the trainer's local path. Recover the
    repo-relative path by finding where the repo name appears in the path.
    Returns None if the path can't be normalized.
    """
    if not source_path or not expected_repo:
        return None
    repo_name = expected_repo.split("/")[-1]  # owner/repo → repo
    # Look for /repo_name/ in the path — everything after that is repo-relative
    needle = f"/{repo_name}/"
    idx = source_path.find(needle)
    if idx == -1:
        return None
    return source_path[idx + len(needle):].lstrip("/")


def get_training_run_params(experiment_name: str) -> dict:
    """Fetch the latest training run for an MLflow experiment.

    Use this to compare training-time settings (was the anomalous attribute
    set during training, and to what value?) against production span attributes.
    A mismatch between training params and production span attrs is a STRONG
    signal that a config drift or code change introduced the regression.

    Args:
        experiment_name: MLflow experiment name (e.g., "tl-eta-prod") OR experiment_id ("42").

    Returns:
        Dict with run_id, params, metrics, tags, start_time_ms. params is a
        dict[str, str] of every hyperparameter logged at training time.
    """
    adapter = get_mlflow_adapter()
    try:
        return adapter.get_latest_run(experiment_name)
    finally:
        adapter.close()


def detect_training_inference_mismatch(
    experiment_name: str,
    span_attribute: str,
    observed_value: str,
) -> dict:
    """Check whether a production span attribute value matches training-time settings.

    One hypothesis among several (use it when the anomaly is on a feature/param,
    not for drift/infra causes). Given the anomaly's attribute=value, this checks
    whether the training run ever used that value. If the training run has no such
    param (or a very different value), production is running on something the
    model was never trained against — a confirmed training/inference mismatch.

    Args:
        experiment_name: MLflow experiment to compare against.
        span_attribute: the anomalous attribute name (whatever the anomaly named).
        observed_value: the value flagged by Sentinel, as a string.

    Returns:
        Dict with:
          training_value: what training had (None if absent)
          observed_value: what production has
          mismatch: bool — True when training is silent or differs
          severity: 'high' | 'medium' | 'low' — high when training is silent
          description: human-readable summary for the verdict
    """
    adapter = get_mlflow_adapter()
    try:
        run = adapter.get_latest_run(experiment_name)
    finally:
        adapter.close()

    if "error" in run:
        return {
            "training_value": None,
            "observed_value": observed_value,
            "mismatch": False,
            "severity": "unknown",
            "description": f"Could not load training run: {run['error']}",
        }

    training_value = run.get("params", {}).get(span_attribute)
    if training_value is None:
        return {
            "training_value": None,
            "observed_value": observed_value,
            "mismatch": True,
            "severity": "high",
            "description": (
                f"Training run {run.get('run_id', '?')} has NO `{span_attribute}` parameter, "
                f"but production spans are setting it to {observed_value!r}. "
                "This is a training/inference mismatch — production is using a parameter "
                "the model was never trained against."
            ),
            "run_id": run.get("run_id"),
        }

    if str(training_value).strip() != str(observed_value).strip():
        return {
            "training_value": training_value,
            "observed_value": observed_value,
            "mismatch": True,
            "severity": "medium",
            "description": (
                f"Training run {run.get('run_id', '?')} used `{span_attribute}={training_value}` "
                f"but production is using `{span_attribute}={observed_value}`. "
                "Behavior may differ from what the model expects."
            ),
            "run_id": run.get("run_id"),
        }

    return {
        "training_value": training_value,
        "observed_value": observed_value,
        "mismatch": False,
        "severity": "low",
        "description": (
            f"Training and production agree on `{span_attribute}={training_value}`. "
            "This attribute is NOT the cause of the anomaly."
        ),
        "run_id": run.get("run_id"),
    }


# ───────────────────────────────────────────────────────────────────────
#  Graphify tools — code knowledge graph queries
# ───────────────────────────────────────────────────────────────────────
def find_symbol_callers(repo: str, symbol: str, max_results: int = 10) -> dict:
    """Find every call site of `symbol` in the repo (via Graphify).

    Use this AFTER find_commits_introducing_pattern when you need the exact line
    where the offending value is passed. Given the function a suspect commit
    modified, THIS tool tells you which file:line in production code actually
    calls it — turning a suspect commit into a confirmed call site.

    Args:
        repo: e.g. "cloudqwest/dynamic_eta_prediction"
        symbol: function or method name (e.g. "fetch_historical_checkcalls")
        max_results: cap on returned call sites

    Returns:
        Dict with `n_callers` and a list of `callers` (file, line, snippet,
        kwargs_passed).
    """
    adapter = get_graphify_adapter()
    try:
        callers = adapter.find_callers(repo=repo, symbol=symbol, max_results=max_results)
        return {
            "repo": repo,
            "symbol": symbol,
            "n_callers": len(callers),
            "callers": callers,
        }
    finally:
        adapter.close()


def find_constant_assignment_sites(repo: str, symbol: str, max_results: int = 10) -> dict:
    """Find lines that ASSIGN a value to `symbol` (constant defs and kwargs).

    This is the "show me where this gets set to that value" query. Catches
    BOTH module-level constants (`SOME_PARAM = <value>`) AND kwarg assignments
    (`some_fn(entry, some_param=<value>)`).

    Localizes the code line behind whatever attribute the anomaly named — pins a
    suspect commit to an exact assignment site.

    Args:
        repo: owner/repo of the service under investigation
        symbol: the attribute/constant name from the anomaly (case-insensitive)
        max_results: cap

    Returns:
        Dict with `n_sites` and a list of `assignments` (file, line, kind,
        snippet, value, commit, commit_author).
    """
    adapter = get_graphify_adapter()
    try:
        sites = adapter.find_constant_assignments(
            repo=repo, symbol=symbol, max_results=max_results
        )
        return {
            "repo": repo,
            "symbol": symbol,
            "n_sites": len(sites),
            "assignments": sites,
        }
    finally:
        adapter.close()


def find_symbol_definition(repo: str, symbol: str) -> dict:
    """Where is `symbol` defined? Returns file/line/snippet for the definition."""
    adapter = get_graphify_adapter()
    try:
        return adapter.find_definition(repo=repo, symbol=symbol)
    finally:
        adapter.close()
