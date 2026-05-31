"""List Azure OpenAI deployments on the configured endpoint.

Usage:
    # After setting AZURE_API_KEY, AZURE_API_BASE, AZURE_API_VERSION in .env:
    python scripts/list_azure_deployments.py

Prints each deployment name. Pick the one we want and set:
    AZURE_OPENAI_DEPLOYMENT=<that-name>

Note: this uses Azure's management API, which requires the same key/endpoint
but a different URL pattern than the chat completions endpoint.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import AzureOpenAI

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)


def main() -> None:
    key = os.environ.get("AZURE_API_KEY")
    base = os.environ.get("AZURE_API_BASE")
    version = os.environ.get("AZURE_API_VERSION", "2024-12-01-preview")

    missing = [
        n for n, v in [("AZURE_API_KEY", key), ("AZURE_API_BASE", base)] if not v
    ]
    if missing:
        print(f"Missing env vars: {', '.join(missing)} — add to .env first.")
        sys.exit(1)

    client = AzureOpenAI(api_version=version, azure_endpoint=base, api_key=key)

    print(f"Listing models available on {base} ...\n")
    try:
        models = client.models.list()
    except Exception as e:
        print(f"❌ Failed to list models: {type(e).__name__}: {e}")
        print("\nIf this returns 401, your key is invalid for this endpoint.")
        print("If this returns 404, deployments may need to be discovered via the")
        print("Azure portal (this REST endpoint is not exposed on all resource SKUs).")
        sys.exit(1)

    print(f"{'name':<40} {'id':<40}")
    print("-" * 80)
    for m in models:
        # AzureOpenAI returns OpenAI Model objects — `.id` is the deployment name
        print(f"{getattr(m, 'id', '?'):<40} {getattr(m, 'object', '?')}")
    print(f"\nPick one of the names above and set in .env:")
    print(f"    AZURE_OPENAI_DEPLOYMENT=<that-name>")


if __name__ == "__main__":
    main()
