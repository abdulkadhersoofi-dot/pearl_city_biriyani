#!/usr/bin/env bash
# ONE-TIME setup of the Google Cloud project, run once by the project's
# owner in Google Cloud Shell (it is already signed in as you):
#
#   git clone https://github.com/abdulkadhersoofi-dot/pearl_city_biriyani.git
#   cd pearl_city_biriyani && git checkout claude/epic-keller-g2gvbr
#   bash scripts/gcp/bootstrap.sh YOUR_PROJECT_ID
#
# It does the parts only the owner may do: turns on the services, makes
# the service accounts and gives each only what it needs, creates the
# image store, the transfer bucket and the empty secrets, sets the
# Rs.15,000/month budget alert, and makes the key Claude uses
# (claude-deployer - it can deploy, read logs and work on the database,
# but cannot delete the database, its backups or the project).
# Safe to run again: whatever exists already is left as it is.
set -euo pipefail

PROJECT_ID=${1:?"usage: bash scripts/gcp/bootstrap.sh YOUR_PROJECT_ID"}
REGION=asia-south1
step() { echo; echo "== $*"; }
sa() { echo "$1@$PROJECT_ID.iam.gserviceaccount.com"; }

gcloud config set project "$PROJECT_ID" >/dev/null
BILLING=$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingAccountName)' 2>/dev/null || true)
if [ -z "$BILLING" ]; then
  echo "This project has no billing account linked. Link one (Billing -> Link a billing account), then run this again."
  exit 1
fi

step "1/9 Turning on the Google Cloud services ARFA uses (takes a minute or two)"
gcloud services enable run.googleapis.com sqladmin.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com cloudscheduler.googleapis.com \
  iam.googleapis.com iamcredentials.googleapis.com cloudresourcemanager.googleapis.com \
  firebase.googleapis.com firebasehosting.googleapis.com billingbudgets.googleapis.com \
  cloudbilling.googleapis.com logging.googleapis.com monitoring.googleapis.com storage.googleapis.com \
  serviceusage.googleapis.com

step "2/9 Service accounts"
make_sa() {
  gcloud iam service-accounts describe "$(sa "$1")" >/dev/null 2>&1 || \
    gcloud iam service-accounts create "$1" --display-name "$2"
}
make_sa arfa-run "ARFA app (Cloud Run)"
make_sa arfa-build "ARFA builds and deploys (Cloud Build)"
make_sa arfa-ops "ARFA admin jobs (grants, data transfer)"
make_sa arfa-scheduler "ARFA nightly jobs (Cloud Scheduler)"
make_sa claude-deployer "Claude Code - deploys, logs, database work (no deletes)"

step "3/9 The database role for Claude and the builds: everything except deleting"
SQL_PERMS=cloudsql.instances.get,cloudsql.instances.list,cloudsql.instances.create,cloudsql.instances.update
SQL_PERMS+=,cloudsql.instances.export,cloudsql.instances.import,cloudsql.instances.executeSql
SQL_PERMS+=,cloudsql.instances.login,cloudsql.instances.connect,cloudsql.instances.clone,cloudsql.instances.restart
SQL_PERMS+=,cloudsql.databases.create,cloudsql.databases.get,cloudsql.databases.list,cloudsql.databases.update
SQL_PERMS+=,cloudsql.users.create,cloudsql.users.list,cloudsql.users.update
SQL_PERMS+=,cloudsql.backupRuns.create,cloudsql.backupRuns.get,cloudsql.backupRuns.list
if gcloud iam roles describe arfaSqlOperator --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam roles update arfaSqlOperator --project "$PROJECT_ID" --permissions "$SQL_PERMS" --quiet >/dev/null
else
  gcloud iam roles create arfaSqlOperator --project "$PROJECT_ID" --title "ARFA SQL operator (no delete)" \
    --permissions "$SQL_PERMS" --quiet >/dev/null
fi

step "4/9 Permissions - each account gets only what it needs"
grant() {
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$(sa "$1")" --role "$2" \
    --condition=None --quiet >/dev/null
  echo "  $1: $2"
}
# The app itself: connect to the database, write logs.
grant arfa-run roles/cloudsql.client
grant arfa-run roles/logging.logWriter
# Builds: deploy Cloud Run and its jobs (and keep them reachable - Firebase
# Hosting needs that; the app does its own sign-in), push images, publish
# Firebase Hosting.
grant arfa-build roles/run.admin
grant arfa-build roles/artifactregistry.writer
grant arfa-build roles/logging.logWriter
grant arfa-build roles/firebasehosting.admin
grant arfa-build roles/firebase.viewer
grant arfa-build roles/cloudsql.client
grant arfa-build "projects/$PROJECT_ID/roles/arfaSqlOperator"
# Admin jobs: connect as the database admin user.
grant arfa-ops roles/cloudsql.client
grant arfa-ops roles/logging.logWriter
# Nightly jobs: start the "nightly" Cloud Run Job.
grant arfa-scheduler roles/run.invoker
# Claude: deploy, read every log, work on the database (no deletes),
# rotate secrets, schedule jobs, publish Firebase Hosting.
grant claude-deployer roles/run.admin
grant claude-deployer roles/cloudbuild.builds.editor
grant claude-deployer roles/cloudbuild.connectionViewer
grant claude-deployer roles/logging.viewer
grant claude-deployer roles/monitoring.viewer
grant claude-deployer roles/cloudscheduler.admin
grant claude-deployer roles/firebasehosting.admin
grant claude-deployer roles/firebase.viewer
grant claude-deployer roles/secretmanager.secretVersionManager
grant claude-deployer roles/secretmanager.secretAccessor
grant claude-deployer roles/artifactregistry.reader
grant claude-deployer roles/serviceusage.serviceUsageConsumer
grant claude-deployer "projects/$PROJECT_ID/roles/arfaSqlOperator"
# "Act as": deploying something that runs as another account needs this.
act_as() {
  gcloud iam service-accounts add-iam-policy-binding "$(sa "$2")" --member "serviceAccount:$(sa "$1")" \
    --role roles/iam.serviceAccountUser --quiet >/dev/null
  echo "  $1 may act as $2"
}
for target in arfa-run arfa-ops arfa-scheduler arfa-build; do act_as claude-deployer "$target"; done
act_as arfa-build arfa-run

step "5/9 Image store (Artifact Registry, Mumbai)"
gcloud artifacts repositories describe arfa --location "$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create arfa --repository-format docker --location "$REGION" \
    --description "ARFA server images"

step "6/9 Transfer bucket (the one-time copy of the Render database; files deleted after 30 days)"
BUCKET="gs://$PROJECT_ID-transfer"
if ! gcloud storage buckets describe "$BUCKET" >/dev/null 2>&1; then
  gcloud storage buckets create "$BUCKET" --location "$REGION" --uniform-bucket-level-access
  printf '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30}}]}' > /tmp/arfa-lifecycle.json
  gcloud storage buckets update "$BUCKET" --lifecycle-file /tmp/arfa-lifecycle.json
fi
for who in claude-deployer arfa-build; do
  gcloud storage buckets add-iam-policy-binding "$BUCKET" --member "serviceAccount:$(sa "$who")" \
    --role roles/storage.admin --quiet >/dev/null
done

step "7/9 Secrets (empty boxes; the values are filled in later)"
secret() {
  gcloud secrets describe "$1" >/dev/null 2>&1 || \
    gcloud secrets create "$1" --replication-policy user-managed --locations "$REGION" --quiet >/dev/null
  for who in "${@:2}"; do
    gcloud secrets add-iam-policy-binding "$1" --member "serviceAccount:$(sa "$who")" \
      --role roles/secretmanager.secretAccessor --quiet >/dev/null
  done
  echo "  $1"
}
for s in arfa-db-url arfa-secret-key arfa-einvoice-key arfa-db-url-staging arfa-secret-key-staging \
         arfa-einvoice-key-staging arfa-sentry-dsn arfa-smtp-host arfa-smtp-username arfa-smtp-password arfa-smtp-from; do
  secret "$s" arfa-run
done
secret arfa-db-admin-url arfa-ops
secret arfa-render-db-url arfa-build
secret arfa-db-url arfa-run arfa-build      # the build reads it only for a rollback copy back to Render

step "8/9 Budget alert: Rs.15,000 a month (emails at 50%, 90% and 100%)"
if gcloud billing budgets list --billing-account "${BILLING#billingAccounts/}" --format='value(displayName)' 2>/dev/null \
    | grep -qx "ARFA monthly"; then
  echo "  already set"
else
  gcloud billing budgets create --billing-account "${BILLING#billingAccounts/}" --display-name "ARFA monthly" \
    --budget-amount 15000INR --filter-projects "projects/$PROJECT_ID" \
    --threshold-rule percent=0.5 --threshold-rule percent=0.9 --threshold-rule percent=1.0 \
    || echo "  Couldn't set it here (is the billing account in INR?) - set it in Billing -> Budgets & alerts instead."
fi

step "9/9 Claude's key"
KEY=~/claude-deployer-key.json
if [ -f "$KEY" ]; then
  echo "  $KEY already exists - not making another."
elif gcloud iam service-accounts keys create "$KEY" --iam-account "$(sa claude-deployer)"; then
  echo "  Made $KEY"
else
  echo "  Key creation was refused. If your account is in a Google Workspace organisation, the policy"
  echo "  iam.disableServiceAccountKeyCreation blocks it: IAM & Admin -> Organization policies ->"
  echo "  'Disable service account key creation' -> Override for this project -> Off. Then run this again."
  exit 1
fi

echo
echo "Done. Next (docs/cloud-setup.md, step 6):"
echo "  1. Download the key:      cloudshell download $KEY"
echo "  2. Put its whole text in the Claude Code cloud environment as GCP_SA_KEY_JSON"
echo "  3. Delete it here:        rm $KEY"
