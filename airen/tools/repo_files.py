"""Repo file access for agents — list + read files in any configured repo.

This is what gives an ADK agent *eyes* into the code it's reasoning about: to diff
serving vs training preprocessing, the agent must actually read both repos. It
reads from the local graphify clone cache (same cache Investigator's code-graph
tools use), so a repo is cloned once and reused. Read-only, path-escape guarded.

Works for ANY repo string — the service's serving repo (`github.repo`) and its
separate training repo (`github.training_repo`).
"""

from __future__ import annotations

from pathlib import Path

_MAX_BYTES = 60_000
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", "images", "dist", "build"}


def _repo_root(repo: str, branch: str | None = None) -> Path:
    # Reuse graphify's clone-or-update cache so we don't re-clone per call.
    from airen.adapters.graphify_real import _ensure_repo

    # resolve() so /tmp vs /private/tmp (macOS symlink) doesn't break relative_to
    return _ensure_repo(repo, branch).resolve()


def _safe(root: Path, rel: str) -> Path | None:
    target = (root / rel).resolve()
    return target if str(target).startswith(str(root.resolve())) else None


def list_repo_tree(repo: str, subdir: str = "", branch: str | None = None,
                   pattern: str = "*.py", max_entries: int = 400) -> dict:
    """List files (default *.py) under `subdir` of `repo`. Use this to discover a
    repo's structure before reading specific files. `repo` is 'owner/repo' or a URL."""
    try:
        root = _repo_root(repo, branch)
    except Exception as e:  # noqa: BLE001
        return {"repo": repo, "error": f"could not access repo: {type(e).__name__}: {e}"}
    base = _safe(root, subdir)
    if base is None:
        return {"repo": repo, "error": "subdir escapes repo root"}
    if not base.exists():
        return {"repo": repo, "subdir": subdir or ".", "error": "subdir not found"}
    files: list[str] = []
    for p in sorted(base.rglob(pattern)):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.is_file():
            files.append(str(p.relative_to(root)))
            if len(files) >= max_entries:
                break
    return {"repo": repo, "subdir": subdir or ".", "n_files": len(files), "files": files}


def read_repo_file(repo: str, path: str, branch: str | None = None,
                   max_bytes: int = _MAX_BYTES) -> dict:
    """Read a file from `repo` (clone-cached). Returns up to max_bytes of content.
    Use after list_repo_tree to inspect specific preprocessing/model files."""
    try:
        root = _repo_root(repo, branch)
    except Exception as e:  # noqa: BLE001
        return {"repo": repo, "path": path, "error": f"could not access repo: {type(e).__name__}: {e}"}
    target = _safe(root, path)
    if target is None:
        return {"repo": repo, "path": path, "error": "path escapes repo root"}
    if not target.is_file():
        return {"repo": repo, "path": path, "error": "file not found"}
    try:
        data = target.read_text(errors="ignore")
    except Exception as e:  # noqa: BLE001
        return {"repo": repo, "path": path, "error": f"read failed: {type(e).__name__}: {e}"}
    return {
        "repo": repo, "path": path,
        "truncated": len(data) > max_bytes,
        "n_bytes": len(data),
        "content": data[:max_bytes],
    }


def grep_repo(repo: str, pattern: str, branch: str | None = None,
              subdir: str = "", max_hits: int = 60) -> dict:
    """Search the repo for a regex (file:line:text). Use to locate where a feature,
    transform, scaler, or model is defined/used before reading the file."""
    import re

    try:
        root = _repo_root(repo, branch)
    except Exception as e:  # noqa: BLE001
        return {"repo": repo, "error": f"could not access repo: {type(e).__name__}: {e}"}
    base = _safe(root, subdir)
    if base is None:
        return {"repo": repo, "error": "subdir escapes repo root"}
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return {"repo": repo, "error": f"bad regex: {e}"}
    hits: list[str] = []
    for p in sorted(base.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        try:
            for i, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{p.relative_to(root)}:{i}: {line.strip()[:200]}")
                    if len(hits) >= max_hits:
                        return {"repo": repo, "pattern": pattern, "n_hits": len(hits), "hits": hits}
        except Exception:
            continue
    return {"repo": repo, "pattern": pattern, "n_hits": len(hits), "hits": hits}
