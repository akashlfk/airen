#!/usr/bin/env bash
# Deploy Airen's web UI + orchestrator to Google Cloud Run.
#
# Prereqs:
#   - gcloud installed + authenticated (`gcloud auth login`)
#   - A GCP project with billing enabled
#   - Cloud Run + Cloud Build APIs enabled
#
# Usage:
#   GCP_PROJECT=airen-hackathon-2026 GCP_REGION=us-central1 ./deploy/cloud-run/deploy.sh
#
# Required secrets must be set in your shell (passed as --set-env-vars to Cloud Run):
#   GOOGLE_API_KEY (or GOOGLE_GENAI_USE_VERTEXAI=1 + project for Vertex)
#   GITHUB_TOKEN
#   SLACK_BOT_TOKEN
#   SLACK_CHANNEL
#   PHOENIX_API_KEY (for Phoenix Cloud)
#   PHOENIX_COLLECTOR_ENDPOINT (Phoenix Cloud URL)

set -euo pipefail

PROJECT="${GCP_PROJECT:?Set GCP_PROJECT environment variable}"
REGION="${GCP_REGION:-us-central1}"
SERVICE="${AIREN_SERVICE_NAME:-airen}"
IMAGE="gcr.io/${PROJECT}/${SERVICE}"

echo "→ Building image ${IMAGE} via Cloud Build..."
gcloud builds submit --project="${PROJECT}" --tag="${IMAGE}"

echo "→ Deploying to Cloud Run..."
gcloud run deploy "${SERVICE}" \
    --project="${PROJECT}" \
    --image="${IMAGE}" \
    --region="${REGION}" \
    --platform=managed \
    --allow-unauthenticated \
    --port=8000 \
    --memory=1Gi \
    --cpu=1 \
    --min-instances=0 \
    --max-instances=3 \
    --concurrency=20 \
    --timeout=300 \
    --set-env-vars="GOOGLE_API_KEY=${GOOGLE_API_KEY:-}" \
    --set-env-vars="GITHUB_TOKEN=${GITHUB_TOKEN:-}" \
    --set-env-vars="SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN:-}" \
    --set-env-vars="SLACK_CHANNEL=${SLACK_CHANNEL:-}" \
    --set-env-vars="PHOENIX_API_KEY=${PHOENIX_API_KEY:-}" \
    --set-env-vars="PHOENIX_COLLECTOR_ENDPOINT=${PHOENIX_COLLECTOR_ENDPOINT:-}" \
    --set-env-vars="AIREN_LLM_BACKEND=${AIREN_LLM_BACKEND:-gemini}" \
    --set-env-vars="AIREN_USE_PHOENIX_MCP=1"

URL=$(gcloud run services describe "${SERVICE}" --project="${PROJECT}" --region="${REGION}" --format='value(status.url)')
echo ""
echo "✓ Deployed. Service URL:"
echo "  ${URL}"
echo ""
echo "Quick sanity:"
echo "  curl -s ${URL}/healthz"
echo "  open ${URL}"
