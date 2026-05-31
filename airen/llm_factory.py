"""LLM model factory — returns the right model for each agent based on env config.

Backend selection via `AIREN_LLM_BACKEND` env var:

  gemini   (default)  ADK uses google-genai SDK natively. Model string is "gemini-2.5-flash"
                       (or whatever GEMINI_MODEL says). REQUIRED for hackathon submission.

  azure    (dev only) ADK uses LiteLLM to route to Azure OpenAI. Set AZURE_* env vars.
                       NOT eligible for hackathon submission — see memory/azure_dev_only.md.

  mock     bypassed entirely by the pipeline runner (see airen/mocks.py).
           This factory still returns a sane default so the Agent object is constructible.

The factory hides the backend switch from agent code. Each agent declares
`model=get_model()` instead of hard-coding a model string.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any


def _gemini_model_string() -> str:
    return os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def _azure_litellm() -> Any:
    """Wrap an Azure OpenAI deployment in ADK's LiteLlm so the Agent abstraction works."""
    try:
        from google.adk.models.lite_llm import LiteLlm
    except ImportError as e:
        raise RuntimeError(
            "LiteLlm not available. Install it with:\n"
            "    conda run -n mlre pip install 'litellm>=1.50'"
        ) from e

    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    # LiteLLM picks up AZURE_API_KEY / AZURE_API_BASE / AZURE_API_VERSION from env.
    # Verify they're set so we fail loudly instead of mid-LLM-call.
    required = ("AZURE_API_KEY", "AZURE_API_BASE", "AZURE_API_VERSION")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            f"AIREN_LLM_BACKEND=azure requires env vars: {', '.join(missing)}. "
            "Add them to .env (and never commit the key)."
        )
    return LiteLlm(model=f"azure/{deployment}")


def get_model() -> Any:
    """Return whichever model object ADK's Agent() should consume."""
    backend = os.environ.get("AIREN_LLM_BACKEND", "gemini").strip().lower()
    if backend == "azure":
        return _azure_litellm()
    # Default + "mock" path returns the Gemini string (mock runner short-circuits
    # before any LLM call is made, so this never actually executes in mock mode).
    return _gemini_model_string()


def get_model_for(agent_name: str) -> Any:
    """Like get_model() but allows per-agent overrides via env var.

    e.g., set INVESTIGATOR_MODEL=gemini-2.5-pro to upgrade just Investigator.
    Only applies when backend is gemini.
    """
    backend = os.environ.get("AIREN_LLM_BACKEND", "gemini").strip().lower()
    if backend == "azure":
        return _azure_litellm()
    override = os.environ.get(f"{agent_name.upper()}_MODEL")
    return override if override else _gemini_model_string()


# ───────────────────────────────────────────────────────────────────────
#  Structured output portability
#  Gemini natively supports output_schema. Azure OpenAI deployments
#  inconsistently support response_format=json_schema (FK's qa-ai-poc
#  rejects it on every model snapshot we tried). For Azure we fall back
#  to prompt-based JSON: append the schema to the prompt, parse output
#  with extract_json() before Pydantic validation.
# ───────────────────────────────────────────────────────────────────────
def supports_strict_output_schema() -> bool:
    """True when the active backend supports API-level strict JSON schema enforcement."""
    backend = os.environ.get("AIREN_LLM_BACKEND", "gemini").strip().lower()
    return backend != "azure"


def schema_appendix(schema_cls: Any) -> str:
    """Render a Pydantic schema as a prompt-tail telling the LLM the exact JSON to emit."""
    schema_json = json.dumps(schema_cls.model_json_schema(), indent=2)
    return (
        "\n\n---\n"
        "IMPORTANT — RETURN ONLY VALID JSON matching this exact schema. No prose, "
        "no preamble, no code fence markers, no trailing text. Just the JSON object.\n\n"
        f"Schema:\n```json\n{schema_json}\n```"
    )


def extract_json(text: str) -> str:
    """Pull the first JSON object out of LLM text output.

    LLMs sometimes wrap their JSON in code fences or add a sentence of preamble
    even when instructed not to. This finds the first {...} block and returns it.
    """
    if not text:
        return text
    # Try fenced code block first
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    # Try raw object — match outermost braces
    raw = re.search(r"\{.*\}", text, re.DOTALL)
    if raw:
        return raw.group(0)
    return text
