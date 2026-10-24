#!/usr/bin/env bash
# Copies the Render database into Cloud SQL and proves the copy is exact.
#   scripts/gcp/transfer.sh staging       a rehearsal, into the staging database (emptied first)
#   scripts/gcp/transfer.sh production    the cutover itself (the production database must be empty)
# Needs the Render database's external URL in the secret arfa-render-db-url
# (setup guide step 7). See docs/cloud-cutover.md for when to run which.
set -euo pipefail
cd "$(dirname "$0")/../.."
source deploy/gcp/settings.env
TARGET=${1:?staging or production}
case "$TARGET" in
  staging)    DB=$STAGING_DB_NAME USER=arfa_staging_app ;;
  production) DB=$DB_NAME USER=arfa_app ;;
  *) echo "staging or production" >&2; exit 2 ;;
esac
RUN_ID="$TARGET-$(date -u +%Y%m%d-%H%M%S)"
admin_sql() {
  gcloud run jobs execute arfa-admin-sql --project "$PROJECT_ID" --region "$REGION" --wait --quiet \
    --update-env-vars "SQL_DB=$1,SQL_B64=$(printf '%s' "$2" | base64 -w0)"
}
tables_in() {
  gcloud sql instances execute-sql "$SQL_INSTANCE" --project "$PROJECT_ID" --database "$1" --format json \
    --sql "SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = 'public'" |
    python3 -c '
import json, sys
def values(node):
    if isinstance(node, dict):
        if "value" in node:
            yield str(node["value"])
        for v in node.values():
            yield from values(v)
    elif isinstance(node, list):
        for v in node:
            yield from values(v)
found = [v for v in values(json.load(sys.stdin)) if v.isdigit()]
if not found:
    sys.exit("could not read the table count")
print(found[0])'
}

N=$(tables_in "$DB")
if [ "$N" != "0" ]; then
  if [ "$TARGET" = "production" ]; then
    echo "The production database already has $N tables - refusing to import over it." >&2
    exit 1
  fi
  echo "DESTRUCTIVE (staging only): emptying $DB ($N tables) for a fresh rehearsal copy."
  admin_sql "$DB" "DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION $USER;"
fi

SQL_SA=$(gcloud sql instances describe "$SQL_INSTANCE" --project "$PROJECT_ID" --format 'value(serviceAccountEmailAddress)')
gcloud storage buckets add-iam-policy-binding "gs://$PROJECT_ID-transfer" --member "serviceAccount:$SQL_SA" \
  --role roles/storage.objectViewer --quiet >/dev/null

echo "== Copying Render -> $DB (run $RUN_ID)"
gcloud builds submit --project "$PROJECT_ID" --region "$REGION" \
  --service-account "projects/$PROJECT_ID/serviceAccounts/arfa-build@$PROJECT_ID.iam.gserviceaccount.com" \
  --config deploy/gcp/cloudbuild-transfer.yaml \
  --substitutions "_TARGET_DB=$DB,_TARGET_USER=$USER,_RUN_ID=$RUN_ID,_INSTANCE=$SQL_INSTANCE" .

echo "== Comparing the copy with Render"
WORK=$(mktemp -d)
gcloud storage cp "gs://$PROJECT_ID-transfer/$RUN_ID/render_counts.txt" "$WORK/render.txt" --quiet
gcloud sql instances execute-sql "$SQL_INSTANCE" --project "$PROJECT_ID" --database "$DB" --format json \
  --row_limit 100000 --sql "$(grep -v '^--' deploy/gcp/verify_counts.sql | tr '\n' ' ')" > "$WORK/copy.json"
python3 - "$WORK/render.txt" "$WORK/copy.json" <<'PY'
import json, sys
render = dict(line.rstrip("\n").split("|", 1) for line in open(sys.argv[1]) if "|" in line)
copy = {}
def walk(node):
    if isinstance(node, dict):
        vals = node.get("values")
        if isinstance(vals, list) and len(vals) == 2:
            copy[str(vals[0].get("value"))] = str(vals[1].get("value"))
        for v in node.values():
            walk(v)
    elif isinstance(node, list):
        for v in node:
            walk(v)
walk(json.load(open(sys.argv[2])))
bad = [(k, render.get(k), copy.get(k)) for k in sorted(set(render) | set(copy)) if render.get(k) != copy.get(k)]
print(f"{len(render)} checks on Render, {len(copy)} on the copy")
for k, a, b in bad:
    print(f"  DIFFERENT {k}: Render {a} / copy {b}")
if bad or not render:
    print("NOT IDENTICAL - do not go live on this copy.")
    sys.exit(1)
print("IDENTICAL: every table's rows, every business's invoices, bills and notes (count and value), every series.")
PY
