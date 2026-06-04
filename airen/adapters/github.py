"""GitHub adapter — low-level repo queries.

Higher-level reasoning (which commit caused which anomaly) lives in
airen/tools/investigation.py. This file is the "boring SDK call" layer:
list commits, search code, get a diff. Returns plain JSON-serialisable dicts.

Authentication: reads GITHUB_TOKEN from env (already in .env, gitignored).
We never log, print, or include the token in returned data.

Why FK-real-first: this adapter talks to the real cloudqwest org repos.
When we swap to GCP-clean for hackathon submission, we either point at a
demo repo (cloudqwest/dynamic_eta_demo) or replace the adapter with a
mock. The agent code never sees the difference — same interface.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from github import Auth, Github, GithubException


def _client() -> Github:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN is not set in environment. "
            "Add it to .env (gitignored). Never paste tokens elsewhere."
        )
    return Github(auth=Auth.Token(token))


# ───────────────────────────────────────────────────────────────────────
#  Public adapter functions
# ───────────────────────────────────────────────────────────────────────
def list_recent_commits(
    repo_full_name: str,
    days_back: int = 7,
    max_commits: int = 50,
    branch: str | None = None,
) -> list[dict]:
    """List commits in `repo_full_name` from the last `days_back` days.

    Args:
        repo_full_name: e.g. "cloudqwest/dynamic_eta_prediction".
        days_back: Lookback window in days. Default 7.
        max_commits: Hard cap on number returned. Default 50.
        branch: Branch to read (None = repo default branch).

    Returns:
        List of dicts with sha, author, date, message, files_changed.
    """
    repo = _client().get_repo(repo_full_name)
    since = datetime.now(timezone.utc) - timedelta(days=days_back)
    commits = repo.get_commits(since=since, sha=branch) if branch else repo.get_commits(since=since)

    out: list[dict] = []
    for c in commits[:max_commits]:
        out.append(
            {
                "sha": c.sha,
                "short_sha": c.sha[:8],
                "author": (c.author.login if c.author else (c.commit.author.name if c.commit.author else "unknown")),
                "date": c.commit.author.date.isoformat() if c.commit.author else None,
                "message": (c.commit.message or "").splitlines()[0] if c.commit.message else "",
                "files_changed": [f.filename for f in c.files] if c.files else [],
                "url": c.html_url,
            }
        )
    return out


def search_code(
    repo_full_name: str,
    pattern: str,
    max_results: int = 20,
) -> list[dict]:
    """Search code in `repo_full_name` for the literal `pattern`.

    Uses GitHub's code search API. Returns file paths + line snippets where
    the pattern appears. Useful for "find where 'api_fetch_limit' is used".

    Args:
        repo_full_name: e.g. "cloudqwest/dynamic_eta_prediction".
        pattern: The literal string to grep for, e.g. "api_fetch_limit".
        max_results: Max files to return. Default 20.

    Returns:
        List of dicts with path, html_url, repository. Empty list when no
        matches (or when GitHub returns 0 results — PaginatedList's slice
        operation raises IndexError in that case, which we catch defensively).
    """
    query = f'"{pattern}" repo:{repo_full_name}'
    try:
        results = _client().search_code(query=query)
    except GithubException as e:
        return [{"error": f"GitHub search failed: {e.data.get('message', e)}"}]

    # PyGithub's PaginatedList raises IndexError when totalCount==0 and you
    # try to slice. Check first; safer than iterating blindly.
    try:
        if results.totalCount == 0:
            return []
    except Exception:
        pass  # if totalCount isn't reliable, fall through and try iterating

    out: list[dict] = []
    try:
        for i, r in enumerate(results):
            if i >= max_results:
                break
            out.append(
                {
                    "path": r.path,
                    "name": r.name,
                    "url": r.html_url,
                    "repository": r.repository.full_name,
                }
            )
    except IndexError:
        # PaginatedList edge case — empty result yielded by enumerate. Treat as empty.
        return out
    except GithubException as e:
        # Search rate limit (10 req/min unauthenticated, 30/min authenticated)
        return out if out else [{"error": f"GitHub search failed mid-iter: {e}"}]
    return out


def get_commit_details(repo_full_name: str, sha: str) -> dict:
    """Fetch full details for a single commit: message, diff, files."""
    repo = _client().get_repo(repo_full_name)
    c = repo.get_commit(sha)

    files = []
    for f in c.files:
        files.append(
            {
                "filename": f.filename,
                "status": f.status,  # added / modified / removed
                "additions": f.additions,
                "deletions": f.deletions,
                # `patch` may be None for binary or very large files
                "patch_excerpt": (f.patch or "")[:2000],
            }
        )

    return {
        "sha": c.sha,
        "short_sha": c.sha[:8],
        "author": (c.author.login if c.author else (c.commit.author.name if c.commit.author else "unknown")),
        "date": c.commit.author.date.isoformat() if c.commit.author else None,
        "message": c.commit.message or "",
        "html_url": c.html_url,
        "files": files,
    }


def get_pr_for_commit(repo_full_name: str, sha: str) -> dict | None:
    """If the commit was merged via a PR, return PR metadata. Else None."""
    repo = _client().get_repo(repo_full_name)
    c = repo.get_commit(sha)
    pulls = list(c.get_pulls())
    if not pulls:
        return None
    pr = pulls[0]
    return {
        "number": pr.number,
        "title": pr.title,
        "url": pr.html_url,
        "author": pr.user.login if pr.user else None,
        "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
        "body_excerpt": (pr.body or "")[:500],
    }


def merge_pull_request(repo_full_name: str, pr_number: int, method: str = "squash") -> dict:
    """Merge a PR. method ∈ {squash, merge, rebase}. Outward-facing + irreversible —
    callers must gate this behind explicit approval + config. Returns merge metadata."""
    repo = _client().get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)
    if pr.merged:
        return {"merged": True, "sha": pr.merge_commit_sha, "already": True}
    if not pr.mergeable:
        # mergeable can be None while GitHub computes it; surface clearly.
        return {"merged": False, "error": f"PR #{pr_number} is not mergeable (conflicts or checks pending)."}
    result = pr.merge(merge_method=method)
    return {
        "merged": bool(result.merged),
        "sha": result.sha,
        "message": result.message,
    }


def is_pr_merged(repo_full_name: str, pr_number: int) -> bool:
    """True if the PR has been merged (e.g. a human merged it in GitHub)."""
    try:
        return bool(_client().get_repo(repo_full_name).get_pull(pr_number).merged)
    except GithubException:
        return False


def get_blame_for_line(repo_full_name: str, file_path: str, line_number: int) -> dict | None:
    """Find the commit that last touched a specific line. Best-effort.

    PyGithub doesn't expose git-blame directly, so we use the GraphQL API
    equivalent only if available. Falls back to listing commits that touched
    the file.
    """
    repo = _client().get_repo(repo_full_name)
    try:
        commits = list(repo.get_commits(path=file_path)[:1])
    except GithubException:
        return None
    if not commits:
        return None
    c = commits[0]
    return {
        "sha": c.sha,
        "short_sha": c.sha[:8],
        "author": c.author.login if c.author else None,
        "date": c.commit.author.date.isoformat() if c.commit.author else None,
        "message": (c.commit.message or "").splitlines()[0],
        "url": c.html_url,
        "note": "latest commit touching this file; for exact line-blame, fetch full git history",
    }
