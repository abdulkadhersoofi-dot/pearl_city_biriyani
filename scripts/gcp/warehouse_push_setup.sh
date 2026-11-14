#!/usr/bin/env bash
# One-time setup so new orders ring on the ARFA Warehouse phones like a
# message (Firebase Cloud Messaging). Run it once, in Cloud Shell, as the
# project's owner, after pulling this branch:
#
#   cd pearl_city_biriyani && git pull && bash scripts/gcp/warehouse_push_setup.sh
#
# It:
#   1. switches on the Firebase and Cloud Messaging APIs;
#   2. lets arfa-run (the website) send pushes;
#   3. registers the app com.arfa.warehouse with the project's Firebase;
#   4. keeps the app's Firebase settings (not secrets, but long) in the
#      secret arfa-warehouse-fcm, readable by arfa-run.
# Then add "warehouse-push" to OPTIONAL_SECRETS in deploy/gcp/settings.env and
# deploy: the website hands the phones those settings at sign-in and starts
# sending. Until then the app works on polling every 30 seconds.
# Safe to run again.
set -euo pipefail
cd "$(dirname "$0")/../.."
source deploy/gcp/settings.env

PACKAGE="com.arfa.warehouse"
RUN_SA="arfa-run@$PROJECT_ID.iam.gserviceaccount.com"
SECRET="arfa-warehouse-fcm"
API="https://firebase.googleapis.com/v1beta1/projects/$PROJECT_ID"

gcloud config set project "$PROJECT_ID" --quiet >/dev/null
step() { printf '\n== %s\n' "$1"; }
call() { curl -sS -H "Authorization: Bearer $(gcloud auth print-access-token)" -H "x-goog-user-project: $PROJECT_ID" \
  -H "Content-Type: application/json" "$@"; }

step "1/4 Firebase and Cloud Messaging APIs"
gcloud services enable firebase.googleapis.com fcm.googleapis.com fcmregistrations.googleapis.com --quiet

step "2/4 arfa-run may send pushes"
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$RUN_SA" \
  --role roles/firebasecloudmessaging.admin --condition None --quiet >/dev/null
echo "ok"

step "3/4 The Android app $PACKAGE in Firebase"
find_app() {
  call "$API/androidApps?pageSize=100" | python3 -c '
import json, sys
apps = json.load(sys.stdin).get("apps", [])
print(next((a["appId"] for a in apps if a.get("packageName") == sys.argv[1]), ""))' "$PACKAGE"
}
APP_ID=$(find_app)
if [ -z "$APP_ID" ]; then
  call -X POST "$API/androidApps" -d "{\"packageName\": \"$PACKAGE\", \"displayName\": \"ARFA Warehouse\"}" >/dev/null
  for _ in $(seq 1 20); do
    sleep 3
    APP_ID=$(find_app)
    [ -n "$APP_ID" ] && break
  done
fi
[ -n "$APP_ID" ] || { echo "Firebase didn't register the app - run this again in a minute."; exit 1; }
echo "app id $APP_ID"

step "4/4 The app's settings in the secret $SECRET"
CONFIG=$(call "$API/androidApps/$APP_ID/config" | python3 -c '
import base64, json, sys
cfg = json.loads(base64.b64decode(json.load(sys.stdin)["configFileContents"]))
client = next(c for c in cfg["client"] if c["client_info"]["android_client_info"]["package_name"] == sys.argv[1])
print(json.dumps({"app_id": client["client_info"]["mobilesdk_app_id"],
                  "api_key": client["api_key"][0]["current_key"],
                  "project_id": cfg["project_info"]["project_id"],
                  "sender_id": cfg["project_info"]["project_number"]}))' "$PACKAGE")
if ! gcloud secrets describe "$SECRET" >/dev/null 2>&1; then
  gcloud secrets create "$SECRET" --replication-policy automatic --quiet >/dev/null
fi
printf '%s' "$CONFIG" | gcloud secrets versions add "$SECRET" --data-file=- --quiet >/dev/null
gcloud secrets add-iam-policy-binding "$SECRET" --member "serviceAccount:$RUN_SA" \
  --role roles/secretmanager.secretAccessor --quiet >/dev/null
echo "saved"

printf '\nDone. Add "warehouse-push" to OPTIONAL_SECRETS in deploy/gcp/settings.env (or ask Claude to), then deploy.\n'
