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

# Best-effort, not fatal: free tier also has no Shell tab to run this
# by hand, so it happens here instead. A failure here (e.g. a bad env
# var) must never take the whole app down - `|| true` keeps the site up
# even if this one step has a problem.
flask bootstrap-super-admin || true

exec gunicorn -c deploy/gunicorn.conf.py wsgi:app
