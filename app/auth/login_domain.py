"""A client's own sign-in address (Tenant.login_domain).

A client with their own branding can have their sign-in page on a domain
they own (say login.theirbrand.com), pointed at this server like
arfa.live is. Only the sign-in screens are served there - the email and
password page, two-step sign-in, the logo - in the client's colours.
Every other address on that domain goes to the same path on PUBLIC_URL.

A browser's sign-in (its session cookie) belongs to one domain and can't
be carried to another. So when the password (and two-step code, where the
account has one) is right, nothing is signed in on the client's domain:
it issues a one-time hand-over (LoginHandoff, valid for a minute, only its
hash kept; ten when the recovery codes of a new two-step set-up are
shown first) and the browser posts it straight on to PUBLIC_URL/auth/handoff,
which signs the user in there. The token travels in a POST body, never in
an address, so it isn't left in browser history or request logs.

Only the client's own logins sign in on their domain; anyone else is told
to use the main address."""

import hashlib
import re
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from flask import current_app, g, redirect, request, session, url_for

from app.extensions import db
from app.models.mixins import utcnow

HANDOFF_SECONDS = 60
# After setting up two-step sign-in there, the recovery codes are read first.
HANDOFF_AFTER_SETUP_SECONDS = 600

# What a client's sign-in domain serves; everything else goes to PUBLIC_URL.
SERVED_ENDPOINTS = {"auth.login", "auth.two_factor", "auth.two_factor_setup", "static", "main.healthz",
                    "main.media"}

# Addresses that are never a client's sign-in domain (no lookup needed).
_PLATFORM_SUFFIXES = (".web.app", ".firebaseapp.com", ".run.app")
_LOCAL_HOSTS = {"localhost", "127.0.0.1"}

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN_RE = re.compile(rf"^(?:{_LABEL}\.)+[a-z]{{2,63}}$")


def public_url() -> str:
    return (current_app.config.get("PUBLIC_URL") or "").rstrip("/")


def main_host() -> str:
    return (urlsplit(public_url()).hostname or "").lower()


def request_host() -> str:
    return (request.host or "").lower().split(":")[0].rstrip(".")


def normalise_domain(raw: str | None) -> str | None:
    """"https://Login.Brand.com/auth" -> "login.brand.com"; None when it
    isn't a domain name at all."""
    text = (raw or "").strip().lower()
    if not text:
        return None
    if "://" not in text:
        text = "//" + text
    host = (urlsplit(text).hostname or "").rstrip(".")
    if len(host) > 253 or not _DOMAIN_RE.match(host):
        return None
    return host


def domain_problem(domain: str) -> str | None:
    """Why this domain can't be a client's sign-in address (None if it can)."""
    main = main_host()
    if main and (domain == main or domain.endswith("." + main)):
        return f"{domain} is the main address itself - use a domain the client owns."
    if domain.endswith(_PLATFORM_SUFFIXES):
        return f"{domain} belongs to the hosting platform - use a domain the client owns."
    return None


def tenant_for_host(host: str):
    """The client whose sign-in domain this is, or None. Only looked up for
    an address that isn't the platform's own."""
    if not host or host in _LOCAL_HOSTS or host == main_host() or host.endswith(_PLATFORM_SUFFIXES):
        return None
    from app.models.tenant import Tenant

    return Tenant.query.filter_by(login_domain=host).first()


def current_login_tenant():
    """The client whose sign-in domain this request came in on (None on the
    main address)."""
    return g.get("login_domain_tenant")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_handoff(user, method: str, seconds: int = HANDOFF_SECONDS) -> dict:
    """Issues the hand-over for a user whose sign-in just succeeded on a
    client's domain. The caller commits. Leaves nothing signed in here."""
    from app.models.platform import LoginHandoff
    from app.utils.audit import record_audit

    token = secrets.token_urlsafe(32)
    host = request_host()
    db.session.add(LoginHandoff(user_id=user.id, token_hash=_hash(token), from_host=host,
                                expires_at=utcnow() + timedelta(seconds=seconds)))
    record_audit(user, "login_handoff_issued", tenant_id=user.tenant_id, details={"host": host, "method": method})
    session.clear()
    return {"action": public_url() + url_for("auth.handoff"), "token": token}


def redeem(token: str):
    """The user a hand-over signs in, once - None if it is unknown, used,
    expired, or its account has been switched off since. The caller commits."""
    from app.models.platform import LoginHandoff

    if not token or len(token) > 200:
        return None
    row = LoginHandoff.query.filter_by(token_hash=_hash(token)).with_for_update().first()
    if row is None or row.used_at is not None or row.expires_at <= utcnow():
        return None
    row.used_at = utcnow()
    user = row.user
    if user is None or not user.is_active:
        return None
    return user


def sign_in_address_for(user) -> str | None:
    """Where this user signs in again after signing out: their client's own
    domain when it is in use, else None (the main sign-in page)."""
    tenant = getattr(user, "tenant", None)
    if tenant is None or not tenant.login_domain or not tenant.is_active:
        return None
    from app.utils.entitlements import own_branding

    if not own_branding(tenant):
        return None
    return f"https://{tenant.login_domain}/auth/login"


def delete_expired() -> int:
    """Hand-overs older than a day (used or not) - `flask nightly`."""
    from app.models.platform import LoginHandoff

    count = LoginHandoff.query.filter(LoginHandoff.expires_at < utcnow() - timedelta(days=1)).delete()
    db.session.commit()
    return count


def init_login_domains(app) -> None:
    @app.before_request
    def serve_login_domains():
        tenant = tenant_for_host(request_host())
        g.login_domain_tenant = None
        if tenant is None:
            return None
        from app.utils.entitlements import own_branding

        if not tenant.is_active or not own_branding(tenant):
            # Not (or no longer) entitled to its own address: the main one.
            return redirect(public_url() + "/auth/login")
        g.login_domain_tenant = tenant
        if request.endpoint in SERVED_ENDPOINTS:
            return None
        if request.path in ("", "/"):
            return redirect(url_for("auth.login"))
        target = request.full_path if request.query_string else request.path
        return redirect(public_url() + target)

    @app.context_processor
    def login_domain_context():
        tenant = g.get("login_domain_tenant")
        return {"login_tenant": tenant} if tenant is not None else {}
