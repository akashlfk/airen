"""GitHub MCP toolset — wires `@modelcontextprotocol/server-github` into ADK.

The hackathon-clean track values MCP integration. Investigator already has
deterministic tools backed by PyGithub (`airen.adapters.github`) — those
stay as the primary path because they're faster and don't need npx. The
MCP toolset is layered on top so the LLM can also do open-ended GitHub
queries the Python tools don't cover (search code across orgs, browse
issues, read README, etc.).

Auth: uses GITHUB_PERSONAL_ACCESS_TOKEN if set, else falls back to GITHUB_TOKEN
(which Airen already uses elsewhere). The MCP server runs as a stdio child
process; the token is passed via environment, not on the command line.

Toggle via env:
    AIREN_USE_GITHUB_MCP=1   (default — wires it in if GITHUB_TOKEN is set)
    AIREN_USE_GITHUB_MCP=0   (skip, e.g. when offline / no node)
"""

from __future__ import annotations

import os
from typing import Any


def is_github_mcp_enabled() -> bool:
    """True when MCP is requested AND a GitHub token is available.

    Refuses to enable without a token because the MCP server would crash
    on first call, producing a worse error than just not having the tool.
    """
    flag = os.environ.get("AIREN_USE_GITHUB_MCP", "1").strip().lower() not in {"0", "false", "no"}
    if not flag:
        return False
    return bool(_resolve_token())


def _resolve_token() -> str:
    # MCP server reads GITHUB_PERSONAL_ACCESS_TOKEN; we accept the broader
    # GITHUB_TOKEN as a fallback since the rest of Airen uses that name.
    return (
        os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip()
        or os.environ.get("GITHUB_TOKEN", "").strip()
    )


def build_github_mcp_toolset() -> Any | None:
    """Return an MCPToolset wired to the official GitHub MCP server.

    Returns None when:
      - the toggle is off
      - no GitHub token is available
      - ADK/MCP imports fail (older ADK versions, etc.)
    """
    if not is_github_mcp_enabled():
        return None
    try:
        from google.adk.tools.mcp_tool import MCPToolset, StdioConnectionParams
        from mcp import StdioServerParameters
    except ImportError:
        return None

    token = _resolve_token()
    if not token:
        return None

    # Pass the token via env, NOT argv — argv leaks into ps listings.
    child_env = {
        **os.environ,
        "GITHUB_PERSONAL_ACCESS_TOKEN": token,
    }

    server_params = StdioServerParameters(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env=child_env,
    )
    conn = StdioConnectionParams(
        server_params=server_params,
        timeout=30.0,  # npx may need to fetch the package on first run
    )
    return MCPToolset(connection_params=conn)
