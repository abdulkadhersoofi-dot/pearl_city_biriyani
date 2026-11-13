"""Request rate limits for the whole app: one person, phone or network can't
flood ARFA, guess passwords at speed, or tie up the servers building PDFs.

Every request falls in one bucket (the first that matches):

  exempt   /static, the health check, the counter's service worker
  auth     a POST that signs in or proves who you are - the sign-in form,
           two-step codes, the sign-in hand-over, changing a password, the
           phone apps' sign-in / PIN / token refresh, pairing a counter.
           Per network (IP). On top of the per-account lockout
           (app/auth/security.py), which stays.
  report   browsers' CSP reports (/csp-report), per network
  heavy    PDFs, spreadsheets and ZIPs (CPU-heavy to build), per login
  api      everything else under /api/ (the phone apps, the counter's sync),
           per app login (its bearer token), else per network
  web      every other page, per signed-in login, else per network

Each bucket allows its RATE_LIMIT_<BUCKET> requests a minute, as a steady
rate with that many in a burst (GCRA - one number kept per caller), so a
page with many images or a quick run of scans never trips it; a script
hammering the server does. Over it: 429 with Retry-After (JSON under /api/
and for fetch/XHR calls, a short page otherwise), and one security event
per caller a minute (app/monitoring.py) - a burst raises an alert.

The counts live in each server process's memory, so the limit is per process:
on Cloud Run (2 processes on each of 2-9 servers, requests spread between
them) a caller gets at most a few times the number before every process
refuses. That is enough to stop floods and fast password guessing; the
firm-wide cap on failed sign-ins (LOGIN_IP_LIMIT) is kept in the database.

RATE_LIMITS_ENABLED=0 switches it off (tests do, except the tests of this).
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass

from flask import current_app, jsonify, request, session

BUCKETS = ("auth", "report", "heavy", "api", "web")
DEFAULT_PER_MINUTE = {"auth": 30, "report": 30, "heavy": 40, "api": 300, "web": 600}

_EXEMPT_ENDPOINTS = {"static", "main.healthz", "pos.service_worker"}
_AUTH_PATH = re.compile(
    r"^/(?:auth/(?:login(?:/[^/]+)?|handoff|two-factor(?:/setup)?|change-password)"
    r"|app/(?:pair|resume)"
    r"|api/(?:scan|wh)/v1/auth/(?:login|two-factor|pin|refresh))/?$")
_HEAVY_PATH = re.compile(r"(?:\.(?:pdf|xlsx|zip|csv)|/pdf|/download)$")
_MAX_KEYS = 50000
_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Please wait - ARFA</title>
<link rel="stylesheet" href="/static/css/app.css"></head><body class="no-menu">
<main class="empty-state"><h1>Too many requests</h1>
<p>Please wait {seconds} second(s) and try again.</p><a class="btn btn-secondary" href="/">Go back</a></main>
</body></html>"""


@dataclass
class Verdict:
    allowed: bool
    bucket: str
    retry_after: int = 0


class Limiter:
    """GCRA (the generic cell rate algorithm): per key, one number - the
    "theoretical arrival time" the caller's next request is due by at the
    steady rate. Thread-safe; one per process."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tat: dict[str, float] = {}
        self._noted: dict[str, float] = {}

    def hit(self, key: str, per_minute: int, now: float | None = None) -> tuple[bool, float]:
        """(allowed, seconds to wait when not)."""
        if per_minute <= 0:
            return True, 0.0
        now = time.monotonic() if now is None else now
        interval = 60.0 / per_minute
        burst = 60.0           # a full minute's allowance may come at once
        with self._lock:
            tat = max(self._tat.get(key, now), now)
            new_tat = tat + interval
            # 1e-6: (now + 60) - now isn't always exactly 60 in floating point,
            # which refused the last request of a full allowance now and then.
            if new_tat - now > burst + 1e-6:
                return False, new_tat - now - burst
            self._tat[key] = new_tat
            if len(self._tat) > _MAX_KEYS:
                self._prune(now)
            return True, 0.0

    def first_refusal(self, key: str, now: float | None = None) -> bool:
        """True once a minute per key - so a flood is one log line, not thousands."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if now - self._noted.get(key, -1e9) < 60:
                return False
            self._noted[key] = now
            if len(self._noted) > _MAX_KEYS:
                self._noted = {k: v for k, v in self._noted.items() if now - v < 60}
            return True

    def _prune(self, now: float) -> None:
        self._tat = {k: v for k, v in self._tat.items() if v > now}

    def reset(self) -> None:
        with self._lock:
            self._tat.clear()
            self._noted.clear()


limiter = Limiter()


def bucket_for(path: str, method: str, endpoint: str | None) -> str | None:
    if endpoint in _EXEMPT_ENDPOINTS or path.startswith("/static/") or method == "OPTIONS":
        return None
    if method == "POST" and _AUTH_PATH.match(path):
        return "auth"
    if path == "/csp-report":
        return "report"
    if _HEAVY_PATH.search(path):
        return "heavy"
    if path.startswith("/api/"):
        return "api"
    return "web"


def _caller(bucket: str) -> str:
    ip = request.remote_addr or "?"
    if bucket in ("auth", "report"):
        return f"ip:{ip}"
    auth = request.headers.get("Authorization") or ""
    if auth:
        return "tok:" + hashlib.sha256(auth.encode()).hexdigest()[:20]
    user_id = session.get("_user_id")
    if user_id:
        return f"user:{user_id}"
    return f"ip:{ip}"


def per_minute(bucket: str) -> int:
    return int(current_app.config.get(f"RATE_LIMIT_{bucket.upper()}", DEFAULT_PER_MINUTE[bucket]))


def check() -> Verdict | None:
    """The verdict for the current request (None = not limited at all)."""
    bucket = bucket_for(request.path, request.method, request.endpoint)
    if bucket is None:
        return None
    caller = _caller(bucket)
    allowed, wait = limiter.hit(f"{bucket}|{caller}", per_minute(bucket))
    if allowed:
        return Verdict(True, bucket)
    if limiter.first_refusal(f"{bucket}|{caller}"):
        from app.monitoring import security_event

        security_event("rate_limited", bucket=bucket, caller=caller.split(":", 1)[0],
                       user_id=caller.split(":", 1)[1] if caller.startswith("user:") else None,
                       path=request.path, limit=per_minute(bucket))
    return Verdict(False, bucket, max(1, int(wait + 0.999)))


def _too_many(verdict: Verdict):
    message = "Too many requests - please wait a moment and try again."
    wants_json = (request.path.startswith("/api/") or request.is_json
                  or request.headers.get("X-Requested-With") == "XMLHttpRequest"
                  or "application/json" in (request.headers.get("Accept") or ""))
    if wants_json:
        resp = jsonify({"error": message, "retry_after": verdict.retry_after})
    else:
        # A plain page, not a template: a flood shouldn't cost a database
        # query per refusal (base.html's menus do several).
        resp = current_app.make_response(_PAGE.format(seconds=verdict.retry_after))
    resp.status_code = 429
    resp.headers["Retry-After"] = str(verdict.retry_after)
    return resp


def init_rate_limits(app) -> None:
    for bucket in BUCKETS:
        app.config.setdefault(f"RATE_LIMIT_{bucket.upper()}", DEFAULT_PER_MINUTE[bucket])

    @app.before_request
    def _rate_limit():
        if not app.config.get("RATE_LIMITS_ENABLED", True):
            return None
        verdict = check()
        if verdict is None or verdict.allowed:
            return None
        return _too_many(verdict)
