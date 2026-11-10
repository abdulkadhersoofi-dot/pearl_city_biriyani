"""Security headers on every response - what a browser should and shouldn't
allow a page from ARFA to do.

  Strict-Transport-Security   HTTPS only, for a year (sent only over HTTPS;
                              includeSubDomains only on PUBLIC_URL's own
                              domain - never on a client's sign-in domain,
                              where it would bind the client's other sites)
  Content-Security-Policy     pages run only ARFA's own scripts (files from
                              /static, and inline blocks carrying this
                              response's nonce - {{ csp_nonce() }}), talk only
                              to ARFA (connect-src), load images and fonts
                              only from ARFA, can't be framed by another site,
                              and post forms only to ARFA. Inline event
                              handlers (onclick="...") are refused - pages use
                              data- attributes and static/js/actions.js.
                              'unsafe-eval' stays: Alpine.js evaluates its
                              x-/@ attributes that way. Violations are posted
                              to /csp-report and logged as security events.
  X-Content-Type-Options      nosniff - a file is only what it says it is
  X-Frame-Options             SAMEORIGIN (the receipt print frames are ARFA's own)
  Referrer-Policy             strict-origin-when-cross-origin
  Permissions-Policy          no microphone, location, payment or USB; the
                              camera for ARFA itself only
  Cross-Origin-Opener-Policy  same-origin-allow-popups (the print and WhatsApp
                              windows still open)

CSP_MODE: "enforce" (default), "report" (Report-Only: logged, not blocked -
for trying a change), "off".
"""

from __future__ import annotations

import json
import secrets
from urllib.parse import urlsplit

from flask import Response, current_app, has_request_context, request

HSTS_SECONDS = 31536000
REPORT_PATH = "/csp-report"
MAX_REPORT_BYTES = 8192


def csp_nonce() -> str:
    """This response's script nonce (one per request); "" outside a request
    (a PDF or an email rendered in a job has no page to protect)."""
    if not has_request_context():
        return ""
    # Kept on the request itself, not on g: g belongs to the app context,
    # which can outlive one request (a test, a CLI command serving several).
    nonce = request.environ.get("arfa.csp_nonce")
    if nonce is None:
        nonce = request.environ["arfa.csp_nonce"] = secrets.token_urlsafe(18)
    return nonce


def _origin(url: str | None) -> str | None:
    parts = urlsplit(url or "")
    if parts.scheme in ("http", "https") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return None


def content_security_policy(nonce: str) -> str:
    form_targets = ["'self'"]
    public = _origin(current_app.config.get("PUBLIC_URL"))
    if public:
        # A client's sign-in domain hands the sign-in over to the main address
        # by a POST (app/auth/login_domain.py).
        form_targets.append(public)
    directives = [
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}' 'unsafe-eval'",
        "script-src-attr 'none'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "connect-src 'self'",
        "media-src 'self' blob:",
        "worker-src 'self'",
        "manifest-src 'self'",
        "frame-src 'self' blob:",
        "frame-ancestors 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action " + " ".join(form_targets),
        f"report-uri {REPORT_PATH}",
    ]
    return "; ".join(directives)


def _hsts(resp: Response) -> None:
    if not request.is_secure or current_app.debug:
        return
    value = f"max-age={HSTS_SECONDS}"
    main = (urlsplit(current_app.config.get("PUBLIC_URL") or "").hostname or "").lower()
    host = (request.host or "").lower().split(":")[0]
    if main and (host == main or host.endswith("." + main)):
        value += "; includeSubDomains"
    resp.headers.setdefault("Strict-Transport-Security", value)


def init_security_headers(app) -> None:
    app.jinja_env.globals["csp_nonce"] = csp_nonce

    @app.after_request
    def _security_headers(resp: Response) -> Response:
        headers = resp.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault("Permissions-Policy",
                           "camera=(self), microphone=(), geolocation=(), payment=(), usb=(), "
                           "browsing-topics=()")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin-allow-popups")
        _hsts(resp)
        mode = (app.config.get("CSP_MODE") or "enforce").lower()
        if mode != "off" and resp.mimetype == "text/html":
            name = "Content-Security-Policy-Report-Only" if mode == "report" else "Content-Security-Policy"
            headers.setdefault(name, content_security_policy(csp_nonce()))
        return resp

    from app.extensions import csrf

    @app.route(REPORT_PATH, methods=["POST"])
    @csrf.exempt
    def csp_report():
        """Where browsers post what the policy blocked - logged, never stored."""
        from app.monitoring import security_event

        raw = request.get_data(cache=False)[:MAX_REPORT_BYTES]
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return "", 204
        report = body.get("csp-report") if isinstance(body, dict) else None
        if isinstance(report, dict):
            security_event("csp_violation",
                           directive=report.get("effective-directive") or report.get("violated-directive"),
                           blocked=_strip_query(report.get("blocked-uri")),
                           page=_strip_query(report.get("document-uri")),
                           line=report.get("line-number"))
        return "", 204


def _strip_query(url) -> str:
    text = str(url or "")
    return text.split("?", 1)[0].split("#", 1)[0][:200]
