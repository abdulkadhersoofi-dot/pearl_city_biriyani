"""Security hardening: browser security headers and the CSP (app/security_headers.py),
request rate limits (app/ratelimit.py) and structured logs / security events
(app/monitoring.py)."""

import json
import logging
import re
import subprocess
from pathlib import Path

import pytest

from app import ratelimit
from app.monitoring import JsonFormatter, json_logs_wanted, security_event

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


@pytest.fixture(autouse=True)
def _fresh_limiter():
    ratelimit.limiter.reset()
    yield
    ratelimit.limiter.reset()


# --- headers -----------------------------------------------------------------------------------

def test_every_page_carries_the_security_headers(client, db):
    resp = client.get("/auth/login")
    h = resp.headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "SAMEORIGIN"
    assert h["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "microphone=()" in h["Permissions-Policy"] and "camera=(self)" in h["Permissions-Policy"]
    assert h["Cross-Origin-Opener-Policy"] == "same-origin-allow-popups"
    csp = h["Content-Security-Policy"]
    for directive in ("default-src 'self'", "object-src 'none'", "frame-ancestors 'self'", "base-uri 'self'",
                      "connect-src 'self'", "script-src-attr 'none'", "report-uri /csp-report"):
        assert directive in csp
    assert "'unsafe-inline'" not in csp.split("script-src ", 1)[1].split(";", 1)[0]
    # Plain http (a laptop): no HSTS - it would pin the browser to https.
    assert "Strict-Transport-Security" not in h


def test_inline_scripts_carry_this_responses_nonce(client, db):
    resp = client.get("/auth/login")
    nonce = re.search(r"'nonce-([^']+)'", resp.headers["Content-Security-Policy"]).group(1)
    html = resp.get_data(as_text=True)
    tags = re.findall(r"<script(?![^>]*\bsrc=)(?![^>]*application/json)([^>]*)>", html)
    assert tags and all(f'nonce="{nonce}"' in t for t in tags)
    # A new nonce every response.
    again = client.get("/auth/login").headers["Content-Security-Policy"]
    assert nonce not in again


def test_hsts_over_https_with_subdomains_only_on_the_main_address(app, client, db):
    app.config["PUBLIC_URL"] = "https://arfa.live"
    main = client.get("/auth/login", base_url="https://arfa.live")
    assert main.headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"
    other = client.get("/health", base_url="https://login.someclient.in")
    assert other.headers["Strict-Transport-Security"] == "max-age=31536000"


def test_csp_only_on_pages_and_can_be_report_only_or_off(app, client, db):
    assert "Content-Security-Policy" not in client.get("/health").headers      # JSON
    app.config["CSP_MODE"] = "report"
    h = client.get("/auth/login").headers
    assert "Content-Security-Policy-Report-Only" in h and "Content-Security-Policy" not in h
    app.config["CSP_MODE"] = "off"
    h = client.get("/auth/login").headers
    assert "Content-Security-Policy" not in h and "Content-Security-Policy-Report-Only" not in h
    assert h["X-Frame-Options"] == "SAMEORIGIN"


def test_form_action_allows_the_sign_in_hand_over_to_the_main_address(app, client, db):
    app.config["PUBLIC_URL"] = "https://arfa.live/"
    csp = client.get("/auth/login").headers["Content-Security-Policy"]
    assert "form-action 'self' https://arfa.live;" in csp


def test_no_template_uses_inline_event_handlers_or_an_unnonced_script():
    handlers, bare = [], []
    for f in TEMPLATES.rglob("*.html"):
        text = f.read_text()
        if re.search(r"""\son[a-z]+\s*=\s*["']""", text):
            handlers.append(f.name)
        for attrs in re.findall(r"<script(?![^>]*\bsrc=)([^>]*)>", text):
            if "application/json" not in attrs and "csp_nonce()" not in attrs:
                bare.append(f.name)
    assert not handlers, handlers
    assert not bare, bare


def test_the_data_attribute_behaviours_are_loaded_everywhere():
    for layout in ("base.html", "auth_base.html"):
        assert "js/actions.js" in (TEMPLATES / layout).read_text()
    js = (ROOT / "app/static/js/actions.js").read_text()
    for attr in ("data-autosubmit", "data-locked", "data-print", "data-share", "data-check-all",
                 "data-clear-inputs", "data-fill-oldest", "data-split-into", "data-shows"):
        assert attr in js


def test_csp_reports_are_logged_as_security_events(client, db, caplog):
    report = {"csp-report": {"document-uri": "https://arfa.live/invoices/?token=secret",
                             "effective-directive": "script-src-elem", "blocked-uri": "inline", "line-number": 5}}
    with caplog.at_level(logging.WARNING, logger="arfa.security"):
        resp = client.post("/csp-report", data=json.dumps(report), content_type="application/csp-report")
    assert resp.status_code == 204
    rec = next(r for r in caplog.records if getattr(r, "security_event", None) == "csp_violation")
    assert rec.details["page"] == "https://arfa.live/invoices/"              # query string dropped
    assert rec.details["directive"] == "script-src-elem"
    assert client.post("/csp-report", data="not json").status_code == 204


# --- rate limits -------------------------------------------------------------------------------

def test_gcra_allows_a_burst_then_refuses_with_a_wait():
    lim = ratelimit.Limiter()
    results = [lim.hit("k", 6, now=100.0) for _ in range(7)]
    assert all(ok for ok, _ in results[:6])
    ok, wait = results[6]
    assert not ok and 9 < wait <= 10.01                  # one more allowed every 10 s at 6 a minute
    assert lim.hit("k", 6, now=111.0)[0]                 # ... and it comes back
    assert lim.hit("other", 6, now=100.0)[0]             # callers are separate
    assert lim.hit("k", 0, now=100.0)[0]                 # 0 = no limit


def test_requests_fall_into_buckets():
    b = ratelimit.bucket_for
    assert b("/static/css/app.css", "GET", "static") is None
    assert b("/health", "GET", "main.healthz") is None
    assert b("/auth/login", "POST", "auth.login") == "auth"
    assert b("/auth/login", "GET", "auth.login") == "web"
    assert b("/auth/login/acme", "POST", "auth.login_slug") == "auth"
    assert b("/api/wh/v1/auth/pin", "POST", None) == "auth"
    assert b("/api/scan/v1/auth/two-factor", "POST", None) == "auth"
    assert b("/app/pair", "POST", None) == "auth"
    assert b("/csp-report", "POST", "csp_report") == "report"
    assert b("/invoices/12.pdf", "GET", None) == "heavy"
    assert b("/reports/gstr1.xlsx", "GET", None) == "heavy"
    assert b("/api/wh/v1/pipeline", "GET", None) == "api"
    assert b("/invoices/", "GET", None) == "web"
    assert b("/invoices/", "OPTIONS", None) is None


def test_fast_sign_in_attempts_get_429_with_retry_after(app, client, db, caplog):
    app.config.update(RATE_LIMITS_ENABLED=True, RATE_LIMIT_AUTH=3)
    codes = [client.post("/auth/login", data={"email": "x@example.com", "password": "nope"}).status_code
             for _ in range(3)]
    assert 429 not in codes
    with caplog.at_level(logging.WARNING, logger="arfa.security"):
        resp = client.post("/auth/login", data={"email": "x@example.com", "password": "nope"})
        client.post("/auth/login", data={"email": "x@example.com", "password": "nope"})
    assert resp.status_code == 429 and int(resp.headers["Retry-After"]) >= 1
    assert "Too many requests" in resp.get_data(as_text=True)
    events = [r for r in caplog.records if getattr(r, "security_event", None) == "rate_limited"]
    assert len(events) == 1 and events[0].details["bucket"] == "auth"      # one line per caller a minute
    assert client.get("/auth/login").status_code == 200                    # reading the page is another bucket
    assert client.get("/health").status_code == 200


def test_api_floods_get_json_429_per_token(app, client, db):
    app.config.update(RATE_LIMITS_ENABLED=True, RATE_LIMIT_API=2)
    a = {"Authorization": "Bearer aaa"}
    assert [client.get("/api/wh/v1/me", headers=a).status_code == 429 for _ in range(3)] == [False, False, True]
    resp = client.get("/api/wh/v1/me", headers=a)
    assert resp.status_code == 429 and resp.get_json()["retry_after"] >= 1
    assert client.get("/api/wh/v1/me", headers={"Authorization": "Bearer bbb"}).status_code != 429


def test_signed_in_logins_are_limited_one_by_one(app, client, db, tenant, client_admin, staff):
    app.config.update(RATE_LIMITS_ENABLED=True, RATE_LIMIT_WEB=50, RATE_LIMIT_AUTH=50)
    _login(client, "admin@acme.example.com", "ClientSecret123")
    other = app.test_client()
    _login(other, "cashier@acme.example.com", "CashierSecret123")
    app.config["RATE_LIMIT_WEB"] = 1
    ratelimit.limiter.reset()
    assert client.get("/invoices/").status_code == 200
    assert client.get("/invoices/").status_code == 429
    assert other.get("/pos/").status_code != 429               # the cashier is someone else


# --- logs and security events ------------------------------------------------------------------

def _record(msg="hello", level=logging.WARNING, **extra):
    rec = logging.LogRecord("x", level, __file__, 1, msg, (), None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_json_log_lines_carry_severity_and_security_events():
    line = json.loads(JsonFormatter().format(_record(security_event="rate_limited", details={"bucket": "api"})))
    assert line["severity"] == "WARNING" and line["message"] == "hello"
    assert line["security_event"] == "rate_limited" and line["details"] == {"bucket": "api"}


def test_errors_with_a_stack_trace_are_marked_for_error_reporting():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = _record("failed", logging.ERROR)
        rec.exc_info = sys.exc_info()
    line = json.loads(JsonFormatter().format(rec))
    assert line["severity"] == "ERROR"
    assert line["@type"].endswith("ReportedErrorEvent")
    assert "ValueError: boom" in line["message"] and "Traceback" in line["message"]


def test_json_lines_name_the_request_without_its_query_string(app):
    with app.test_request_context("/invoices/?token=secret", headers={"X-Cloud-Trace-Context": "abc123/1;o=1"}):
        app.config["GCP_PROJECT"] = "p"
        import os

        os.environ["GOOGLE_CLOUD_PROJECT"] = "p"
        try:
            line = json.loads(JsonFormatter().format(_record()))
        finally:
            del os.environ["GOOGLE_CLOUD_PROJECT"]
    assert line["httpRequest"]["requestUrl"] == "/invoices/"
    assert line["logging.googleapis.com/trace"] == "projects/p/traces/abc123"


def test_json_logs_on_cloud_run_only(monkeypatch):
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert not json_logs_wanted()
    monkeypatch.setenv("K_SERVICE", "arfa-web")
    assert json_logs_wanted()
    monkeypatch.setenv("LOG_FORMAT", "text")
    assert not json_logs_wanted()


def test_failed_sign_ins_become_security_events_without_the_email(client, db, caplog):
    with caplog.at_level(logging.WARNING, logger="arfa.security"):
        _login(client, "nobody@example.com", "wrong")
    rec = next(r for r in caplog.records if getattr(r, "security_event", None) == "login_failed")
    assert "nobody@example.com" not in rec.getMessage() and "nobody@example.com" not in json.dumps(rec.details)


def test_security_event_details_are_short_strings():
    rec = []
    handler = logging.Handler()
    handler.emit = rec.append
    logger = logging.getLogger("arfa.security")
    logger.addHandler(handler)
    try:
        security_event("test", path="x" * 500, n=3)
    finally:
        logger.removeHandler(handler)
    assert len(rec[0].details["path"]) == 200 and rec[0].details["n"] == 3


# --- the alert setup -----------------------------------------------------------------------------

def test_monitoring_setup_script_is_valid_and_covers_the_alerts():
    script = ROOT / "scripts/gcp/monitoring_setup.sh"
    assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0
    text = script.read_text()
    for name in ("ARFA website down", "ARFA server errors (5xx)", "ARFA slow responses", "ARFA near its server limit",
                 "ARFA app errors", "ARFA security events burst", "ARFA database CPU high",
                 "ARFA database connections high", "ARFA scheduled job failed", "arfa_security_events"):
        assert name in text
    assert "@" not in text.split("set -euo pipefail", 1)[1].split("step ", 1)[0]   # no address baked in


def test_a_form_without_its_csrf_token_is_a_security_event(app, client, db, caplog):
    app.config["WTF_CSRF_ENABLED"] = True
    with caplog.at_level(logging.WARNING, logger="arfa.security"):
        resp = client.post("/auth/login", data={"email": "a@b.c", "password": "x"})
    assert resp.status_code == 400
    assert any(getattr(r, "security_event", None) == "csrf_failed" for r in caplog.records)


def test_the_rate_limit_runs_before_the_csrf_check(app, client, db):
    app.config.update(WTF_CSRF_ENABLED=True, RATE_LIMITS_ENABLED=True, RATE_LIMIT_AUTH=1)
    codes = [client.post("/auth/login", data={}).status_code for _ in range(3)]
    assert codes == [400, 429, 429]
