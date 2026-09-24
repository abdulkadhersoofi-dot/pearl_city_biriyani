import os
from datetime import timedelta


def _normalize_database_url(url: str) -> str:
    """Render (like Heroku) hands out connection strings as postgres://,
    which SQLAlchemy 1.4+/psycopg2 reject - they require postgresql://.
    """
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-insecure-key")

    SQLALCHEMY_DATABASE_URI = _normalize_database_url(
        os.environ.get(
            "DATABASE_URL", "postgresql+psycopg2://gst_app:gst_app@localhost:5432/gst_billing"
        )
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 280,
    }

    REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    SESSION_TYPE = "redis"
    SESSION_PERMANENT = True
    SESSION_USE_SIGNER = True
    SESSION_KEY_PREFIX = "gstapp:sess:"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)

    SESSION_COOKIE_NAME = "gstapp_session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("FLASK_ENV") == "production"

    WTF_CSRF_ENABLED = True

    OTP_EXPIRY_MINUTES = int(os.environ.get("OTP_EXPIRY_MINUTES", "10"))
    OTP_LENGTH = int(os.environ.get("OTP_LENGTH", "6"))
    OTP_MAX_ATTEMPTS = 5

    MAIL_BACKEND = os.environ.get("MAIL_BACKEND", "console")
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_FROM = os.environ.get("SMTP_FROM", "no-reply@example.com")

    FIRM_NAME = os.environ.get("FIRM_NAME", "Pearl City & Associates")

    LOGIN_RATE_LIMIT_ATTEMPTS = 8
    LOGIN_RATE_LIMIT_WINDOW_MINUTES = 15


class DevelopmentConfig(Config):
    DEBUG = True
    SESSION_COOKIE_SECURE = False


class TestingConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    # A real Postgres test database, not sqlite: row locking (SELECT ... FOR
    # UPDATE) in the invoice-numbering allocator and our use of native JSON
    # / Enum columns need Postgres semantics to be tested faithfully.
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg2://gst_app:gst_app@localhost:5432/gst_billing_test",
    )
    SESSION_TYPE = "filesystem"
    SESSION_COOKIE_SECURE = False


class ProductionConfig(Config):
    DEBUG = False


config_by_name = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}


def get_config():
    env = os.environ.get("FLASK_ENV", "production")
    return config_by_name.get(env, ProductionConfig)
