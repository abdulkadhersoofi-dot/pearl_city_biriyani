import multiprocessing
import os

# Render (and most PaaS targets) inject PORT and expect the app to bind
# 0.0.0.0:$PORT; GUNICORN_BIND stays available for a bare VPS/Nginx setup
# where the app should only listen on localhost.
if os.environ.get("PORT"):
    bind = f"0.0.0.0:{os.environ['PORT']}"
else:
    bind = os.environ.get("GUNICORN_BIND", "127.0.0.1:8000")
workers = int(os.environ.get("GUNICORN_WORKERS", multiprocessing.cpu_count() * 2 + 1))
worker_class = "sync"
timeout = 60
graceful_timeout = 30
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

# Every worker is stateless (sessions live in Redis, not memory), so scaling
# workers - or running this behind multiple app servers - never fragments
# who-is-logged-in-where.
preload_app = True
