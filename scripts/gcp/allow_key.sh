#!/usr/bin/env bash
# For an account in a Google Workspace / Cloud Identity organisation, where
# new organisations block service-account keys by default
# (iam.disableServiceAccountKeyCreation). Lifts that block for THIS ONE
# PROJECT only - the rest of the organisation stays as it is - so
# bootstrap.sh can make Claude's key. Run once, in Cloud Shell, by the owner:
#   bash scripts/gcp/allow_key.sh YOUR_PROJECT_ID
set -euo pipefail
PROJECT_ID=${1:?"usage: bash scripts/gcp/allow_key.sh YOUR_PROJECT_ID"}
ME=$(gcloud config get-value account 2>/dev/null)
ORG=$(gcloud projects get-ancestors "$PROJECT_ID" --format='value(id,type)' | awk '$2 == "organization" {print $1}')
if [ -z "$ORG" ]; then
  echo "This project isn't in an organisation, so the block comes from somewhere else - send this output to Claude."
  exit 1
fi

echo "== Letting $ME change organisation policies (organisation $ORG)"
gcloud organizations add-iam-policy-binding "$ORG" --member "user:$ME" --role roles/orgpolicy.policyAdmin \
  --condition=None --quiet >/dev/null

echo "== Allowing service-account keys in project $PROJECT_ID only"
allow() {   # allow CONSTRAINT - "not enforced" for this project
  printf 'name: projects/%s/policies/%s\nspec:\n  rules:\n  - enforce: false\n' "$PROJECT_ID" "$1" > /tmp/arfa-allow.yaml
  gcloud org-policies set-policy /tmp/arfa-allow.yaml >/dev/null
}
for attempt in 1 2 3 4 5 6; do
  if allow iam.disableServiceAccountKeyCreation; then
    allow iam.managed.disableServiceAccountKeyCreation 2>/dev/null || true   # the newer form, where it exists
    echo "Done - for project $PROJECT_ID only. Wait 2 minutes, then run bootstrap.sh again."
    exit 0
  fi
  echo "  (the new permission takes a moment to apply - trying again in 20 seconds)"
  sleep 20
done
echo "Still refused. Send Claude the lines above (they contain no keys)."
exit 1
