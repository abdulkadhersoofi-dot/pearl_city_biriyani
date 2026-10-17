"""Sign-in security (Phase 10) - the rules in one place, the routes in
app.auth.routes:

  Two-step sign-in   TOTP (RFC 6238: HMAC-SHA1, 30-second steps, 6 digits)
                     - works with Google Authenticator, Microsoft
                     Authenticator, Authy and the like; nothing is sent
                     anywhere. Required for the Ultra Admin and the Auditor
                     hierarchy (they can open every client they manage);
                     a Client Admin can turn it on for themselves. The
                     secret is stored encrypted (Fernet, key derived from
                     SECRET_KEY); a step already used can't be used again;
                     eight one-time recovery codes for a lost phone.
  Lockout            LOGIN_LOCKOUT_ATTEMPTS wrong passwords in a row lock
                     the account for LOGIN_LOCKOUT_MINUTES; one network
                     (IP) gets LOGIN_IP_LIMIT failed sign-ins per window
                     across all accounts.
  Idle sign-out      admin-hierarchy logins after ADMIN_IDLE_MINUTES;
                     a client's own logins only if the Client Admin sets
                     a limit in Settings (off unless they do).
  Cashier PIN        HMAC-SHA256 of the PIN keyed by SECRET_KEY and scoped
                     to the counter - quick to check and unique per counter.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import timedelta
from urllib.parse import quote, urlencode

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

from app.models.mixins import utcnow

TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
# Accept the code from one step either side of now - phones drift.
TOTP_WINDOW = 1
RECOVERY_CODE_COUNT = 8


# --- TOTP ----------------------------------------------------------------------------------

def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(f"totp-secret:{current_app.config['SECRET_KEY']}".encode()).digest())
    return Fernet(key)


def seal_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def unseal_secret(sealed: str | None) -> str | None:
    if not sealed:
        return None
    try:
        return _fernet().decrypt(sealed.encode()).decode()
    except InvalidToken:
        return None


def _code_at(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def current_step(at: float | None = None) -> int:
    return int((time.time() if at is None else at) // TOTP_STEP_SECONDS)


def code_for(secret: str, at: float | None = None) -> str:
    return _code_at(secret, current_step(at))


def matching_step(secret: str, code: str, last_step: int | None = None, at: float | None = None) -> int | None:
    """The step `code` belongs to, or None. A step at or before `last_step`
    (already used) never matches again."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != TOTP_DIGITS or not secret:
        return None
    now = current_step(at)
    for step in range(now - TOTP_WINDOW, now + TOTP_WINDOW + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(_code_at(secret, step), code):
            return step
    return None


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    """otpauth:// link an authenticator app reads from the QR code."""
    label = quote(f"{issuer}:{account}")
    query = urlencode({"secret": secret, "issuer": issuer, "digits": TOTP_DIGITS, "period": TOTP_STEP_SECONDS},
                      quote_via=quote)
    return f"otpauth://totp/{label}?{query}"


def verify_user_code(user, code: str) -> bool:
    """A user's authenticator code - consumed, so it can't be replayed."""
    step = matching_step(unseal_secret(user.totp_secret), code, user.totp_last_step)
    if step is None:
        return False
    user.totp_last_step = step
    return True


# --- recovery codes ------------------------------------------------------------------------

def _hash_code(code: str) -> str:
    return hashlib.sha256(code.replace("-", "").strip().lower().encode()).hexdigest()


def new_recovery_codes() -> tuple[list[str], list[str]]:
    """(codes to show the user once, hashes to store)."""
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = secrets.token_hex(5)
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes, [_hash_code(c) for c in codes]


def use_recovery_code(user, code: str) -> bool:
    if not code or not user.recovery_codes:
        return False
    wanted = _hash_code(code)
    remaining = [h for h in user.recovery_codes if not hmac.compare_digest(h, wanted)]
    if len(remaining) == len(user.recovery_codes):
        return False
    user.recovery_codes = remaining
    return True


def enable_two_factor(user, secret: str) -> list[str]:
    codes, hashes = new_recovery_codes()
    user.totp_secret = seal_secret(secret)
    user.totp_enabled_at = utcnow()
    user.totp_last_step = current_step()
    user.recovery_codes = hashes
    return codes


def disable_two_factor(user) -> None:
    user.totp_secret = None
    user.totp_enabled_at = None
    user.totp_last_step = None
    user.recovery_codes = None


def two_factor_required(user) -> bool:
    if user.is_admin_hierarchy:
        return bool(current_app.config.get("ADMIN_2FA_REQUIRED", True))
    # A client's package can require it for the owner's login (stage 5).
    if user.is_client_admin and user.tenant is not None:
        from app.utils.entitlements import security_minimum

        return security_minimum(user.tenant, "owner_two_step")
    return False


def two_factor_allowed(user) -> bool:
    """Who may turn it on for themselves: the admin hierarchy and Client
    Admins. A branch counter is shared - it gets cashier PINs instead."""
    return bool(user.is_admin_hierarchy or user.is_client_admin)


# --- lockout ---------------------------------------------------------------------------------

def locked_until(user):
    if user.locked_until and user.locked_until > utcnow():
        return user.locked_until
    return None


def register_failure(user) -> bool:
    """One more wrong password. True when this one locked the account."""
    user.failed_login_count = (user.failed_login_count or 0) + 1
    if user.failed_login_count >= current_app.config["LOGIN_LOCKOUT_ATTEMPTS"]:
        user.locked_until = utcnow() + timedelta(minutes=current_app.config["LOGIN_LOCKOUT_MINUTES"])
        user.failed_login_count = 0
        return True
    return False


def clear_failures(user) -> None:
    user.failed_login_count = 0
    user.locked_until = None


def ip_failures(ip: str | None, window_minutes: int) -> int:
    from app.models.audit_log import AuditLog

    if not ip:
        return 0
    since = utcnow() - timedelta(minutes=window_minutes)
    return AuditLog.query.filter(AuditLog.action == "login_failed", AuditLog.ip_address == ip,
                                 AuditLog.created_at >= since).count()


# --- idle sign-out ---------------------------------------------------------------------------

def idle_limit_minutes(user, impersonating: bool = False) -> int:
    """0 = no idle limit."""
    if user.is_admin_hierarchy or impersonating:
        return int(current_app.config.get("ADMIN_IDLE_MINUTES") or 0)
    tenant = user.tenant
    if tenant is None:
        return 0
    from app.utils.entitlements import security_minimum, strictest_minutes
    from app.utils.module_settings import module_settings

    # Never longer than the package's sign-in minimum (stage 5).
    return strictest_minutes(int(module_settings(tenant, "security").get("idle_minutes") or 0),
                             security_minimum(tenant, "idle_minutes"))


# --- cashier PIN -----------------------------------------------------------------------------

PIN_MIN, PIN_MAX = 4, 6


def pin_error(pin: str) -> str | None:
    pin = (pin or "").strip()
    if not pin.isdigit() or not PIN_MIN <= len(pin) <= PIN_MAX:
        return f"A PIN is {PIN_MIN} to {PIN_MAX} digits."
    if len(set(pin)) == 1 or pin in "0123456789" or pin in "9876543210":
        return "Pick a PIN that isn't a repeated or running number (like 1111 or 1234)."
    return None


def pin_digest(branch_user_id: int, pin: str) -> str:
    key = current_app.config["SECRET_KEY"].encode()
    return hmac.new(key, f"cashier-pin:{branch_user_id}:{pin.strip()}".encode(), hashlib.sha256).hexdigest()
