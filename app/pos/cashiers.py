"""Cashiers sharing one counter (feature "cashier_pin").

A branch login stays signed in on the counter all day; each cashier taps
in with their own PIN, so every bill records who rang it up. A PIN picks
its cashier - it is unique at that counter - and is stored only as a
keyed hash (app.auth.security.pin_digest). The counter locks again when a
cashier taps Lock / Switch, after the Client Admin's idle minutes, and
after five wrong PINs it waits a minute.

Who is at the counter lives in the browser session: cashier_id, and when
they last did something (cashier_at)."""

import time

from flask import session

from app.auth.security import pin_digest, pin_error
from app.extensions import db
from app.models.platform import Cashier
from app.models.user import User, UserRole
from app.utils.module_settings import module_settings

MAX_PIN_TRIES = 5
PIN_WAIT_SECONDS = 60


class CashierError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def counter_logins(tenant) -> list[User]:
    """Logins a counter runs under: the Client Admin(s) and every branch."""
    return (User.query.filter(User.tenant_id == tenant.id, User.is_active.is_(True),
                              User.role.in_([UserRole.CLIENT_ADMIN, UserRole.STAFF]))
            .order_by(User.role, User.name).all())


def cashiers_for(tenant, counter_user, active_only: bool = True) -> list[Cashier]:
    query = Cashier.query.filter_by(tenant_id=tenant.id, branch_user_id=counter_user.id)
    if active_only:
        query = query.filter_by(is_active=True)
    return query.order_by(Cashier.name).all()


def uses_pins(tenant, counter_user) -> bool:
    return bool(tenant.has_feature("cashier_pin")
                and Cashier.query.filter_by(tenant_id=tenant.id, branch_user_id=counter_user.id,
                                            is_active=True).first())


def lock_minutes(tenant) -> int:
    from app.utils.entitlements import security_minimum, strictest_minutes

    # Never longer than the package's sign-in minimum (stage 5).
    return strictest_minutes(int(module_settings(tenant, "cashier").get("lock_minutes") or 0),
                             security_minimum(tenant, "lock_minutes"))


def current_cashier(tenant, counter_user) -> Cashier | None:
    cashier_id = session.get("cashier_id")
    if not cashier_id:
        return None
    limit = lock_minutes(tenant)
    if limit and time.time() - (session.get("cashier_at") or 0) > limit * 60:
        lock()
        return None
    cashier = db.session.get(Cashier, cashier_id)
    if (cashier is None or not cashier.is_active or cashier.tenant_id != tenant.id
            or cashier.branch_user_id != counter_user.id):
        lock()
        return None
    return cashier


def touch() -> None:
    if session.get("cashier_id"):
        session["cashier_at"] = time.time()


def lock() -> None:
    session.pop("cashier_id", None)
    session.pop("cashier_at", None)


def wait_seconds() -> int:
    return max(0, int((session.get("pin_block_until") or 0) - time.time()))


def unlock(tenant, counter_user, pin: str) -> Cashier:
    """The cashier this PIN belongs to at this counter. Raises CashierError."""
    if wait_seconds():
        raise CashierError(f"Too many wrong PINs - wait {wait_seconds()} seconds.")
    digest = pin_digest(counter_user.id, pin or "")
    cashier = Cashier.query.filter_by(tenant_id=tenant.id, branch_user_id=counter_user.id, pin_digest=digest,
                                      is_active=True).first()
    if cashier is None:
        tries = session.get("pin_fails", 0) + 1
        session["pin_fails"] = tries
        if tries >= MAX_PIN_TRIES:
            session["pin_fails"] = 0
            session["pin_block_until"] = time.time() + PIN_WAIT_SECONDS
            raise CashierError(f"Too many wrong PINs - wait {PIN_WAIT_SECONDS} seconds.")
        raise CashierError("That PIN isn't right.")
    session["pin_fails"] = 0
    session["cashier_id"] = cashier.id
    session["cashier_at"] = time.time()
    return cashier


def for_checkout(tenant, counter_user) -> Cashier | None:
    """The cashier a new bill is rung up by; refused when the counter uses
    PINs and nobody has tapped in."""
    if not uses_pins(tenant, counter_user):
        return None
    cashier = current_cashier(tenant, counter_user)
    if cashier is None:
        raise CashierError("Enter your cashier PIN first.")
    touch()
    return cashier


# --- the Client Admin's list -----------------------------------------------------------------

def _check_pin(tenant, counter_user, pin: str, exclude_id: int | None = None) -> str:
    error = pin_error(pin)
    if error:
        raise CashierError(error)
    digest = pin_digest(counter_user.id, pin)
    clash = Cashier.query.filter(Cashier.tenant_id == tenant.id, Cashier.branch_user_id == counter_user.id,
                                 Cashier.pin_digest == digest, Cashier.is_active.is_(True))
    if exclude_id:
        clash = clash.filter(Cashier.id != exclude_id)
    if clash.first():
        raise CashierError("Another cashier at this counter already has that PIN - pick a different one.")
    return digest


def add_cashier(tenant, counter_user, name: str, pin: str) -> Cashier:
    name = (name or "").strip()[:60]
    if not name:
        raise CashierError("Enter the cashier's name.")
    if Cashier.query.filter_by(branch_user_id=counter_user.id, name=name).first():
        raise CashierError(f"{name} is already a cashier at this counter.")
    cashier = Cashier(tenant_id=tenant.id, branch_user_id=counter_user.id, name=name,
                      pin_digest=_check_pin(tenant, counter_user, pin))
    db.session.add(cashier)
    db.session.flush()
    return cashier


def change_pin(cashier: Cashier, pin: str) -> None:
    from app.models.tenant import Tenant

    tenant = db.session.get(Tenant, cashier.tenant_id)
    cashier.pin_digest = _check_pin(tenant, cashier.branch_user, pin, exclude_id=cashier.id)


def set_active(cashier: Cashier, active: bool) -> None:
    if active:
        from app.models.tenant import Tenant

        tenant = db.session.get(Tenant, cashier.tenant_id)
        clash = Cashier.query.filter(Cashier.tenant_id == tenant.id, Cashier.branch_user_id == cashier.branch_user_id,
                                     Cashier.pin_digest == cashier.pin_digest, Cashier.is_active.is_(True),
                                     Cashier.id != cashier.id).first()
        if clash:
            raise CashierError(f"{clash.name} now uses the same PIN - give {cashier.name} a new PIN first.")
    cashier.is_active = active
