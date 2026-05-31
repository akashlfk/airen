"""Phoenix tracing setup for Airen.

Call setup_tracing() ONCE, at startup, BEFORE importing any ADK module.
That ordering matters because openinference-instrumentation-google-adk
monkey-patches ADK at instrument-time; if ADK is already imported and
in use, the patches won't apply to already-cached references.

Env vars:
    PHOENIX_PROJECT_NAME       — default: airen-dev
    PHOENIX_COLLECTOR_ENDPOINT — default: http://localhost:6006 (local Phoenix)
    PHOENIX_API_KEY            — only needed for Phoenix Cloud (px_live_...)
"""

from __future__ import annotations

import os
from typing import Any

from phoenix.otel import register

_provider: Any = None


def setup_tracing() -> Any:
    """Returns the configured tracer provider. Idempotent — safe to call multiple times."""
    global _provider
    if _provider is not None:
        return _provider

    _provider = register(
        project_name=os.environ.get("PHOENIX_PROJECT_NAME", "airen-dev"),
        # batch=False uses SimpleSpanProcessor — spans flush immediately.
        # For dev this gives instant feedback; for prod we'll switch to batch=True.
        batch=False,
        # The magic: discovers installed openinference-instrumentation-* packages
        # and wraps ADK, google-genai, etc. so every agent/tool/LLM call is a span.
        auto_instrument=True,
        verbose=False,
    )
    return _provider
