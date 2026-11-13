#!/usr/bin/env bash
# Monitoring and alerts for ARFA, all inside Google Cloud (Cloud Monitoring,
# Cloud Logging, Error Reporting - nothing leaves the project). Run it once,
# in Cloud Shell, as the project's owner, after pulling this branch, with the
# email address(es) the alerts go to:
#
#   cd pearl_city_biriyani && git pull && bash scripts/gcp/monitoring_setup.sh you@yourfirm.in [other@...]
#
# It sets up:
#   1. the APIs (Monitoring, Logging, Error Reporting);
#   2. an email notification channel per address given;
#   3. log-based metrics - arfa_app_errors (the app's ERROR lines),
#      arfa_security_events (failed sign-ins, lockouts, rate limits, blocked
#      scripts - app/monitoring.py), counted by kind, and
#      arfa_books_check_failed (the nightly Books check);
#   4. an uptime check on PUBLIC_URL/health every minute from around the world;
#   5. alert policies, each emailing with what to check:
#        website down            the uptime check fails
#        server errors           5xx answers above ~15 in 5 minutes
#        slow responses          95% of requests slower than 3 s for 10 minutes
#        near the server limit   running instances close to MAX_INSTANCES
#        app errors              more than 10 error lines in 5 minutes
#        security events         more than 50 in 10 minutes (an attack, or
#                                someone locked out)
#        database CPU / memory / disk / connections high
#        a scheduled job failed  (the nightly run, a migration)
#        books check failed      the nightly check found a business whose
#                                figures don't agree with each other
# Safe to run again: it updates what is there instead of adding copies.
set -euo pipefail
cd "$(dirname "$0")/../.."
source deploy/gcp/settings.env

if [ "$#" -lt 1 ]; then
  sed -n '2,8p' "$0"
  echo "Give at least one email address for the alerts."
  exit 2
fi

gcloud config set project "$PROJECT_ID" --quiet >/dev/null
step() { printf '\n== %s\n' "$1"; }
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
HOST=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.urlsplit(sys.argv[1]).hostname)' "$PUBLIC_URL")

step "1/5 APIs"
gcloud services enable monitoring.googleapis.com logging.googleapis.com clouderrorreporting.googleapis.com --quiet
echo "ok"

step "2/5 Email channels"
CHANNELS=()
for EMAIL in "$@"; do
  NAME=$(gcloud beta monitoring channels list --filter "type=\"email\" AND labels.email_address=\"$EMAIL\"" \
           --format 'value(name)' --limit 1)
  if [ -z "$NAME" ]; then
    NAME=$(gcloud beta monitoring channels create --display-name "ARFA alerts ($EMAIL)" --type email \
             --channel-labels "email_address=$EMAIL" --format 'value(name)')
  fi
  echo "$EMAIL -> $NAME"
  CHANNELS+=("$NAME")
done
CHANNELS_CSV=$(IFS=,; echo "${CHANNELS[*]}")

step "3/5 Log-based metrics"
RUN_LOGS="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$SERVICE\""
cat > "$WORK/arfa_app_errors.yaml" <<EOF
description: ARFA's own error lines (the request log's 5xx lines are counted by the server-errors alert instead)
filter: >-
  $RUN_LOGS AND severity>=ERROR AND NOT logName:"run.googleapis.com%2Frequests"
EOF
cat > "$WORK/arfa_security_events.yaml" <<EOF
description: ARFA security events (app/monitoring.py) - failed sign-ins, lockouts, rate limits, blocked scripts
filter: >-
  $RUN_LOGS AND jsonPayload.security_event:*
labelExtractors:
  kind: EXTRACT(jsonPayload.security_event)
metricDescriptor:
  metricKind: DELTA
  valueType: INT64
  labels:
  - key: kind
    valueType: STRING
    description: what happened (login_failed, rate_limited, csp_violation ...)
EOF
cat > "$WORK/arfa_books_check_failed.yaml" <<EOF
description: The nightly Books check found a business whose figures don't agree (flask books-check, app/finance/checks.py)
filter: >-
  resource.type="cloud_run_job" AND textPayload:"books check"
  AND (textPayload:"books check failed" OR textPayload:"books check could not run")
EOF
for METRIC in arfa_app_errors arfa_security_events arfa_books_check_failed; do
  if gcloud logging metrics describe "$METRIC" >/dev/null 2>&1; then
    gcloud logging metrics update "$METRIC" --config-from-file "$WORK/$METRIC.yaml" --quiet >/dev/null
  else
    gcloud logging metrics create "$METRIC" --config-from-file "$WORK/$METRIC.yaml" --quiet >/dev/null
  fi
  echo "$METRIC ok"
done

step "4/5 Uptime check on $PUBLIC_URL/health"
UPTIME_NAME="ARFA website"
CHECK=$(gcloud monitoring uptime list-configs --filter "displayName=\"$UPTIME_NAME\"" --format 'value(name)' --limit 1)
if [ -z "$CHECK" ]; then
  gcloud monitoring uptime create "$UPTIME_NAME" --resource-type uptime-url \
    --resource-labels "host=$HOST,project_id=$PROJECT_ID" --protocol https --path /health \
    --period 1 --timeout 10 --status-classes 2xx --quiet >/dev/null
  CHECK=$(gcloud monitoring uptime list-configs --filter "displayName=\"$UPTIME_NAME\"" --format 'value(name)' --limit 1)
fi
CHECK_ID=${CHECK##*/}
echo "check $CHECK_ID"

step "5/5 Alert policies"
export PROJECT_ID SERVICE SQL_INSTANCE SQL_MAX_CONNECTIONS MAX_INSTANCES CHECK_ID CHANNELS_CSV WORK PUBLIC_URL
python3 - <<'PY'
import json, os

e = os.environ
channels = [c for c in e["CHANNELS_CSV"].split(",") if c]
run = f'resource.type="cloud_run_revision" AND resource.labels.service_name="{e["SERVICE"]}"'
sql = f'resource.type="cloudsql_database" AND resource.labels.database_id="{e["PROJECT_ID"]}:{e["SQL_INSTANCE"]}"'
logs = "https://console.cloud.google.com/logs/query?project=" + e["PROJECT_ID"]


def policy(key, name, filt, threshold, *, aligner, reducer="REDUCE_SUM", period=300, duration=0,
           group_by=(), comparison="COMPARISON_GT", doc=""):
    agg = {"alignmentPeriod": f"{period}s", "perSeriesAligner": aligner}
    if reducer:
        agg["crossSeriesReducer"] = reducer
        agg["groupByFields"] = list(group_by)
    body = {
        "displayName": name,
        "combiner": "OR",
        "conditions": [{"displayName": name, "conditionThreshold": {
            "filter": filt, "comparison": comparison, "thresholdValue": threshold,
            "duration": f"{duration}s", "aggregations": [agg], "trigger": {"count": 1}}}],
        "notificationChannels": channels,
        "alertStrategy": {"autoClose": "3600s"},
        "documentation": {"mimeType": "text/markdown", "content": doc + f"\n\nLogs: {logs}\n\n"
                          "From Cloud Shell: `bash scripts/gcp/logs.sh errors 1` (the last hour's errors)."},
        "userLabels": {"app": "arfa"},
    }
    with open(os.path.join(e["WORK"], f"policy_{key}.json"), "w") as fh:
        json.dump(body, fh, indent=1)


policy("down", "ARFA website down",
       f'metric.type="monitoring.googleapis.com/uptime_check/check_passed" AND resource.type="uptime_url" '
       f'AND metric.labels.check_id="{e["CHECK_ID"]}"',
       1, aligner="ALIGN_NEXT_OLDER", reducer="REDUCE_COUNT_FALSE", group_by=["resource.label.host"],
       period=300, duration=120,
       doc=f"{e['PUBLIC_URL']}/health has failed from more than one place for 2 minutes - the site or its "
           "database isn't answering. Check Cloud Run (arfa-web) and Cloud SQL (arfa-db) in the console.")
policy("5xx", "ARFA server errors (5xx)",
       f'metric.type="run.googleapis.com/request_count" AND {run} AND metric.labels.response_code_class="5xx"',
       0.05, aligner="ALIGN_RATE",
       doc="More than ~15 requests in 5 minutes ended in a server error. Usually a bug in a new release "
           "(roll back: Cloud Run > arfa-web > Revisions) or the database refusing connections.")
policy("slow", "ARFA slow responses",
       f'metric.type="run.googleapis.com/request_latencies" AND {run}',
       3000, aligner="ALIGN_PERCENTILE_95", reducer="REDUCE_MAX", duration=600,
       doc="For 10 minutes, 1 request in 20 has taken longer than 3 seconds. Look at database CPU and "
           "connections, and at which pages are slow in the request log.")
policy("instances", "ARFA near its server limit",
       f'metric.type="run.googleapis.com/container/instance_count" AND {run} AND metric.labels.state="active"',
       int(e["MAX_INSTANCES"]) - 1.5, aligner="ALIGN_MAX", duration=600,
       doc=f"Cloud Run has been running nearly all {e['MAX_INSTANCES']} allowed servers for 10 minutes - "
           "busy hours or a flood. Past the limit requests queue and fail. Raising MAX_INSTANCES needs a "
           "bigger database first (deploy/gcp/settings.env explains the connection budget).")
policy("app_errors", "ARFA app errors",
       'metric.type="logging.googleapis.com/user/arfa_app_errors" AND resource.type="cloud_run_revision"',
       10, aligner="ALIGN_DELTA",
       doc="More than 10 error lines in 5 minutes. Error Reporting groups them with their stack traces: "
           f"https://console.cloud.google.com/errors?project={e['PROJECT_ID']}")
policy("security", "ARFA security events burst",
       'metric.type="logging.googleapis.com/user/arfa_security_events" AND resource.type="cloud_run_revision"',
       50, aligner="ALIGN_DELTA", period=600, group_by=["metric.label.kind"],
       doc="More than 50 security events of one kind in 10 minutes - password guessing (login_failed, "
           "account_locked), a flood (rate_limited) or a page script being blocked (csp_violation). The log "
           "lines carry the network (httpRequest.remoteIp) and the path. Query: "
           "`jsonPayload.security_event:*`")
policy("books", "ARFA books check failed",
       'metric.type="logging.googleapis.com/user/arfa_books_check_failed" AND resource.type="cloud_run_job"',
       0, aligner="ALIGN_DELTA", period=3600,
       doc="The nightly Books check found a business whose figures don't agree with each other - the dashboard, "
           "the Sales report, the GST working papers, stock or accounts. The log line names the business "
           "(tenant id), the month and the checks. The owner sees the same under Needs you, and Reports > "
           "Books check shows the difference and the records behind it. `bash scripts/gcp/logs.sh job "
           "arfa-nightly` shows the run.")
policy("db_cpu", "ARFA database CPU high",
       f'metric.type="cloudsql.googleapis.com/database/cpu/utilization" AND {sql}',
       0.8, aligner="ALIGN_MEAN", reducer=None, duration=900,
       doc="The database has been above 80% CPU for 15 minutes. Next size up: SQL_TIER in "
           "deploy/gcp/settings.env (db-custom-2-7680).")
policy("db_memory", "ARFA database memory high",
       f'metric.type="cloudsql.googleapis.com/database/memory/utilization" AND {sql}',
       0.9, aligner="ALIGN_MEAN", reducer=None, duration=900,
       doc="The database has used more than 90% of its memory for 15 minutes.")
policy("db_disk", "ARFA database disk nearly full",
       f'metric.type="cloudsql.googleapis.com/database/disk/utilization" AND {sql}',
       0.85, aligner="ALIGN_MAX", reducer=None, duration=300,
       doc="The database disk is over 85% full. It grows by itself, but check it is growing and why.")
policy("db_conn", "ARFA database connections high",
       f'metric.type="cloudsql.googleapis.com/database/postgresql/num_backends" AND {sql}',
       int(int(e["SQL_MAX_CONNECTIONS"]) * 0.8), aligner="ALIGN_MAX", duration=300,
       doc=f"More than 80% of the database's {e['SQL_MAX_CONNECTIONS']} connections are in use - new requests "
           "will soon wait or fail.")
policy("jobs", "ARFA scheduled job failed",
       'metric.type="run.googleapis.com/job/completed_execution_count" AND resource.type="cloud_run_job" '
       'AND metric.labels.result="failed"',
       0, aligner="ALIGN_DELTA", group_by=["resource.label.job_name"],
       doc="A Cloud Run job (the nightly run, a migration) ended in failure. "
           "`bash scripts/gcp/logs.sh job <name>` shows its last run.")
PY

for FILE in "$WORK"/policy_*.json; do
  NAME=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["displayName"])' "$FILE")
  EXISTING=$(gcloud monitoring policies list --filter "displayName=\"$NAME\"" --format 'value(name)' --limit 1)
  for TRY in 1 2 3; do
    if [ -n "$EXISTING" ]; then
      gcloud monitoring policies update "$EXISTING" --policy-from-file "$FILE" --quiet >/dev/null 2>"$WORK/err" && break
    else
      gcloud monitoring policies create --policy-from-file "$FILE" --quiet >/dev/null 2>"$WORK/err" && break
    fi
    # A log-based metric made a moment ago can take a minute to be known to Monitoring.
    [ "$TRY" = 3 ] && { echo "!! $NAME:"; cat "$WORK/err"; continue 2; }
    sleep 30
  done
  echo "$NAME ok"
done

cat <<EOF

Done. Alerts email: $*
One step the command line can't do - email on a NEW kind of error, with its
stack trace (Error Reporting):
  https://console.cloud.google.com/errors?project=$PROJECT_ID
  -> "Configure notifications" -> tick the ARFA alerts channel(s).
See them all: https://console.cloud.google.com/monitoring/alerting?project=$PROJECT_ID
EOF
