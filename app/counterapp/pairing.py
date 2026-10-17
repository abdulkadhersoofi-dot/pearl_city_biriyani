"""Pairing a phone or tablet with a branch login (ARFA Counter app,
stage 1).

The Client Admin asks for a code on the Branches page: six digits, valid
for PAIRING_MINUTES, used once, kept only as a hash. The app sends the
code once; the device becomes one of the business's counter devices
(CounterDevice - the same record offline billing numbers its bills from,
O1/2026-27/0001...) for that branch login, and gets its own secret.
From then on the app signs itself back in with that secret, so staff never
type the branch's email or password. Switching the device off (Branches
page) stops it at once; so does pausing the branch login or the business.

Wrong codes and wrong secrets are counted per network address, as wrong
passwords are, and refused for a while past PAIR_ATTEMPTS.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from app.extensions import db
from app.models.mixins import utcnow
from app.models.platform import CounterDevice, DevicePairing
from app.models.user import UserRole

PAIRING_MINUTES = 15
PAIR_ATTEMPTS = 10           # wrong codes / secrets from one address...
PAIR_WINDOW_MINUTES = 15     # ...within this many minutes, then wait
FAILED_ACTION = "device_pair_failed"


class PairingError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _clean(code) -> str:
    return "".join(ch for ch in str(code or "") if ch.isdigit())


def new_pairing(tenant, branch_user, actor) -> tuple[DevicePairing, str]:
    """A fresh code for one branch login. Any earlier unused code for that
    login stops working, so only the code on screen now pairs."""
    if branch_user.tenant_id != tenant.id or branch_user.role != UserRole.STAFF:
        raise PairingError("Phones pair with a branch login.")
    if not branch_user.is_active:
        raise PairingError(f"{branch_user.name} is switched off - switch the branch on first.")
    now = utcnow()
    (DevicePairing.query.filter(DevicePairing.tenant_id == tenant.id, DevicePairing.user_id == branch_user.id,
                                DevicePairing.used_at.is_(None), DevicePairing.expires_at > now)
     .update({"expires_at": now}, synchronize_session=False))
    # Unique among the codes anyone could use right now.
    for _ in range(20):
        code = f"{secrets.randbelow(10 ** 6):06d}"
        clash = DevicePairing.query.filter(DevicePairing.code_hash == _hash(code), DevicePairing.used_at.is_(None),
                                           DevicePairing.expires_at > now).first()
        if clash is None:
            break
    else:  # pragma: no cover - a million codes, a handful live
        raise PairingError("Couldn't make a code just now - try again.")
    pairing = DevicePairing(tenant_id=tenant.id, user_id=branch_user.id, code_hash=_hash(code),
                            expires_at=now + timedelta(minutes=PAIRING_MINUTES), created_by_id=actor.id)
    db.session.add(pairing)
    db.session.flush()
    return pairing, code


def too_many_attempts(ip: str | None) -> bool:
    from app.models.audit_log import AuditLog

    if not ip:
        return False
    since = utcnow() - timedelta(minutes=PAIR_WINDOW_MINUTES)
    return AuditLog.query.filter(AuditLog.action == FAILED_ACTION, AuditLog.ip_address == ip,
                                 AuditLog.created_at >= since).count() >= PAIR_ATTEMPTS


def _can_sign_in(user) -> None:
    from app.utils.billing import tenant_access_status

    if user is None or not user.is_active or user.role != UserRole.STAFF:
        raise PairingError("This branch login is switched off - ask the owner.", 403)
    status = tenant_access_status(user.tenant)
    if status.blocked:
        raise PairingError(status.message, 403)


def pair(code, device_name: str, platform: str) -> tuple[CounterDevice, str]:
    """Use a code: returns the new counter device and its secret."""
    from app.pos import offline

    code = _clean(code)
    if len(code) != 6:
        raise PairingError("The code is 6 digits.")
    now = utcnow()
    pairing = (DevicePairing.query.filter(DevicePairing.code_hash == _hash(code), DevicePairing.used_at.is_(None),
                                          DevicePairing.expires_at > now)
               .with_for_update().first())
    if pairing is None:
        raise PairingError("That code isn't right, or it has expired - ask the owner for a new one.")
    user = pairing.user
    _can_sign_in(user)
    try:
        device, token = offline.register_device(user.tenant, user, device_name or "Phone", platform)
    except offline.OfflineError as exc:
        raise PairingError(exc.message)
    pairing.used_at = now
    pairing.device_id = device.id
    return device, token


def resume(device_id, token: str) -> CounterDevice:
    """A paired device signing itself back in with its secret."""
    try:
        device_id = int(device_id)
    except (TypeError, ValueError):
        raise PairingError("This device isn't paired.", 403)
    device = CounterDevice.query.filter_by(id=device_id).first()
    if device is None or not token or not hmac.compare_digest(device.token_hash, _hash(token)):
        raise PairingError("This device isn't paired.", 403)
    if not device.is_active:
        raise PairingError("This device was switched off by the owner - ask them to switch it on, or pair again.",
                           403)
    _can_sign_in(device.user)
    device.last_seen_at = utcnow()
    return device


def app_devices(tenant):
    """The business's counter devices, newest first."""
    return (CounterDevice.query.filter_by(tenant_id=tenant.id)
            .order_by(CounterDevice.is_active.desc(), CounterDevice.last_seen_at.desc().nullslast()).all())
