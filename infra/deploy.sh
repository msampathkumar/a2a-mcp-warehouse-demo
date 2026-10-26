#!/usr/bin/env bash
# Deploy the 5 isolated DuckDB data warehouses + 5 MCP servers + live telemetry homepage to Cloud Run.
# Defaults to a2a-practical-demos so it attaches directly to the msampathkumar.com HTTPS Load Balancer (pcb-https).
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-a2a-practical-demos}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-dw-mcp}"
SA_NAME="${SA_NAME:-warehouse-demo-sa}"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
PUBLIC_URL="${PUBLIC_URL:-https://msampathkumar.com/demos/data-warehouse}"
BQ_DATASET="${BQ_DATASET:-dw_telemetry}"
BQ_TABLE="${BQ_TABLE:-mcp_events}"
BQ_TELEMETRY_TABLE="${BQ_TELEMETRY_TABLE:-${PROJECT_ID}.${BQ_DATASET}.${BQ_TABLE}}"

# Generate a random admin token if not provided so /_stats is protected in PUBLIC_DEMO mode
ADMIN_TOKEN="${ADMIN_TOKEN:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')}"

echo "==> 1. Enabling required APIs in ${PROJECT_ID}..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  logging.googleapis.com \
  cloudtrace.googleapis.com \
  bigquery.googleapis.com \
  --project="${PROJECT_ID}" \
  --quiet

echo "==> 2. Ensuring least-privilege service account ${SA_EMAIL} and BigQuery telemetry table..."
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" --quiet >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="MCP Data Warehouses Demo SA" \
    --description="Least-privilege SA for Cloud Run 5 MCP Data Warehouses demo" \
    --project="${PROJECT_ID}" \
    --quiet
fi

for ROLE in roles/bigquery.jobUser roles/bigquery.dataEditor; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${ROLE}" \
    --condition=None \
    --quiet >/dev/null
done

echo "==> 3. Deploying Cloud Run service ${SERVICE_NAME} in ${REGION}..."
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/cloud-run-source-deploy/${SERVICE_NAME}:latest"
if command -v podman >/dev/null 2>&1; then
  echo "    Building & pushing image locally via podman (${IMAGE})..."
  gcloud auth print-access-token | podman login -u oauth2accesstoken --password-stdin "https://${REGION}-docker.pkg.dev"
  podman build -t "${IMAGE}" .
  podman push "${IMAGE}"
  DEPLOY_SOURCE_ARGS=(--image="${IMAGE}")
else
  DEPLOY_SOURCE_ARGS=(--source .)
fi

gcloud run deploy "${SERVICE_NAME}" \
  "${DEPLOY_SOURCE_ARGS[@]}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --allow-unauthenticated \
  --ingress=internal-and-cloud-load-balancing \
  --memory=1Gi \
  --cpu=1 \
  --min-instances=0 \
  --max-instances=5 \
  --concurrency=80 \
  --set-env-vars="LEVEL=5,PUBLIC_DEMO=true,A2A_ENABLED=false,ISOLATED_WAREHOUSES=true,CLOUD_LOGGING=true,PUBLIC_URL=${PUBLIC_URL},ADMIN_TOKEN=${ADMIN_TOKEN},GOOGLE_CLOUD_PROJECT=${PROJECT_ID},BQ_TELEMETRY_TABLE=${BQ_TELEMETRY_TABLE},BQ_LOCATION=${REGION}" \
  --quiet

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format='value(status.url)' \
  --quiet)"

echo ""
echo "============================================================"
echo "Cloud Run Service:  ${SERVICE_NAME} (${SERVICE_URL})"
echo "Public Base URL:    ${PUBLIC_URL}/"
echo "Catalog:            ${PUBLIC_URL}/catalog.json"
echo "Live Metrics JSON:  ${PUBLIC_URL}/metrics.json"
echo "MCP Orders:         ${PUBLIC_URL}/orders/       (orders.duckdb)"
echo "MCP Stores:         ${PUBLIC_URL}/stores/       (stores.duckdb)"
echo "MCP Customers:      ${PUBLIC_URL}/customers/    (customers.duckdb)"
echo "MCP Order Items:    ${PUBLIC_URL}/order_items/  (order_items.duckdb)"
echo "MCP Products:       ${PUBLIC_URL}/products/     (products.duckdb)"
echo "============================================================"
