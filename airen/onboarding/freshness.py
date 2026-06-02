"""Freshness — poll-on-cycle re-graphify.

The chosen hook model (no webhook server, demo-safe): every orchestrator cycle,
cheaply check whether the monitored repo's HEAD has moved since we last
graphified it. If it has, re-graphify and refresh the manifest. This keeps
Airen's understanding current at exactly the moment it matters — right before
an investigation — without hosting anything GitHub can reach.

Fully best-effort: any failure (offline, no git, mock mode, private-repo auth)
is swallowed and the cycle proceeds with whatever manifest exists.
"""

from __future__ import annotations

from airen.config import AirenServiceConfig
from airen.onboarding.manifest import RepoManifest
from airen.onboarding.scan import graphify, remote_head


def maybe_refresh_manifest(config: AirenServiceConfig | None) -> str | None:
    """If the repo HEAD moved since last graphify, re-graphify. Returns a status
    line for the orchestrator log, or None if nothing to report."""
    if config is None or config.github is None or not config.github.repo:
        return None

    import os

    # Skip the network in mock mode — demos stay deterministic and offline.
    if os.environ.get("AIREN_LLM_MODE", "real").strip().lower() == "mock":
        return None

    repo = config.github.repo
    try:
        existing = RepoManifest.load(repo, is_remote=True)
        head = remote_head(repo)
        if head is None:
            return None  # couldn't reach remote; keep existing understanding
        if existing is not None and existing.commit_sha == head:
            return None  # already fresh
        manifest = graphify(repo)  # clones/pulls + re-scans + saves
        action = "graphified" if existing is None else "re-graphified (repo moved)"
        sha = (manifest.commit_sha or "")[:12]
        return f"{action} {repo} @ {sha} — {len(manifest.models)} model(s), {len(manifest.apis)} API(s)"
    except Exception:
        return None
