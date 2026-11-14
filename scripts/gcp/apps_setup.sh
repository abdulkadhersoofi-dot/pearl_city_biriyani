#!/usr/bin/env bash
# One-time setup so the Android apps are handed out from arfa.live
# (arfa.live/apps, app/apps) instead of GitHub. Run it once, in Cloud Shell,
# as the project's owner:
#
#   git clone https://github.com/abdulkadhersoofi-dot/pearl_city_biriyani.git
#   cd pearl_city_biriyani && git checkout claude/exciting-franklin-cb4jaj
#   bash scripts/gcp/apps_setup.sh
#
# It makes:
#   1. the private bucket APPS_BUCKET (deploy/gcp/settings.env) - nobody on
#      the internet can read it; the website reads it as arfa-run;
#   2. arfa-github, the account the GitHub build signs in as, allowed to
#      write to that bucket and nothing else;
#   3. keyless sign-in for GitHub (Workload Identity Federation): only GitHub
#      Actions runs of this one repository may act as arfa-github, so no key
#      or password is ever stored on GitHub;
#   4. for arfa-run (the website): reading the bucket, and asking Google to
#      sign its short-lived download links (no key file either).
# Safe to run again: whatever already exists is left as it is.
set -euo pipefail
cd "$(dirname "$0")/../.."
source deploy/gcp/settings.env

REPO_SLUG="abdulkadhersoofi-dot/pearl_city_biriyani"
POOL="github"
PROVIDER="github"
UPLOADER="arfa-github@$PROJECT_ID.iam.gserviceaccount.com"
RUN_SA="arfa-run@$PROJECT_ID.iam.gserviceaccount.com"
: "${APPS_BUCKET:?APPS_BUCKET is not set in deploy/gcp/settings.env}"

gcloud config set project "$PROJECT_ID" --quiet >/dev/null
NUMBER=$(gcloud projects describe "$PROJECT_ID" --format 'value(projectNumber)')
step() { printf '\n== %s\n' "$1"; }

step "1/5 The services this needs"
gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com storage.googleapis.com --quiet

step "2/5 The private apps bucket gs://$APPS_BUCKET"
if ! gcloud storage buckets describe "gs://$APPS_BUCKET" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://$APPS_BUCKET" --location "$REGION" \
    --uniform-bucket-level-access --public-access-prevention
fi
gcloud storage buckets add-iam-policy-binding "gs://$APPS_BUCKET" --member "serviceAccount:$RUN_SA" \
  --role roles/storage.objectViewer --quiet >/dev/null

step "3/5 arfa-github - may only write the apps into that bucket"
gcloud iam service-accounts describe "$UPLOADER" >/dev/null 2>&1 || \
  gcloud iam service-accounts create arfa-github --display-name "ARFA Android builds (GitHub) - uploads the apps"
gcloud storage buckets add-iam-policy-binding "gs://$APPS_BUCKET" --member "serviceAccount:$UPLOADER" \
  --role roles/storage.objectAdmin --quiet >/dev/null

step "4/5 Keyless sign-in for GitHub Actions of $REPO_SLUG only"
gcloud iam workload-identity-pools describe "$POOL" --location global >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools create "$POOL" --location global --display-name "GitHub Actions"
gcloud iam workload-identity-pools providers describe "$PROVIDER" --location global \
    --workload-identity-pool "$POOL" >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --location global \
    --workload-identity-pool "$POOL" --display-name "GitHub" \
    --issuer-uri "https://token.actions.githubusercontent.com" \
    --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition "assertion.repository == '$REPO_SLUG'"
gcloud iam service-accounts add-iam-policy-binding "$UPLOADER" --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$NUMBER/locations/global/workloadIdentityPools/$POOL/attribute.repository/$REPO_SLUG" \
  --quiet >/dev/null

step "5/5 The website may have its download links signed"
gcloud iam service-accounts add-iam-policy-binding "$RUN_SA" --member "serviceAccount:$RUN_SA" \
  --role roles/iam.serviceAccountTokenCreator --quiet >/dev/null

WANT="projects/$NUMBER/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER"
if ! grep -q "$WANT" .github/workflows/android-apk.yml; then
  echo
  echo "!! .github/workflows/android-apk.yml names another sign-in provider - set GCP_WORKLOAD_PROVIDER there to:"
  echo "   $WANT"
fi
echo
echo "Done. The next Android build puts the apps in gs://$APPS_BUCKET, and arfa.live/apps hands them out."
