#!/usr/bin/env bash
# Deploy CampusPulse to Cloud Run with BigQuery, Pub/Sub and Gemini on Vertex AI.
# Usage: PROJECT_ID=my-project ./deploy/deploy.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-asia-southeast1}"
SERVICE="${SERVICE:-campuspulse}"
DATASET="${DATASET:-campuspulse}"
INGEST_KEY="${INGEST_KEY:-$(openssl rand -hex 16)}"
SA="campuspulse-run@${PROJECT_ID}.iam.gserviceaccount.com"

cd "$(dirname "$0")/.."
gcloud config set project "$PROJECT_ID"

echo "Enabling APIs"
gcloud services enable run.googleapis.com bigquery.googleapis.com pubsub.googleapis.com \
  aiplatform.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com cloudscheduler.googleapis.com

echo "Service account"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create campuspulse-run --display-name "CampusPulse Cloud Run"
for role in roles/bigquery.dataEditor roles/bigquery.jobUser roles/pubsub.publisher roles/aiplatform.user; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$SA" --role "$role" --quiet >/dev/null
done

echo "BigQuery dataset, tables, views and 28 days of simulated history"
GOOGLE_CLOUD_PROJECT="$PROJECT_ID" CP_BACKEND=bigquery CP_BQ_DATASET="$DATASET" CP_BQ_LOCATION="$REGION" \
  python scripts/bq_setup.py --seed

echo "Cloud Run (builds the root Dockerfile with Cloud Build)"
gcloud run deploy "$SERVICE" --source . --region "$REGION" \
  --service-account "$SA" --allow-unauthenticated \
  --min-instances 1 --max-instances 2 --memory 1Gi --cpu 1 \
  --set-env-vars "CP_BACKEND=bigquery,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,CP_BQ_DATASET=$DATASET,CP_BQ_LOCATION=$REGION,CP_GEMINI_VERTEX=true,CP_INGEST_KEY=$INGEST_KEY"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"
echo
echo "Deployed: $URL"
echo "Ingest key (put it in firmware secrets.h and the MQTT bridge): $INGEST_KEY"
echo "Train BigQuery ML once data is in: GOOGLE_CLOUD_PROJECT=$PROJECT_ID CP_BACKEND=bigquery python scripts/bq_setup.py --ml"
