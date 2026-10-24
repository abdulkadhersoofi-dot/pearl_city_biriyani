#!/usr/bin/env bash
# Builds ARFA's Google Cloud infrastructure, step by step, after the owner
# has run bootstrap.sh. Run by Claude with the deployer key
# (source scripts/gcp/auth.sh first). Each step is safe to run again.
#
#   scripts/gcp/setup.sh database      Cloud SQL (Mumbai, zonal, backups + 7-day PITR, Data API on,
#                                      deletion protection on), the two databases, users, grants
#   scripts/gcp/setup.sh connections   reads max_connections back and works out MAX_INSTANCES
#   scripts/gcp/setup.sh image         builds the first image (no deploy)
#   scripts/gcp/setup.sh staging       first staging release + its Firebase site
#   scripts/gcp/setup.sh scheduler     the nightly job at 01:30 IST
#   scripts/gcp/setup.sh trigger       git push -> deploy (needs the GitHub connection, setup guide step 5)
#   scripts/gcp/setup.sh domain        asks Firebase for arfa.live and prints the DNS records to add
set -euo pipefail
cd "$(dirname "$0")/../.."
source deploy/gcp/settings.env
CONN="$PROJECT_ID:$REGION:$SQL_INSTANCE"
DEPLOYER="claude-deployer@$PROJECT_ID.iam"
sa() { echo "$1@$PROJECT_ID.iam.gserviceaccount.com"; }
random() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }
put_secret() {   # put_secret NAME VALUE - adds a version (the old ones stay, disabled later by hand)
  printf '%s' "$2" | gcloud secrets versions add "$1" --data-file=- --project "$PROJECT_ID" --quiet >/dev/null
  echo "  secret $1 set"
}
has_secret() { gcloud secrets versions describe latest --secret "$1" --project "$PROJECT_ID" >/dev/null 2>&1; }

admin_sql() {    # admin_sql DB "SQL" - runs as the database admin, through the arfa-admin-sql job
  local b64
  b64=$(printf '%s' "$2" | base64 -w0)
  gcloud run jobs execute arfa-admin-sql --project "$PROJECT_ID" --region "$REGION" --wait \
    --update-env-vars "SQL_DB=$1,SQL_B64=$b64" --quiet
}

case "${1:-}" in
database)
  if ! gcloud sql instances describe "$SQL_INSTANCE" --project "$PROJECT_ID" >/dev/null 2>&1; then
    echo "== Creating Cloud SQL $SQL_INSTANCE ($SQL_TIER, $SQL_VERSION) - about 10 minutes"
    gcloud sql instances create "$SQL_INSTANCE" --project "$PROJECT_ID" --region "$REGION" \
      --database-version "$SQL_VERSION" --edition ENTERPRISE --tier "$SQL_TIER" --availability-type ZONAL \
      --storage-type SSD --storage-size "$SQL_STORAGE_GB" --storage-auto-increase \
      --backup-start-time 20:30 --retained-backups-count 14 \
      --enable-point-in-time-recovery --retained-transaction-log-days 7 \
      --maintenance-window-day SUN --maintenance-window-hour 21 \
      --database-flags "max_connections=$SQL_MAX_CONNECTIONS,cloudsql.iam_authentication=on" \
      --data-api-access ALLOW_DATA_API --deletion-protection --insights-config-query-insights-enabled
  fi
  for db in "$DB_NAME" "$STAGING_DB_NAME"; do
    gcloud sql databases describe "$db" --instance "$SQL_INSTANCE" --project "$PROJECT_ID" >/dev/null 2>&1 || \
      gcloud sql databases create "$db" --instance "$SQL_INSTANCE" --project "$PROJECT_ID"
  done
  if ! has_secret arfa-db-admin-url; then
    PW=$(random 24)
    gcloud sql users set-password postgres --instance "$SQL_INSTANCE" --project "$PROJECT_ID" --password "$PW"
    put_secret arfa-db-admin-url "postgresql://postgres:$PW@/$DB_NAME?host=/cloudsql/$CONN"
  fi
  gcloud sql users list --instance "$SQL_INSTANCE" --project "$PROJECT_ID" --format 'value(name)' | grep -qx "$DEPLOYER" || \
    gcloud sql users create "$DEPLOYER" --instance "$SQL_INSTANCE" --project "$PROJECT_ID" --type CLOUD_IAM_SERVICE_ACCOUNT

  IMAGE=$(gcloud artifacts docker images list "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/web" --project "$PROJECT_ID" \
    --sort-by ~UPDATE_TIME --limit 1 --format 'value(format("{}@{}", package, version))' 2>/dev/null || true)
  [ -n "$IMAGE" ] || { echo "No image yet - run: scripts/gcp/setup.sh image"; exit 1; }
  gcloud run jobs deploy arfa-admin-sql --project "$PROJECT_ID" --region "$REGION" --image "$IMAGE" \
    --service-account "$(sa arfa-ops)" --set-cloudsql-instances "$CONN" --command python --args deploy/run_sql.py \
    --set-secrets DATABASE_URL=arfa-db-admin-url:latest --set-env-vars SQL_B64=,SQL_DB= \
    --max-retries 0 --task-timeout 3600 --quiet

  # The app's own login per database, owning its database; Claude's IAM
  # login is a member of both, so its queries see what the app sees.
  for pair in "arfa_app:$DB_NAME:arfa-db-url" "arfa_staging_app:$STAGING_DB_NAME:arfa-db-url-staging"; do
    IFS=: read -r role db secret <<<"$pair"
    if ! has_secret "$secret"; then
      PW=$(random 24)
      admin_sql "$DB_NAME" "DO \$\$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$role') THEN
            CREATE ROLE $role LOGIN PASSWORD '$PW';
          ELSE ALTER ROLE $role LOGIN PASSWORD '$PW'; END IF; END \$\$;
        GRANT $role TO postgres;
        ALTER DATABASE $db OWNER TO $role;
        REVOKE CONNECT ON DATABASE $db FROM PUBLIC;
        GRANT CONNECT ON DATABASE $db TO $role;
        GRANT $role TO \"$DEPLOYER\";
        GRANT CONNECT ON DATABASE $db TO \"$DEPLOYER\";"
      put_secret "$secret" "postgresql://$role:$PW@/$db?host=/cloudsql/$CONN"
    fi
  done
  has_secret arfa-secret-key || put_secret arfa-secret-key "$(random 48)"
  has_secret arfa-secret-key-staging || put_secret arfa-secret-key-staging "$(random 48)"
  has_secret arfa-einvoice-key-staging || put_secret arfa-einvoice-key-staging "$(random 48)"
  has_secret arfa-einvoice-key || echo "  arfa-einvoice-key is still empty: the owner stores Render's SECRET_KEY there (setup guide step 7)."
  echo "Database ready. Next: scripts/gcp/setup.sh connections"
  ;;

connections)
  RAW=$(gcloud sql instances execute-sql "$SQL_INSTANCE" --project "$PROJECT_ID" --database "$DB_NAME" \
          --sql "SHOW max_connections" --format json)
  MC=$(printf '%s' "$RAW" | python3 -c 'import json, re, sys; m = re.findall(r"\b(\d{2,5})\b", json.dumps(json.load(sys.stdin))); print(m[0] if m else "")')
  if [ -z "$MC" ]; then echo "Couldn't read max_connections from: $RAW" >&2; exit 1; fi
  PER=$(( WORKERS * (POOL_SIZE + MAX_OVERFLOW) ))
  MAX=$(( (MC * 8 / 10 - RESERVED_CONNECTIONS) / PER ))
  echo "max_connections on the server: $MC"
  echo "each instance: up to $PER connections; reserved: $RESERVED_CONNECTIONS"
  echo "=> MAX_INSTANCES = $MAX ($(( MAX * CONCURRENCY )) requests at once)"
  if [ "$MAX" -lt 9 ]; then
    echo "That is under 9 instances (72 requests at once): raise SQL_TIER instead of capping traffic."
  fi
  sed -i "s/^MAX_INSTANCES=.*/MAX_INSTANCES=\"$MAX\"/" deploy/gcp/settings.env
  echo "deploy/gcp/settings.env updated - commit it."
  ;;

image)
  gcloud builds submit --project "$PROJECT_ID" --region "$REGION" --gcs-source-staging-dir "gs://$PROJECT_ID-transfer/_source" \
    --service-account "projects/$PROJECT_ID/serviceAccounts/$(sa arfa-build)" \
    --config deploy/gcp/cloudbuild-image.yaml --substitutions "_REGION=$REGION,_REPO=$REPO" .
  ;;

staging)
  npx --yes firebase-tools@15.32.1 hosting:sites:get "$PROJECT_ID-staging" --project "$PROJECT_ID" >/dev/null 2>&1 || \
    npx --yes firebase-tools@15.32.1 hosting:sites:create "$PROJECT_ID-staging" --project "$PROJECT_ID" --non-interactive
  scripts/gcp/deploy.sh
  ;;

scheduler)
  # The nightly Cloud Run job exists only once production is released
  # (release.sh). Until then the schedule is created paused; running this
  # step again after the cutover binds the invoker and resumes it.
  HAVE_JOB=""
  if gcloud run jobs describe arfa-nightly --project "$PROJECT_ID" --region "$REGION" >/dev/null 2>&1; then
    HAVE_JOB=1
    gcloud run jobs add-iam-policy-binding arfa-nightly --project "$PROJECT_ID" --region "$REGION" \
      --member "serviceAccount:$(sa arfa-scheduler)" --role roles/run.invoker --quiet >/dev/null
  fi
  URI="https://run.googleapis.com/v2/projects/$PROJECT_ID/locations/$REGION/jobs/arfa-nightly:run"
  if gcloud scheduler jobs describe arfa-nightly --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
    VERB=update
  else
    VERB=create
  fi
  gcloud scheduler jobs "$VERB" http arfa-nightly --location "$REGION" --project "$PROJECT_ID" \
    --schedule "30 1 * * *" --time-zone "Asia/Kolkata" --uri "$URI" --http-method POST \
    --oauth-service-account-email "$(sa arfa-scheduler)" --attempt-deadline 30m
  if [ -n "$HAVE_JOB" ]; then
    gcloud scheduler jobs resume arfa-nightly --location "$REGION" --project "$PROJECT_ID" --quiet 2>/dev/null || true
    echo "Nightly job scheduled at 01:30 IST and enabled."
  else
    gcloud scheduler jobs pause arfa-nightly --location "$REGION" --project "$PROJECT_ID" --quiet
    echo "Nightly job scheduled at 01:30 IST but PAUSED: the arfa-nightly job appears with the production release."
    echo "Run scripts/gcp/setup.sh scheduler again after the cutover to enable it."
  fi
  ;;

trigger)
  REPO_NAME=$(gcloud builds repositories list --connection github --region "$REGION" --project "$PROJECT_ID" \
    --format 'value(name)' | head -1)
  [ -n "$REPO_NAME" ] || { echo "No GitHub connection yet (setup guide step 5)."; exit 1; }
  REPO_PATH="projects/$PROJECT_ID/locations/$REGION/connections/github/repositories/${REPO_NAME##*/}"
  gcloud builds triggers describe arfa-push --region "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1 && \
    { echo "Trigger arfa-push exists."; exit 0; }
  gcloud builds triggers create github --name arfa-push --region "$REGION" --project "$PROJECT_ID" \
    --repository "$REPO_PATH" --branch-pattern '^claude/epic-keller-g2gvbr$' --build-config cloudbuild.yaml \
    --service-account "projects/$PROJECT_ID/serviceAccounts/$(sa arfa-build)"
  ;;

domain)
  TOKEN=$(gcloud auth print-access-token)
  DOMAIN=${2:-arfa.live}
  API="https://firebasehosting.googleapis.com/v1beta1/projects/$PROJECT_ID/sites/$PROJECT_ID/customDomains"
  curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    "$API?customDomainId=$DOMAIN" -d '{}' >/dev/null || true
  sleep 5
  curl -sS -H "Authorization: Bearer $TOKEN" "$API/$DOMAIN" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("Status:", d.get("hostState"), "/", d.get("ownershipState"), "/ certificate:", (d.get("cert") or {}).get("state"))
for upd in ((d.get("requiredDnsUpdates") or {}).get("desired") or []):
    for rec in upd.get("records", []):
        print(f"  {rec.get(\"requiredAction\", \"ADD\"):6} {rec.get(\"type\"):6} {rec.get(\"domainName\")}  ->  {rec.get(\"rdata\")}")
'
  ;;

*)
  sed -n '2,20p' "$0"; exit 2 ;;
esac
