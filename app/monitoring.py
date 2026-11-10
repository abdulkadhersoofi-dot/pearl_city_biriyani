"""Monitoring: what the app writes to its logs, and (optionally) Sentry.

Logs. On Google Cloud Run (K_SERVICE is set) or with LOG_FORMAT=json, every
log line is one JSON object Cloud Logging understands: a real severity, the
request it came from (method and path - never the query string, which can
carry a token) and its trace. An error with a stack trace is marked for
Error Reporting, which groups it and can email on a new kind of error.
Elsewhere (a laptop, tests) logs stay plain text.

Security events. security_event("rate_limited", ...) writes one WARNING line
carrying "security_event": "<kind>" - the log-based metric
arfa_security_events counts them and an alert fires on a burst
(scripts/gcp/monitoring_setup.sh). The sign-in failures already written to
the audit log (app.utils.audit) are logged this way too. No email
addresses, names or passwords go in them - a user id at most.

Sentry, only when SENTRY_DSN is set - with no DSN
nothing is imported, initialised or sent anywhere.

What goes to Sentry: unhandled exceptions with their stack trace, the
request URL and the signed-in user's id and role (no email, name or form
data - send_default_pii stays off). Performance tracing is off unless
SENTRY_TRACES_SAMPLE_RATE is set above 0."""

import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone

log = logging.getLogger(__name__)
security_log = logging.getLogger("arfa.security")

# The audit-log actions that are also security events (a burst of them is an
# attack in progress, or someone locked out).
AUDITED_SECURITY_EVENTS = frozenset({
    "login_failed", "account_locked", "login_refused_locked", "two_factor_failed",
    "warehouse_login_failed", "device_pair_failed",
})
_ERROR_EVENT_TYPE = "type.googleapis.com/google.devtools.clouderrorreporting.v1beta1.ReportedErrorEvent"


def json_logs_wanted() -> bool:
    fmt = (os.environ.get("LOG_FORMAT") or "").lower()
    if fmt in ("json", "text"):
        return fmt == "json"
    return bool(os.environ.get("K_SERVICE"))


class JsonFormatter(logging.Formatter):
    """One log record -> one line of JSON for Cloud Logging (structured
    logging: https://cloud.google.com/logging/docs/structured-logging)."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        entry = {
            "severity": _SEVERITY.get(record.levelno, "DEFAULT"),
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "logger": record.name,
        }
        if record.exc_info:
            message = f"{message}\n{''.join(traceback.format_exception(*record.exc_info))}"
            entry["@type"] = _ERROR_EVENT_TYPE
            entry["serviceContext"] = {"service": os.environ.get("K_SERVICE") or "arfa",
                                       "version": os.environ.get("K_REVISION") or _release() or "dev"}
        elif record.levelno >= logging.ERROR and record.stack_info:
            message = f"{message}\n{record.stack_info}"
        entry["message"] = message
        event = getattr(record, "security_event", None)
        if event:
            entry["security_event"] = event
            entry["details"] = getattr(record, "details", None) or {}
        entry.update(_request_fields())
        try:
            return json.dumps(entry, default=str, ensure_ascii=False)
        except (TypeError, ValueError):  # pragma: no cover - default=str takes nearly anything
            return json.dumps({"severity": entry["severity"], "message": message})


_SEVERITY = {logging.DEBUG: "DEBUG", logging.INFO: "INFO", logging.WARNING: "WARNING",
             logging.ERROR: "ERROR", logging.CRITICAL: "CRITICAL"}


def _request_fields() -> dict:
    try:
        from flask import has_request_context, request
    except ImportError:  # pragma: no cover
        return {}
    if not has_request_context():
        return {}
    fields = {"httpRequest": {"requestMethod": request.method, "requestUrl": request.path,
                              "remoteIp": request.remote_addr or "",
                              "userAgent": (request.user_agent.string or "")[:200]}}
    trace = (request.headers.get("X-Cloud-Trace-Context") or "").split("/", 1)[0]
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
    if trace and project:
        fields["logging.googleapis.com/trace"] = f"projects/{project}/traces/{trace}"
    return fields


def configure_logging(level: str | None = None) -> None:
    """Plain text by default; JSON lines on Cloud Run (json_logs_wanted)."""
    root = logging.getLogger()
    level = (level or os.environ.get("LOG_LEVEL") or "WARNING").upper()
    if json_logs_wanted():
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        root.handlers[:] = [handler]
        root.setLevel(level)
    elif not root.handlers:
        # A bare `flask run` / gunicorn process has no handler on the root
        # logger, so anything logged would silently vanish.
        logging.basicConfig(level=level, format="%(levelname)s:%(name)s:%(message)s")
    # Security events are warnings: always written, whatever LOG_LEVEL says.
    security_log.setLevel(logging.WARNING)


def security_event(kind: str, **details) -> None:
    """One security event: a WARNING log line with "security_event": kind.
    details are short facts (a user id, a path, a limit) - never an email,
    a name, a password or a token."""
    clean = {k: (v if isinstance(v, (int, float, bool)) or v is None else str(v)[:200]) for k, v in details.items()}
    security_log.warning("security event: %s %s", kind,
                         " ".join(f"{k}={v}" for k, v in clean.items()),
                         extra={"security_event": kind, "details": clean})


def init_monitoring(app) -> bool:
    dsn = app.config.get("SENTRY_DSN")
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
    except ImportError:  # pragma: no cover - sentry-sdk is in requirements.txt
        log.warning("SENTRY_DSN is set but sentry-sdk isn't installed - monitoring is off.")
        return False

    def before_send(event, hint):
        user = event.get("user") or {}
        event["user"] = {k: v for k, v in user.items() if k in ("id",)}
        return event

    sentry_sdk.init(
        dsn=dsn,
        integrations=[FlaskIntegration()],
        environment=app.config.get("SENTRY_ENVIRONMENT"),
        release=_release(),
        traces_sample_rate=app.config.get("SENTRY_TRACES_SAMPLE_RATE") or 0.0,
        send_default_pii=False,
        before_send=before_send,
    )

    @app.before_request
    def _sentry_user():
        from flask_login import current_user

        if current_user.is_authenticated:
            sentry_sdk.set_user({"id": current_user.id})
            sentry_sdk.set_tag("role", current_user.role.value)
            if current_user.tenant_id:
                sentry_sdk.set_tag("tenant_id", current_user.tenant_id)

    return True


def _release() -> str | None:
    import os

    return os.environ.get("RENDER_GIT_COMMIT") or os.environ.get("APP_VERSION") or None
