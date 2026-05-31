"""Factory + facade for the Graphify adapter.

Default mode is `mock` so laptop dev works without git+ripgrep in the
critical path. Set AIREN_GRAPHIFY_MODE=real on the FK VM to enable
local repo cloning + symbol search.
"""

from __future__ import annotations

import os
from typing import Optional

from airen.adapters.graphify_base import GraphifyAdapter


def get_graphify_adapter(mode: Optional[str] = None) -> GraphifyAdapter:
    resolved = (mode or os.environ.get("AIREN_GRAPHIFY_MODE", "mock")).strip().lower()
    if resolved == "real":
        from airen.adapters.graphify_real import RealGraphifyAdapter

        return RealGraphifyAdapter()
    if resolved == "mock":
        from airen.adapters.graphify_mock import MockGraphifyAdapter

        return MockGraphifyAdapter()
    raise ValueError(f"Unknown AIREN_GRAPHIFY_MODE: {mode!r}. Expected 'mock' or 'real'.")
