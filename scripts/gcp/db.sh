#!/usr/bin/env bash
# On your own computer (Fedora), a psql session on Cloud SQL through the
# Cloud SQL Auth Proxy - one command. Needs: gcloud signed in
# (gcloud auth login && gcloud auth application-default login), the
# cloud-sql-proxy binary, and psql. Your account needs Cloud SQL Client.
#   scripts/gcp/db.sh            production
#   scripts/gcp/db.sh staging    staging
# (A Claude Code cloud session can't open database ports; it uses
# scripts/gcp/sql.sh, the HTTPS Data API, instead.)
set -euo pipefail
cd "$(dirname "$0")/../.."
source deploy/gcp/settings.env
SECRET=arfa-db-url
[ "${1:-}" = "staging" ] && SECRET=arfa-db-url-staging
URL=$(gcloud secrets versions access latest --secret "$SECRET" --project "$PROJECT_ID")
USER_PW=${URL#postgresql://}; USER_PW=${USER_PW%%@*}
DB=${URL##*/}; DB=${DB%%\?*}
PORT=55432
cloud-sql-proxy --port "$PORT" "$PROJECT_ID:$REGION:$SQL_INSTANCE" >/tmp/arfa-proxy.log 2>&1 &
PROXY=$!
trap 'kill $PROXY 2>/dev/null' EXIT
sleep 3
PGPASSWORD=${USER_PW#*:} psql "host=127.0.0.1 port=$PORT dbname=$DB user=${USER_PW%%:*} sslmode=disable"
