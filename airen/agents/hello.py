"""Airen hello agent — Day 2 smoke test.

Proves the full chain works:
    user message → ADK agent → Gemini → FunctionTool → Phoenix query → response

Every step gets a span in Phoenix automatically (via auto_instrument=True
in airen.instrumentation). Sentinel/Investigator/RCA agents will follow the
same pattern, just with richer tool sets.
"""

from __future__ import annotations

import os

from google.adk.agents import Agent
from google.adk.tools import FunctionTool
from phoenix.client import Client

from airen.llm_factory import get_model


def count_phoenix_spans(project_name: str) -> dict:
    """Count spans in a Phoenix project.

    Use this when the user asks how many predictions / inferences / spans
    a given ML model project has produced. Returns the total span count
    and the project name queried.

    Args:
        project_name: the Phoenix project identifier, e.g. "tl-eta-prediction".

    Returns:
        A dict with keys: project_name, span_count, status.
    """
    base_url = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")
    client = Client(base_url=base_url)
    try:
        spans = client.spans.get_spans_dataframe(
            project_identifier=project_name, limit=10000
        )
        return {
            "project_name": project_name,
            "span_count": int(len(spans)),
            "status": "ok",
        }
    except Exception as e:
        return {
            "project_name": project_name,
            "span_count": 0,
            "status": f"error: {type(e).__name__}: {e}",
        }


root_agent = Agent(
    model=get_model(),
    name="airen_hello",
    description="Airen — Autonomous ML Reliability Engineer (hello-world agent).",
    instruction=(
        "You are Airen, an Autonomous ML Reliability Engineer. "
        "You help engineers understand the health of their ML models by querying "
        "Arize Phoenix observability data.\n\n"
        "When the user asks about predictions, spans, traces, or model activity, "
        "use the count_phoenix_spans tool. The current project of interest is "
        "'tl-eta-prediction' (FourKites' truckload ETA LSTM).\n\n"
        "Always answer concisely and reference exact numbers from the tool output."
    ),
    tools=[FunctionTool(func=count_phoenix_spans)],
)
