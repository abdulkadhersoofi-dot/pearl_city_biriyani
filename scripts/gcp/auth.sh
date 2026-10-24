#!/usr/bin/env bash
# Signs this shell's gcloud (and firebase-tools) in as claude-deployer, from
# the key stored in the Claude Code environment as GCP_SA_KEY_JSON. Never
# writes the key into the repository.
#   source scripts/gcp/auth.sh
if [ -z "${GCP_SA_KEY_JSON:-}" ]; then
  echo "GCP_SA_KEY_JSON isn't set in this environment (docs/cloud-setup.md, step 6)." >&2
  return 1 2>/dev/null || exit 1
fi
KEY_FILE="${HOME}/.config/arfa/claude-deployer.json"
mkdir -p "$(dirname "$KEY_FILE")"
umask 077
printf '%s' "$GCP_SA_KEY_JSON" > "$KEY_FILE"
# A placeholder token set by the environment would win over the key.
unset CLOUDSDK_AUTH_ACCESS_TOKEN
gcloud auth activate-service-account --key-file "$KEY_FILE" --quiet
export GOOGLE_APPLICATION_CREDENTIALS="$KEY_FILE"
source "$(dirname "${BASH_SOURCE[0]}")/../../deploy/gcp/settings.env"
gcloud config set project "$PROJECT_ID" --quiet >/dev/null 2>&1
echo "Signed in as $(gcloud config get-value account 2>/dev/null) on $PROJECT_ID"
