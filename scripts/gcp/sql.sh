#!/usr/bin/env bash
# Runs SQL on the Cloud SQL database through the Data API, as Claude's own
# database login (claude-deployer, a member of the app's role):
#   scripts/gcp/sql.sh "SELECT count(*) FROM invoices"
#   scripts/gcp/sql.sh --staging "SELECT ..."
#   scripts/gcp/sql.sh --destructive "UPDATE ... WHERE id = 42"
#
# Anything that changes or removes data (UPDATE, DELETE, DROP, TRUNCATE,
# ALTER, INSERT, CREATE, GRANT, REVOKE) is refused unless --destructive is
# given - and then, on production, an on-demand backup is taken first, so
# the change can be undone from it (besides the 7 days of point-in-time
# recovery). Schema changes belong in migrations, not here.
set -euo pipefail
cd "$(dirname "$0")/../.."
source deploy/gcp/settings.env
DB=$DB_NAME ALLOW=""
while [ $# -gt 1 ]; do
  case "$1" in
    --staging) DB=$STAGING_DB_NAME ;;
    --destructive) ALLOW=1 ;;
    *) break ;;
  esac
  shift
done
SQL=${1:?SQL}
if printf '%s' "$SQL" | grep -qiE '\b(update|delete|drop|truncate|alter|insert|create|grant|revoke|vacuum|reindex)\b'; then
  if [ -z "$ALLOW" ]; then
    echo "Refused: this changes data or schema. Re-run with --destructive once it has been said out loud what it does." >&2
    exit 3
  fi
  if [ "$DB" = "$DB_NAME" ]; then
    echo "Taking an on-demand backup of $SQL_INSTANCE first..."
    gcloud sql backups create --instance "$SQL_INSTANCE" --project "$PROJECT_ID" \
      --description "before: ${SQL:0:80}" --quiet
  fi
fi
gcloud sql instances execute-sql "$SQL_INSTANCE" --project "$PROJECT_ID" --database "$DB" --sql "$SQL" \
  --row_limit 500 --format json
