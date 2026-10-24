#!/usr/bin/env bash
# Deploys what is checked out now, without waiting for the push trigger:
# the same steps as cloudbuild.yaml (staging, then production if live, then
# Firebase Hosting), run in Cloud Build from this folder.
#   source scripts/gcp/auth.sh && scripts/gcp/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
source deploy/gcp/settings.env
VERSION=$(git rev-parse --short=7 HEAD 2>/dev/null || date +%H%M%S)
gcloud builds submit --project "$PROJECT_ID" --region "$REGION" \
  --service-account "projects/$PROJECT_ID/serviceAccounts/arfa-build@$PROJECT_ID.iam.gserviceaccount.com" \
  --config cloudbuild.yaml --substitutions "_VERSION=$VERSION" .
