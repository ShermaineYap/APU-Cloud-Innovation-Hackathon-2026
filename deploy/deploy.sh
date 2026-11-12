#!/usr/bin/env bash
# Deploy CampusPulse to Google Cloud: Cloud Run + BigQuery (+ BigQuery ML) + Pub/Sub + Gemini.
#
#   PROJECT_ID=my-project ./deploy/deploy.sh
#   SKIP_ML=1 PROJECT_ID=my-project ./deploy/deploy.sh    # redeploy without retraining BigQuery ML
#
# Sized for the free tier: Cloud Run scales to zero and bills CPU only while answering requests
# (simulated readings catch up on each request instead of a background loop), one instance,
# BigQuery queries prune to recent partitions, Pub/Sub well under 10 GiB a month.
# Gemini: uses GEMINI_API_KEY (AI Studio, free tier) from the environment or .env if present;
# otherwise Gemini on Vertex AI with the service account.
#
# Needs: gcloud (logged in: `gcloud auth login` and `gcloud auth application-default login`),
# Python with requirements.txt installed, and a billing account on the project (the free trial counts).
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID, e.g. PROJECT_ID=campuspulse-apu ./deploy/deploy.sh}"
REGION="${REGION:-asia-southeast1}"
SERVICE="${SERVICE:-campuspulse}"
DATASET="${DATASET:-campuspulse}"
TOPIC="campuspulse-telemetry"
SA="campuspulse-run@${PROJECT_ID}.iam.gserviceaccount.com"

cd "$(dirname "$0")/.."
PY="${PYTHON:-$(command -v python3 || command -v python)}"

# The ingest key is kept in .deploy.env (git-ignored) so redeploys keep working with flashed devices.
touch .deploy.env
# shellcheck disable=SC1091
source .deploy.env
INGEST_KEY="${INGEST_KEY:-$(openssl rand -hex 16)}"
grep -q '^INGEST_KEY=' .deploy.env || echo "INGEST_KEY=$INGEST_KEY" >> .deploy.env

# Gemini key: environment first, then .env.
if [[ -z "${GEMINI_API_KEY:-}" && -f .env ]]; then
  GEMINI_API_KEY="$( (grep -E '^GEMINI_API_KEY=' .env || true) | tail -1 | cut -d= -f2- | tr -d '"'"'"' ')"
fi
GEMINI_MODEL="${CP_GEMINI_MODEL:-gemini-3.5-flash-lite}"

gcloud config set project "$PROJECT_ID" >/dev/null
BILLING="$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingEnabled)' 2>/dev/null || true)"
if [[ "$(echo "$BILLING" | tr "[:upper:]" "[:lower:]")" == "false" ]]; then
  echo "This project has no billing account. Start the free trial (https://console.cloud.google.com/freetrial)"
  echo "and link it to the project under Billing > My projects, then run this again."
  exit 1
fi

echo "1/6 Enabling APIs"
APIS="run.googleapis.com bigquery.googleapis.com pubsub.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com iam.googleapis.com"
[[ -z "${GEMINI_API_KEY:-}" ]] && APIS="$APIS aiplatform.googleapis.com"
# shellcheck disable=SC2086
gcloud services enable $APIS

echo "2/6 Service account"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create campuspulse-run --display-name "CampusPulse Cloud Run"
for role in roles/bigquery.dataEditor roles/bigquery.jobUser roles/pubsub.publisher roles/aiplatform.user; do
  # A new service account can take a minute before IAM accepts it, so retry.
  for i in 1 2 3 4 5 6 7 8; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$SA" --role "$role" \
      --condition=None --quiet >/dev/null 2>&1 && break
    if [[ $i == 8 ]]; then echo "Could not grant $role to $SA"; exit 1; fi
    echo "   waiting for the new service account ($role)..."; sleep 10
  done
done
# Source deploys build as the default compute service account; make sure it may build (harmless if it already can).
PN="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:${PN}-compute@developer.gserviceaccount.com" \
  --role roles/run.builder --condition=None --quiet >/dev/null 2>&1 || true

echo "3/6 BigQuery: dataset, tables, views, 28 days of history, BigQuery ML models (a few minutes)"
gcloud auth application-default print-access-token >/dev/null 2>&1 || gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT="$PROJECT_ID" CP_BACKEND=bigquery CP_BQ_DATASET="$DATASET" CP_BQ_LOCATION="$REGION"
if [[ -n "${SKIP_ML:-}" ]]; then "$PY" scripts/bq_setup.py --seed; else "$PY" scripts/bq_setup.py --seed --ml; fi

echo "4/6 Pub/Sub topic"
gcloud pubsub topics describe "$TOPIC" >/dev/null 2>&1 || gcloud pubsub topics create "$TOPIC"

echo "5/6 Cloud Run (built from the Dockerfile by Cloud Build)"
ENV="CP_BACKEND=bigquery,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,CP_BQ_DATASET=$DATASET,CP_BQ_LOCATION=$REGION"
ENV="$ENV,CP_SIM_MODE=request,CP_SETUP_ON_START=false,CP_INGEST_KEY=$INGEST_KEY"
ENV="$ENV,CP_PUBSUB_TOPIC=projects/$PROJECT_ID/topics/$TOPIC,CP_GEMINI_MODEL=$GEMINI_MODEL"
if [[ -n "${GEMINI_API_KEY:-}" ]]; then
  ENV="$ENV,GEMINI_API_KEY=$GEMINI_API_KEY,CP_GEMINI_VERTEX=false"
  echo "   Gemini: AI Studio key from your environment/.env"
else
  ENV="$ENV,CP_GEMINI_VERTEX=true"
  echo "   Gemini: Vertex AI (no GEMINI_API_KEY found)"
fi
gcloud run deploy "$SERVICE" --source . --region "$REGION" --quiet \
  --service-account "$SA" --allow-unauthenticated \
  --min-instances 0 --max-instances 1 --cpu-throttling --cpu-boost \
  --memory 1Gi --cpu 1 --concurrency 40 --timeout 120 \
  --set-env-vars "$ENV"
URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"

echo "6/6 Pub/Sub push subscription -> $URL/api/pubsub/push"
PUSH="$URL/api/pubsub/push?key=$INGEST_KEY"
if gcloud pubsub subscriptions describe campuspulse-push >/dev/null 2>&1; then
  gcloud pubsub subscriptions update campuspulse-push --push-endpoint "$PUSH" >/dev/null
else
  gcloud pubsub subscriptions create campuspulse-push --topic "$TOPIC" --push-endpoint "$PUSH" \
    --ack-deadline 30 --message-retention-duration 1h >/dev/null
fi

grep -q '^URL=' .deploy.env && sed -i.bak "s#^URL=.*#URL=$URL#" .deploy.env && rm -f .deploy.env.bak \
  || echo "URL=$URL" >> .deploy.env

echo
echo "Deployed: $URL"
echo "Ingest key for devices and the camera (kept in .deploy.env): $INGEST_KEY"
echo "Check everything: $PY scripts/cloud_check.py"
