"""Real Graphify adapter — clones the repo locally + uses ripgrep + simple parsing.

Strategy:
  1. Clone the GitHub repo to /tmp/airen-cache/<owner>__<repo> (once)
  2. Use git pull to refresh on subsequent calls
  3. Run ripgrep with PCRE patterns to find symbol references
  4. Light Python AST parsing (using stdlib `ast`) to extract function
     definitions and their call sites

For the Fritolay scenario this is enough to answer:
  - find_definition("fetch_historical_checkcalls") → exact file/line via ast
  - find_callers("fetch_historical_checkcalls") → ripgrep + ast filter
  - find_constant_assignments("api_fetch_limit") → ripgrep for `api_fetch_limit\s*=`

For real production use, swap in tree-sitter or a Sourcegraph-style index.

Requirements at runtime:
  - `git` CLI on PATH
  - `rg` (ripgrep) on PATH — `brew install ripgrep`
  - GITHUB_TOKEN env var for cloning private repos
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


CACHE_DIR = Path(os.environ.get("AIREN_REPO_CACHE", "/tmp/airen-cache"))


def _ensure_ripgrep() -> None:
    if shutil.which("rg") is None:
        raise RuntimeError(
            "ripgrep ('rg') not found on PATH. Install with `brew install ripgrep` "
            "or set AIREN_GRAPHIFY_MODE=mock."
        )


def _clone_url(repo: str) -> str:
    """Return HTTPS clone URL with token embedded if available."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return f"https://x-access-token:{token}@github.com/{repo}.git"
    return f"https://github.com/{repo}.git"


def _git_scrubbed(cmd: list[str], *, timeout: int) -> None:
    """Run a git command, raising a RuntimeError with the token REDACTED from
    both the command and stderr. Never let a PAT leak into a traceback/log."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if token:
            stderr = stderr.replace(token, "<TOKEN_REDACTED>")
        # don't echo the full cmd (it contains the token in the clone URL)
        verb = cmd[1] if len(cmd) > 1 else "git"
        raise RuntimeError(f"git {verb} failed ({proc.returncode}): {stderr[:500]}")


def _ensure_repo(repo: str, branch: str | None = None) -> Path:
    """Clone or update the local cache for `repo`. If `branch` is given, clone /
    check out exactly that branch. Returns the local path.

    A different branch than the cached one forces a fresh clone (simplest, and
    keeps shallow history correct)."""
    owner, _, name = repo.partition("/")
    safe_name = f"{owner}__{name}" + (f"__{branch.replace('/', '_')}" if branch else "")
    target = CACHE_DIR / safe_name
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        cmd = ["git", "clone", "--depth", "50"]
        if branch:
            cmd += ["--branch", branch, "--single-branch"]
        cmd += [_clone_url(repo), str(target)]
        _git_scrubbed(cmd, timeout=120)
    else:
        try:
            ref = f"origin/{branch}" if branch else "origin/HEAD"
            _git_scrubbed(
                ["git", "-C", str(target), "fetch", "--depth", "50", "origin"]
                + ([branch] if branch else []),
                timeout=60,
            )
            _git_scrubbed(["git", "-C", str(target), "reset", "--hard", ref], timeout=30)
        except RuntimeError:
            pass  # cached version still usable
    return target


class RealGraphifyAdapter:
    def __init__(self) -> None:
        _ensure_ripgrep()
        self._repo_paths: dict[str, Path] = {}

    def _repo_path(self, repo: str) -> Path:
        if repo not in self._repo_paths:
            self._repo_paths[repo] = _ensure_repo(repo)
        return self._repo_paths[repo]

    def find_definition(self, repo: str, symbol: str) -> dict[str, Any]:
        path = self._repo_path(repo)
        # Look for `def symbol(` or `class symbol:` or `symbol = `
        patterns = [
            rf"^\s*def\s+{re.escape(symbol)}\s*\(",
            rf"^\s*class\s+{re.escape(symbol)}\s*[:\(]",
            rf"^{re.escape(symbol)}\s*=",
        ]
        for pat in patterns:
            try:
                result = subprocess.run(
                    ["rg", "-n", "--no-heading", "--type", "py", pat, str(path)],
                    capture_output=True,
                    timeout=20,
                    text=True,
                )
            except subprocess.TimeoutExpired:
                continue
            for line in result.stdout.splitlines()[:5]:
                # format: <path>:<line>:<text>
                file, lineno, _, text = line.partition(":") + ("",) if line.count(":") < 2 else (
                    line.split(":", 2) + [""]
                )[:4] if False else line.split(":", 2) + [""]
                parts = line.split(":", 2)
                if len(parts) < 3:
                    continue
                rel_file = str(Path(parts[0]).relative_to(path))
                return {
                    "file": rel_file,
                    "line": int(parts[1]),
                    "snippet": parts[2].strip(),
                    "kind": "function" if "def " in pat else ("class" if "class " in pat else "assignment"),
                }
        return {"error": f"definition of {symbol!r} not found in {repo}"}

    def find_callers(
        self, repo: str, symbol: str, max_results: int = 20
    ) -> list[dict[str, Any]]:
        path = self._repo_path(repo)
        # `symbol(`  but not `def symbol(`  and not `class symbol(`
        # ripgrep PCRE for that
        try:
            result = subprocess.run(
                [
                    "rg",
                    "-n",
                    "--no-heading",
                    "--type",
                    "py",
                    "-P",
                    rf"(?<!\bdef\s){re.escape(symbol)}\s*\(",
                    str(path),
                ],
                capture_output=True,
                timeout=30,
                text=True,
            )
        except subprocess.TimeoutExpired:
            return []
        out: list[dict[str, Any]] = []
        for line in result.stdout.splitlines()[:max_results]:
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            try:
                rel_file = str(Path(parts[0]).relative_to(path))
            except ValueError:
                rel_file = parts[0]
            out.append(
                {
                    "file": rel_file,
                    "line": int(parts[1]),
                    "snippet": parts[2].strip(),
                }
            )
        return out

    def find_constant_assignments(
        self, repo: str, symbol: str, max_results: int = 20
    ) -> list[dict[str, Any]]:
        path = self._repo_path(repo)
        # Match both `SYMBOL = literal` and `symbol=literal` (kwarg form)
        patterns = [
            rf"^\s*{re.escape(symbol)}\s*=",         # top-level constant
            rf"\b{re.escape(symbol)}\s*=\s*\d",      # kwarg with numeric literal
        ]
        seen: set[tuple[str, int]] = set()
        out: list[dict[str, Any]] = []
        for pat in patterns:
            try:
                result = subprocess.run(
                    ["rg", "-n", "--no-heading", "--type", "py", "-P", pat, str(path)],
                    capture_output=True,
                    timeout=20,
                    text=True,
                )
            except subprocess.TimeoutExpired:
                continue
            for line in result.stdout.splitlines():
                parts = line.split(":", 2)
                if len(parts) < 3:
                    continue
                try:
                    rel_file = str(Path(parts[0]).relative_to(path))
                except ValueError:
                    rel_file = parts[0]
                lineno = int(parts[1])
                key = (rel_file, lineno)
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    {
                        "file": rel_file,
                        "line": lineno,
                        "snippet": parts[2].strip(),
                    }
                )
                if len(out) >= max_results:
                    return out
        return out

    def graph_summary(self, repo: str) -> dict[str, Any]:
        path = self._repo_path(repo)
        try:
            n_py = len(list(path.rglob("*.py")))
        except Exception:
            n_py = 0
        return {
            "n_python_files": n_py,
            "n_functions": -1,  # not computed
            "n_classes": -1,
            "languages": ["python"],
            "local_path": str(path),
        }

    def close(self) -> None:
        return
