#!/usr/bin/env bash
# ROLLBACK ONLY: copies the Cloud SQL production database back over Render's
# (docs/cloud-cutover.md, trigger R3). DESTRUCTIVE for Render's database - it
# is replaced by the Cloud copy - so it runs only with the flag below, and
# only while Google Cloud is no longer taking changes.
#   scripts/gcp/transfer_back.sh --replace-render-database
set -euo pipefail
cd "$(dirname "$0")/../.."
source deploy/gcp/settings.env
[ "${1:-}" = "--replace-render-database" ] || { sed -n '2,7p' "$0"; exit 2; }
gcloud builds submit --project "$PROJECT_ID" --region "$REGION" \
  --service-account "projects/$PROJECT_ID/serviceAccounts/arfa-build@$PROJECT_ID.iam.gserviceaccount.com" \
  --config deploy/gcp/cloudbuild-transfer-back.yaml \
  --substitutions "_CONN=$PROJECT_ID:$REGION:$SQL_INSTANCE,_DB=$DB_NAME,_RUN_ID=back-$(date -u +%Y%m%d-%H%M%S)" .
