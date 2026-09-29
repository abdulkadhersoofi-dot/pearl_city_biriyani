"""Minimal notification layer for OTPs.

Three backends, picked by MAIL_BACKEND:
  - "console" (default): logs to stdout, so the app works with zero setup.
  - "resend": Resend's HTTPS API. Free tier hosts (Render's included) block
    outbound SMTP entirely on ports 25/465/587 - Render did this
    deliberately, platform-wide, to stop spam abuse of free instances, not
    a bug on either end. An HTTPS API call (port 443) isn't affected, so
    this is the backend that actually works there.
  - "smtp": raw SMTP. Left in for hosts that don't block it (a paid Render
    plan, most VPS/self-hosted setups) - untouched by the Resend addition.
"""

import json
import logging
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

from flask import current_app

logger = logging.getLogger("gstapp.mail")

RESEND_API_URL = "https://api.resend.com/emails"


def send_otp_email(to_email: str, code: str, purpose: str = "password reset") -> bool:
    """Returns True if the email was (as far as we can tell) sent. False on
    any send failure - logged here, never raised. A wrong API key/app
    password, a provider hiccup, or (on most free hosts) SMTP being
    firewalled outright must never crash the request that triggered this
    (account creation, a password-reset request); callers that can usefully
    tell the operator "this didn't go out" should check the return value,
    but the console-mail fallback effectively can't fail, and forgot-
    password deliberately shows the same message either way (telling them
    apart would leak whether an email is registered).

    On any real-backend failure, the code is also logged (clearly marked,
    distinct from the routine console-mail log line) so it's still
    recoverable from the host's log viewer even though the primary channel
    didn't work - this is the "check the logs" fallback promised to admins
    when onboarding/reset flows report a send failure.
    """
    backend = current_app.config.get("MAIL_BACKEND", "console")
    subject = f"Your {purpose} code"
    body = (
        f"Your one-time code is: {code}\n\n"
        f"It expires in {current_app.config['OTP_EXPIRY_MINUTES']} minutes. "
        "If you did not request this, you can ignore this email."
    )

    if backend not in ("smtp", "resend"):
        logger.info("[console-mail] To: %s | Subject: %s | %s", to_email, subject, body)
        return True

    try:
        if backend == "resend":
            _send_resend(to_email, subject, body)
        else:
            _send_smtp(to_email, subject, body)
        return True
    except (smtplib.SMTPException, OSError, urllib.error.URLError, ValueError):
        logger.exception(
            "Failed to send OTP email to %s via %s backend - code was: %s", to_email, backend, code
        )
        return False


def _send_smtp(to_email: str, subject: str, body: str) -> None:
    cfg = current_app.config
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["SMTP_FROM"]
    msg["To"] = to_email
    msg.set_content(body)

    with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=10) as server:
        server.starttls()
        if cfg["SMTP_USERNAME"]:
            server.login(cfg["SMTP_USERNAME"], cfg["SMTP_PASSWORD"])
        server.send_message(msg)


def _send_resend(to_email: str, subject: str, body: str) -> None:
    cfg = current_app.config
    api_key = cfg.get("RESEND_API_KEY", "")
    if not api_key:
        raise ValueError("RESEND_API_KEY is not set")

    payload = json.dumps(
        {
            "from": cfg["SMTP_FROM"],
            "to": [to_email],
            "subject": subject,
            "text": body,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        RESEND_API_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    # urlopen raises urllib.error.HTTPError for any non-2xx response (e.g. a
    # 403 from the resend.dev sandbox sender being used for a recipient
    # other than the Resend account's own email) - caught by the same
    # except clause in send_otp_email as a network-level failure.
    with urllib.request.urlopen(request, timeout=10):
        pass
