"""Phoenix MCP toolset — wires `@arizeai/phoenix-mcp` into ADK agents.

REQUIRED by the Arize hackathon track:
  > Configure the Phoenix MCP server in your agent so it can introspect
  > its own operational data at runtime.

Implementation:
  - We use ADK's MCPToolset with a stdio connection
  - That spawns `npx -y @arizeai/phoenix-mcp@latest --baseUrl ...`
  - The MCP server talks to Phoenix's HTTP API on our behalf
  - The agent gets MCP tools (search_spans, list_projects, get_dataset, etc.)
    auto-exposed as ADK FunctionTools

Local Phoenix (laptop) → no API key needed.
Phoenix Cloud (hackathon hosted) → set PHOENIX_API_KEY (px_live_...).

Toggle via env var:
    AIREN_USE_PHOENIX_MCP=1   (default — wires it in)
    AIREN_USE_PHOENIX_MCP=0   (skip, e.g. when offline / no node)
"""

from __future__ import annotations

import os
from typing import Any


def is_phoenix_mcp_enabled() -> bool:
    return os.environ.get("AIREN_USE_PHOENIX_MCP", "1").strip().lower() not in {"0", "false", "no"}


def build_phoenix_mcp_toolset() -> Any | None:
    """Return an MCPToolset configured for Phoenix MCP. None on import failure.

    The returned toolset is meant to be appended to an Agent's `tools=[...]`.
    """
    if not is_phoenix_mcp_enabled():
        return None
    try:
        from google.adk.tools.mcp_tool import MCPToolset, StdioConnectionParams
        from mcp import StdioServerParameters
    except ImportError:
        return None

    base_url = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006").strip()
    api_key = os.environ.get("PHOENIX_API_KEY", "").strip()

    args = ["-y", "@arizeai/phoenix-mcp@latest", "--baseUrl", base_url]
    if api_key:
        args.extend(["--apiKey", api_key])

    server_params = StdioServerParameters(
        command="npx",
        args=args,
        env=None,  # inherit
    )
    conn = StdioConnectionParams(
        server_params=server_params,
        timeout=30.0,  # npx may need to download the package on first run
    )
    return MCPToolset(connection_params=conn)
