#!/bin/sh
# The "migrate" Cloud Run Job (scripts/gcp/release.sh runs it before each
# deploy): brings the database schema up to date, once, before the new
# version takes traffic - never from the web containers themselves.
# Migrations must keep working with the version still serving (add, don't
# remove or rename, in the same release).
set -e
DB_STATEMENT_TIMEOUT_MS=0 flask db upgrade
flask bootstrap-super-admin || true
