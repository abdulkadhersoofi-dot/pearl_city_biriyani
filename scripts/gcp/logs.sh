#!/usr/bin/env bash
# Reading what happened, without the console:
#   scripts/gcp/logs.sh builds            the last builds and how they ended
#   scripts/gcp/logs.sh build [ID]        one build's full log (default: the latest)
#   scripts/gcp/logs.sh app [N]           the app's last N log lines (default 100)
#   scripts/gcp/logs.sh errors [HOURS]    the app's errors in the last HOURS (default 24)
#   scripts/gcp/logs.sh staging [N]       the same for staging
#   scripts/gcp/logs.sh job NAME          the last run of a job (arfa-migrate, arfa-nightly, arfa-admin-sql ...)
set -euo pipefail
cd "$(dirname "$0")/../.."
source deploy/gcp/settings.env
P=(--project "$PROJECT_ID")
case "${1:-app}" in
  builds) gcloud builds list "${P[@]}" --region "$REGION" --limit 10 \
            --format 'table(id, createTime.date("%d %b %H:%M"), status, substitutions.SHORT_SHA)' ;;
  build)  ID=${2:-$(gcloud builds list "${P[@]}" --region "$REGION" --limit 1 --format 'value(id)')}
          gcloud builds log "$ID" "${P[@]}" --region "$REGION" ;;
  app)    gcloud run services logs read "$SERVICE" "${P[@]}" --region "$REGION" --limit "${2:-100}" ;;
  staging) gcloud run services logs read "$STAGING_SERVICE" "${P[@]}" --region "$REGION" --limit "${2:-100}" ;;
  errors) gcloud logging read "${P[@]}" --freshness "${2:-24}h" --limit 200 --format 'value(timestamp, textPayload)' \
            "resource.type=cloud_run_revision AND resource.labels.service_name=$SERVICE AND severity>=ERROR" ;;
  job)    EXEC=$(gcloud run jobs executions list "${P[@]}" --region "$REGION" --job "${2:?job name}" --limit 1 \
                   --format 'value(name)')
          gcloud logging read "${P[@]}" --freshness 7d --limit 500 --order asc --format 'value(textPayload)' \
            "resource.type=cloud_run_job AND labels.\"run.googleapis.com/execution_name\"=$EXEC" ;;
  *) sed -n '2,9p' "$0"; exit 2 ;;
esac
