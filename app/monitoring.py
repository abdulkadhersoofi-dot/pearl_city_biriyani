"""Error monitoring: Sentry, only when SENTRY_DSN is set - with no DSN
nothing is imported, initialised or sent anywhere.

What goes to Sentry: unhandled exceptions with their stack trace, the
request URL and the signed-in user's id and role (no email, name or form
data - send_default_pii stays off). Performance tracing is off unless
SENTRY_TRACES_SAMPLE_RATE is set above 0."""

import logging

log = logging.getLogger(__name__)


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
