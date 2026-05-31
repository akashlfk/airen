# Airen — multi-stage build for a slim production image.
#
# Builds:
#   - Python 3.11 slim base
#   - Node.js 22 (for @arizeai/phoenix-mcp via npx, required by Arize hackathon)
#   - All Python deps from requirements.txt
#   - The airen package
#
# Run:
#   docker build -t airen .
#   docker run -p 8000:8000 --env-file .env airen
#
# Cloud Run:
#   gcloud builds submit --tag gcr.io/PROJECT/airen
#   gcloud run deploy airen --image gcr.io/PROJECT/airen --port 8000

# ──────────────────────────────────────────────────────────────────
# Stage 1 — builder
# ──────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# System deps for psycopg2 + confluent-kafka + nodejs
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
        gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --user -r requirements.txt

# ──────────────────────────────────────────────────────────────────
# Stage 2 — runtime
# ──────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/root/.local/bin:$PATH" \
    PORT=8000

# Runtime deps (smaller than builder)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Pre-warm the Phoenix MCP package so first agent run isn't slow
RUN npx -y @arizeai/phoenix-mcp@latest --help 2>&1 | head -5 || true

WORKDIR /app

# Copy Python deps from builder
COPY --from=builder /root/.local /root/.local

# Copy the application
COPY airen ./airen
COPY services ./services
COPY pyproject.toml ./
COPY .gemini ./.gemini

# Logs directory (writable in the container; ephemeral on Cloud Run)
RUN mkdir -p /app/logs

EXPOSE 8000

# Cloud Run injects PORT; default to 8000 locally.
CMD ["sh", "-c", "python -m uvicorn airen.web.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
