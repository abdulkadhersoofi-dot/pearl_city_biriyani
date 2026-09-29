#!/bin/sh
# Render's free tier doesn't support preDeployCommand (a paid-tier-only
# feature that runs a command once before rollout), so migrations run
# here at container startup instead. flask db upgrade is idempotent - a
# no-op once the DB is already current - so this is safe on every
# restart. Only a real concern if this service ever scales past 1
# instance (two containers racing the same migration on cold start);
# free tier runs exactly one, so that's a future-you problem for when
# you upgrade the plan (switch to preDeployCommand at that point instead).
set -e

flask db upgrade
exec gunicorn -c deploy/gunicorn.conf.py wsgi:app
