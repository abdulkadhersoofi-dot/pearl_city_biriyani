#!/usr/bin/env bash
# Secrets in Secret Manager (never in the repository):
#   scripts/gcp/secret.sh list                  which exist and when each was last changed
#   scripts/gcp/secret.sh read NAME             print the current value
#   scripts/gcp/secret.sh set NAME              store a new value, read from the keyboard / a pipe
#   scripts/gcp/secret.sh rotate NAME           a new random value (SECRET_KEY-style secrets only)
# A new value reaches the app on its next deploy (scripts/gcp/deploy.sh).
# Never rotate arfa-einvoice-key: saved e-invoice logins are encrypted with
# it. Rotating arfa-secret-key signs everyone out and changes every image
# address (the old ones stop working); that is all.
set -euo pipefail
cd "$(dirname "$0")/../.."
source deploy/gcp/settings.env
P=(--project "$PROJECT_ID")
case "${1:-list}" in
  list)   gcloud secrets list "${P[@]}" --format 'table(name.basename(), createTime.date("%d %b %Y"))'
          for s in $(gcloud secrets list "${P[@]}" --format 'value(name.basename())'); do
            printf '%-28s latest: %s\n' "$s" "$(gcloud secrets versions list "$s" "${P[@]}" --filter state=ENABLED \
              --limit 1 --format 'value(createTime.date("%d %b %Y %H:%M"))' 2>/dev/null || echo -)"
          done ;;
  read)   gcloud secrets versions access latest --secret "${2:?name}" "${P[@]}" ;;
  set)    gcloud secrets versions add "${2:?name}" "${P[@]}" --data-file=- ;;
  rotate) case "${2:?name}" in
            arfa-secret-key|arfa-secret-key-staging) ;;
            *) echo "Only arfa-secret-key(-staging) rotates to a random value; set the others with 'set'." >&2; exit 2 ;;
          esac
          python3 -c "import secrets; print(secrets.token_urlsafe(48), end='')" | \
            gcloud secrets versions add "$2" "${P[@]}" --data-file=- ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
