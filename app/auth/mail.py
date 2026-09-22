"""Minimal notification layer for OTPs. Defaults to logging to the console
so Phase 1 works out of the box without an SMTP account; set MAIL_BACKEND=smtp
and the SMTP_* env vars to send real email.
"""

import logging
import smtplib
from email.message import EmailMessage

from flask import current_app

logger = logging.getLogger("gstapp.mail")


def send_otp_email(to_email: str, code: str, purpose: str = "password reset") -> None:
    backend = current_app.config.get("MAIL_BACKEND", "console")
    subject = f"Your {purpose} code"
    body = (
        f"Your one-time code is: {code}\n\n"
        f"It expires in {current_app.config['OTP_EXPIRY_MINUTES']} minutes. "
        "If you did not request this, you can ignore this email."
    )

    if backend == "smtp":
        _send_smtp(to_email, subject, body)
    else:
        logger.info("[console-mail] To: %s | Subject: %s | %s", to_email, subject, body)


def _send_smtp(to_email: str, subject: str, body: str) -> None:
    cfg = current_app.config
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["SMTP_FROM"]
    msg["To"] = to_email
    msg.set_content(body)

    with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"]) as server:
        server.starttls()
        if cfg["SMTP_USERNAME"]:
            server.login(cfg["SMTP_USERNAME"], cfg["SMTP_PASSWORD"])
        server.send_message(msg)
