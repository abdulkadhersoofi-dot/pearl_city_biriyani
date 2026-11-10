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
# Threads per worker (gthread): each thread serves one request at a time
# and holds one database connection while it does - keep DB_POOL_SIZE at
# this number. Cloud Run runs 2 workers x 4 threads with its concurrency
# set to 8, so a request never queues inside the container. 1 thread is
# the plain one-request-per-worker behaviour (Render).
threads = int(os.environ.get("GUNICORN_THREADS", "1"))
worker_class = "gthread" if threads > 1 else "sync"
# Under Firebase Hosting's 60-second limit, so a stuck request ends here
# (and is logged) rather than as an anonymous 504 at the edge.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "55"))
graceful_timeout = 30
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

# Every worker is stateless (sessions live in Redis, not memory), so scaling
# workers - or running this behind multiple app servers - never fragments
# who-is-logged-in-where.
preload_app = True

# On Google Cloud Run, gunicorn's own lines (worker timeouts, crashes, the
# access log) are JSON too - the same shape as the app's (app/monitoring.py),
# so each carries its real severity and a worker timeout counts as an error.
# LOG_FORMAT=text keeps them plain.
if os.environ.get("K_SERVICE") and (os.environ.get("LOG_FORMAT") or "json").lower() != "text":
    logconfig_dict = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"json": {"()": "app.monitoring.JsonFormatter"}},
        "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json",
                                 "stream": "ext://sys.stderr"}},
        "root": {"level": "WARNING", "handlers": ["console"]},
        "loggers": {
            "gunicorn.error": {"level": "INFO", "handlers": ["console"], "propagate": False},
            "gunicorn.access": {"level": "INFO", "handlers": ["console"], "propagate": False},
        },
    }
