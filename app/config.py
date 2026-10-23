import os
from datetime import timedelta


def _normalize_database_url(url: str) -> str:
    """Render (like Heroku) hands out connection strings as postgres://,
    which SQLAlchemy 1.4+/psycopg2 reject - they require postgresql://.

    A bare postgresql:// (no +driver) also isn't safe to leave alone:
    SQLAlchemy's default DBAPI choice for that scheme isn't pinned to
    psycopg2 across versions - on Render it resolved to the psycopg (v3)
    dialect, which isn't installed (only psycopg2-binary is, in
    requirements.txt), crashing with ModuleNotFoundError. Forcing
    +psycopg2 explicitly removes the ambiguity instead of relying on
    whatever SQLAlchemy defaults to.
    """
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-insecure-key")
    # Encrypts saved e-invoice / e-way bill logins (app.einvoice.vault);
    # unset = derived from SECRET_KEY.
    EINVOICE_SECRET_KEY = os.environ.get("EINVOICE_SECRET_KEY")

    SQLALCHEMY_DATABASE_URI = _normalize_database_url(
        os.environ.get(
            "DATABASE_URL", "postgresql+psycopg2://gst_app:gst_app@localhost:5432/gst_billing"
        )
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # The business's clock: Indian time (UTC+5:30). Timestamps are still
    # stored as UTC instants; this decides how they read back and what
    # "today" is. The server process runs on it (app._set_process_clock -
    # date.today(), datetime.now()), and so does every database connection
    # (its session time zone), so a stored time comes back as Indian time
    # and a day boundary in a report or numbering is midnight in India,
    # not 5:30 in the morning. TIMEZONE_POSIX needs no tz database on the
    # server (India keeps no daylight saving).
    TIMEZONE_NAME = os.environ.get("APP_TIMEZONE", "Asia/Kolkata")
    TIMEZONE_POSIX = os.environ.get("APP_TIMEZONE_POSIX", "IST-5:30")
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 280,
        "connect_args": {"options": f"-c timezone={TIMEZONE_NAME}"},
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

    FIRM_NAME = os.environ.get("FIRM_NAME", "ARFA")

    LOGIN_RATE_LIMIT_ATTEMPTS = 8
    LOGIN_RATE_LIMIT_WINDOW_MINUTES = 15
    # Phase 10 sign-in security (app.auth.security). Wrong passwords in a
    # row before an account locks, and for how long; failed sign-ins one
    # network (IP) may make per window across all accounts.
    LOGIN_LOCKOUT_ATTEMPTS = 8
    LOGIN_LOCKOUT_MINUTES = 15
    LOGIN_IP_LIMIT = int(os.environ.get("LOGIN_IP_LIMIT", "40"))
    # Two-step sign-in is compulsory for the Ultra Admin / Auditor
    # hierarchy; they're signed out after this many idle minutes.
    ADMIN_2FA_REQUIRED = os.environ.get("ADMIN_2FA_REQUIRED", "1") != "0"
    ADMIN_IDLE_MINUTES = int(os.environ.get("ADMIN_IDLE_MINUTES", "30"))
    # Behind a reverse proxy (Render, nginx), how many proxies to trust for
    # the client's real IP (X-Forwarded-For) - 0 = use the socket address.
    PROXY_FIX_X_FOR = int(os.environ.get("PROXY_FIX_X_FOR", "0"))

    # Error monitoring - Sentry only when a DSN is set (app.monitoring).
    SENTRY_DSN = os.environ.get("SENTRY_DSN")
    SENTRY_ENVIRONMENT = os.environ.get("SENTRY_ENVIRONMENT", os.environ.get("FLASK_ENV", "production"))
    SENTRY_TRACES_SAMPLE_RATE = float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0"))

    # Email (daily owner summary) - only when SMTP_HOST is set.
    SMTP_HOST = os.environ.get("SMTP_HOST")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
    SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "1") != "0"
    SMTP_FROM = os.environ.get("SMTP_FROM")

    # In-app update check (Android) - the GitHub repository whose
    # "android-latest" release carries the APK and version.json.
    ANDROID_RELEASE_REPO = os.environ.get("ANDROID_RELEASE_REPO", "abdulkadhersoofi-dot/pearl_city_biriyani")


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
    # Tests that exercise two-step sign-in switch it on themselves; the
    # rest sign the admin in with a password, as before.
    ADMIN_2FA_REQUIRED = False
    SENTRY_DSN = None
    SMTP_HOST = None


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
