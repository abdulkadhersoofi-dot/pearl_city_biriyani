#!/usr/bin/env bash
# Releases one built image to staging or production:
#   1. the "migrate" job brings that database's schema up to date,
#   2. the new revision is deployed with no traffic,
#   3. its /health must answer 200 with the new version (not /healthz:
#      Cloud Run keeps paths ending in "z" for itself),
#   4. only then does it take the traffic (and the nightly job is updated).
# If 2-3 fail, the version already serving keeps serving.
#
#   scripts/gcp/release.sh staging|production IMAGE VERSION
#
# Cloud Build runs it on every push (cloudbuild.yaml); scripts/gcp/deploy.sh
# runs the same thing by hand.
set -euo pipefail
cd "$(dirname "$0")/../.."
source deploy/gcp/settings.env

TARGET=${1:?staging or production}
IMAGE=${2:?image}
VERSION=${COMMIT:-${3:?version}}   # COMMIT: the pushed commit (trigger builds); else the 3rd argument
CONN="$PROJECT_ID:$REGION:$SQL_INSTANCE"
RUN_SA="arfa-run@$PROJECT_ID.iam.gserviceaccount.com"

case "$TARGET" in
  staging)
    SVC=$STAGING_SERVICE MIN=0 MAX=$STAGING_MAX_INSTANCES SUFFIX="-staging"
    EXTRA_ENV="LOG_FORWARDED_FOR=1" ;;
  production)
    if [ "$PRODUCTION_ENABLED" != "1" ]; then
      echo "Production isn't live yet (PRODUCTION_ENABLED=0 in deploy/gcp/settings.env) - skipped."
      exit 0
    fi
    SVC=$SERVICE MIN=$MIN_INSTANCES MAX=$MAX_INSTANCES SUFFIX="" EXTRA_ENV="LOG_FORWARDED_FOR=0" ;;
  *) echo "staging or production" >&2; exit 2 ;;
esac

ENV_VARS="FLASK_ENV=production,SESSION_TYPE=database,SESSION_COOKIE_NAME=__session,RUN_MIGRATIONS_ON_START=0"
ENV_VARS+=",GUNICORN_WORKERS=$WORKERS,GUNICORN_THREADS=$THREADS,DB_POOL_SIZE=$POOL_SIZE,DB_MAX_OVERFLOW=$MAX_OVERFLOW"
ENV_VARS+=",PROXY_FIX_X_FOR=$PROXY_FIX_X_FOR,PROXY_FIX_X_HOST=1,PUBLIC_URL=$PUBLIC_URL,APP_VERSION=$VERSION,$EXTRA_ENV"
SECRETS="DATABASE_URL=arfa-db-url$SUFFIX:latest,SECRET_KEY=arfa-secret-key$SUFFIX:latest"
SECRETS+=",EINVOICE_SECRET_KEY=arfa-einvoice-key$SUFFIX:latest"
# Optional extras, listed in OPTIONAL_SECRETS (deploy/gcp/settings.env) once
# they hold a value. The build account may read a secret but not look one up,
# so a "does it exist?" check here would always say no.
uses() { case " $OPTIONAL_SECRETS " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }
uses sentry && SECRETS+=",SENTRY_DSN=arfa-sentry-dsn:latest"
uses gemini && SECRETS+=",GEMINI_API_KEY=arfa-gemini-key:latest"   # Scan bills (app/scan) reads with it
if uses smtp; then
  SECRETS+=",SMTP_HOST=arfa-smtp-host:latest,SMTP_USERNAME=arfa-smtp-username:latest"
  SECRETS+=",SMTP_PASSWORD=arfa-smtp-password:latest,SMTP_FROM=arfa-smtp-from:latest"
fi

# The Ultra Admin login, made by the migrate job on an empty database
# (flask bootstrap-super-admin; it does nothing once the account exists).
# Only the job sees it, never the web service.
JOB_SECRETS=$SECRETS
if uses super-admin; then
  JOB_SECRETS+=",SUPER_ADMIN_EMAIL=arfa-super-admin-email:latest,SUPER_ADMIN_PASSWORD=arfa-super-admin-password:latest"
fi

echo "== $TARGET: migrating the $TARGET database"
gcloud run jobs deploy "arfa-migrate$SUFFIX" --project "$PROJECT_ID" --region "$REGION" --image "$IMAGE" \
  --service-account "$RUN_SA" --set-cloudsql-instances "$CONN" --command deploy/migrate.sh \
  --set-env-vars "$ENV_VARS,SUPER_ADMIN_NAME=Firm Admin" --set-secrets "$JOB_SECRETS" \
  --max-retries 0 --task-timeout 3600 --memory 1Gi --quiet
gcloud run jobs execute "arfa-migrate$SUFFIX" --project "$PROJECT_ID" --region "$REGION" --wait

echo "== $TARGET: new revision, no traffic yet"
# Cloud Run can't create a service with --no-traffic. The very first deploy
# of each service has no older version to protect, so it takes the traffic
# at once; the health check below still has to pass before the release is
# reported done.
NO_TRAFFIC="--no-traffic"
gcloud run services describe "$SVC" --project "$PROJECT_ID" --region "$REGION" >/dev/null 2>&1 || NO_TRAFFIC=""
gcloud run deploy "$SVC" --project "$PROJECT_ID" --region "$REGION" --image "$IMAGE" \
  --service-account "$RUN_SA" --add-cloudsql-instances "$CONN" \
  --cpu "$CPU" --memory "$MEMORY" --concurrency "$CONCURRENCY" --min-instances "$MIN" --max-instances "$MAX" \
  --timeout 60 --cpu-boost --set-env-vars "$ENV_VARS" --set-secrets "$SECRETS" \
  --allow-unauthenticated $NO_TRAFFIC --tag candidate --quiet

URL=$(gcloud run services describe "$SVC" --project "$PROJECT_ID" --region "$REGION" \
  --format 'value(status.traffic[].url)' | tr ';' '\n' | grep '^https://candidate---' | head -1)
echo "== $TARGET: checking $URL/health"
ok=""
for attempt in 1 2 3 4 5 6; do
  body=$(curl -fsS --max-time 20 "$URL/health" || true)
  if echo "$body" | grep -q "\"version\": *\"${VERSION:0:7}\"" && echo "$body" | grep -q '"status": *"ok"'; then
    ok=1; break
  fi
  sleep 10
done
if [ -z "$ok" ]; then
  echo "The new revision didn't answer healthy - it gets no traffic; the current one keeps serving." >&2
  echo "Last answer: $body" >&2
  exit 1
fi

echo "== $TARGET: moving traffic to the new revision"
gcloud run services update-traffic "$SVC" --project "$PROJECT_ID" --region "$REGION" --to-latest --quiet

if [ "$TARGET" = "production" ]; then
  gcloud run jobs deploy arfa-nightly --project "$PROJECT_ID" --region "$REGION" --image "$IMAGE" \
    --service-account "$RUN_SA" --set-cloudsql-instances "$CONN" --command flask --args nightly \
    --set-env-vars "$ENV_VARS" --set-secrets "$SECRETS" --max-retries 1 --task-timeout 1800 --quiet
fi
echo "== $TARGET: live on $VERSION"
