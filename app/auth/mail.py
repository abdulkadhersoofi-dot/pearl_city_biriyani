"""Minimal notification layer for OTPs. Defaults to logging to the console
so Phase 1 works out of the box without an SMTP account; set MAIL_BACKEND=smtp
and the SMTP_* env vars to send real email.
"""

import logging
import smtplib
from email.message import EmailMessage

from flask import current_app

logger = logging.getLogger("gstapp.mail")


def send_otp_email(to_email: str, code: str, purpose: str = "password reset") -> bool:
    """Returns True if the email was (as far as we can tell) sent. False on
    any SMTP failure - logged here, never raised. A wrong app password or a
    Gmail hiccup must never crash the request that triggered it (account
    creation, a password-reset request); callers that can usefully tell the
    operator "this didn't go out" should check the return value, but the
    console-mail fallback effectively can't fail, and forgot-password
    deliberately shows the same message either way (telling them apart
    would leak whether an email is registered).
    """
    backend = current_app.config.get("MAIL_BACKEND", "console")
    subject = f"Your {purpose} code"
    body = (
        f"Your one-time code is: {code}\n\n"
        f"It expires in {current_app.config['OTP_EXPIRY_MINUTES']} minutes. "
        "If you did not request this, you can ignore this email."
    )

    if backend != "smtp":
        logger.info("[console-mail] To: %s | Subject: %s | %s", to_email, subject, body)
        return True

    try:
        _send_smtp(to_email, subject, body)
        return True
    except (smtplib.SMTPException, OSError):
        logger.exception("Failed to send OTP email to %s", to_email)
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
