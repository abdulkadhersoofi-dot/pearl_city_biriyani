import smtplib
from unittest.mock import patch

from app.auth.mail import send_otp_email


def test_console_backend_always_reports_success(app):
    with app.app_context():
        assert send_otp_email("someone@example.com", "123456") is True


def test_smtp_failure_is_caught_and_reported_not_raised(app):
    app.config["MAIL_BACKEND"] = "smtp"
    app.config["SMTP_HOST"] = "smtp.example.invalid"
    app.config["SMTP_PORT"] = 587
    app.config["SMTP_USERNAME"] = "user"
    app.config["SMTP_PASSWORD"] = "wrong"
    app.config["SMTP_FROM"] = "user@example.invalid"

    with app.app_context():
        with patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value.__enter__.return_value.login.side_effect = (
                smtplib.SMTPAuthenticationError(535, b"bad credentials")
            )
            # Must not raise - callers rely on a bool return, not a try/except.
            result = send_otp_email("someone@example.com", "123456")
            assert result is False
