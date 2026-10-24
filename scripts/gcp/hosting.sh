#!/usr/bin/env bash
# Publishes firebase.json to Firebase Hosting: the staging site always, the
# production site (arfa.live) once production is live. Cloud Build runs it
# after each release; it also runs by hand with the deployer's key.
set -euo pipefail
cd "$(dirname "$0")/../.."
source deploy/gcp/settings.env
ONLY="hosting:staging"
[ "$PRODUCTION_ENABLED" = "1" ] && ONLY="hosting"
npx --yes firebase-tools@15.32.1 deploy --only "$ONLY" --project "$PROJECT_ID" --non-interactive
