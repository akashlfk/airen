"""Run the Airen web dashboard.

Usage:
    python -m airen.run_web                 # default: 127.0.0.1:8000
    AIREN_WEB_PORT=8080 python -m airen.run_web

Order of operations matches the agent runners — load .env, start tracing,
then import the FastAPI app (so any agent calls made FROM the web layer are
also traced).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from airen.instrumentation import setup_tracing

setup_tracing()

import uvicorn


def main() -> None:
    host = os.environ.get("AIREN_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("AIREN_WEB_PORT", "8000"))
    print(f"\nAiren UI starting on http://{host}:{port}\n")
    uvicorn.run("airen.web.app:app", host=host, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
