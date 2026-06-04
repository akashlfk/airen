"""graphify — turn a repo into a RepoManifest.

Reuses the existing Graphify clone/cache infra (airen.adapters.graphify_real)
so we don't maintain two cloners. Adds the repo-comprehension layer on top:
run every deterministic detector and assemble the manifest.

Entry points:
    graphify(repo)                 # remote owner/repo → clone/refresh → manifest
    graphify_local(path)           # an already-checked-out directory → manifest
    remote_head(repo)              # cheap HEAD sha for the freshness poll
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from airen.onboarding import detectors as D
from airen.onboarding.manifest import RepoManifest


# ───────────────────────────────────────────────────────────────────────
#  git helpers
# ───────────────────────────────────────────────────────────────────────
def _local_head(path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15, check=True,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def _local_branch(path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=15, check=True,
        )
        b = out.stdout.strip()
        return None if b in ("", "HEAD") else b
    except Exception:
        return None


def remote_head(repo: str) -> str | None:
    """Cheap network call: the SHA at the remote's default branch. No clone.

    Used by the freshness poller to decide whether a re-graphify is warranted.
    """
    from airen.adapters.graphify_real import _clone_url

    try:
        out = subprocess.run(
            ["git", "ls-remote", _clone_url(repo), "HEAD"],
            capture_output=True, text=True, timeout=20, check=True,
        )
        first = out.stdout.split("\n", 1)[0].split("\t", 1)[0].strip()
        return first or None
    except Exception:
        return None


# ───────────────────────────────────────────────────────────────────────
#  The scan
# ───────────────────────────────────────────────────────────────────────
def _build_manifest(root: Path, *, source: str, is_remote: bool,
                    commit_sha: str | None, branch: str | None) -> RepoManifest:
    deps = D.detect_dependencies(root)
    langs = D.detect_languages(root)
    frameworks = D.detect_frameworks(deps, root)
    apis = D.detect_apis(root)
    models = D.detect_models(root, frameworks)
    serving = D.detect_serving_topology(root, deps, apis)
    observation = D.detect_observation_schema(root, models)
    obs = D.detect_observability(root)
    trk = D.detect_tracking(root)
    feats = D.detect_feature_constants(root)
    app_type = D.classify_application(apis, frameworks, root)

    return RepoManifest(
        source=source, is_remote=is_remote, commit_sha=commit_sha, default_branch=branch,
        languages=langs, frameworks=frameworks, dependencies=deps, application_type=app_type,
        models=models, apis=apis, serving=serving, observation=observation,
        observability=obs, tracking=trk, feature_constants=feats,
    )


def graphify_local(path: str | Path, *, save: bool = True) -> RepoManifest:
    """Build a manifest from an already-checked-out directory."""
    root = Path(path).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"{root} is not a directory")
    manifest = _build_manifest(
        root, source=str(root), is_remote=False,
        commit_sha=_local_head(root), branch=_local_branch(root),
    )
    if save:
        manifest.save()
    return manifest


def graphify(repo: str, *, branch: str | None = None, save: bool = True) -> RepoManifest:
    """Clone/refresh `owner/repo` (optionally a specific branch) via the existing
    Graphify cache, then scan it."""
    from airen.adapters.graphify_real import _ensure_repo

    root = _ensure_repo(repo, branch=branch)  # clones to /tmp/airen-cache, or pulls if present
    manifest = _build_manifest(
        root, source=repo, is_remote=True,
        commit_sha=_local_head(root), branch=_local_branch(root),
    )
    if save:
        manifest.save()
    return manifest
