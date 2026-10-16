"""Phase 10 sign-in security: TOTP two-step sign-in (compulsory for the
admin hierarchy, optional for a Client Admin), recovery codes, account
lockout and the per-network limit, idle sign-out, and cashier PINs on a
shared counter (feature "cashier_pin") with tenant isolation."""

import time
from decimal import Decimal

import pytest

from app.auth import security
from app.models.audit_log import AuditLog
from app.models.mixins import utcnow
from app.models.platform import Cashier
from app.models.pos_bill import POSBill
from app.models.product import Product
from app.models.tenant import BillingCycle, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.pos import cashiers as cashier_service
from app.utils.module_settings import save_module_settings

D = Decimal


def _login(client, email, password, follow=True):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=follow)


def _signed_in_id(client):
    with client.session_transaction() as sess:
        return sess.get("_user_id")


# --- TOTP ------------------------------------------------------------------------------------

def test_totp_matches_the_rfc_6238_test_vector():
    # RFC 6238 Appendix B, SHA-1 secret "12345678901234567890", T = 59 s -> 94287082 (8 digits);
    # the 6-digit code is its last six digits.
    import base64

    secret = base64.b32encode(b"12345678901234567890").decode()
    assert security.code_for(secret, at=59) == "287082"
    assert security.code_for(secret, at=1111111109) == "081804"


def test_a_code_matches_one_step_either_side_and_never_twice():
    secret = security.new_secret()
    now = time.time()
    step = security.current_step(now)
    earlier = security._code_at(secret, step - 1)
    assert security.matching_step(secret, earlier, at=now) == step - 1
    assert security.matching_step(secret, earlier, last_step=step - 1, at=now) is None  # replayed
    assert security.matching_step(secret, security._code_at(secret, step - 3), at=now) is None  # too old
    assert security.matching_step(secret, "12ab56", at=now) is None


def test_secret_is_stored_encrypted(app):
    secret = security.new_secret()
    sealed = security.seal_secret(secret)
    assert secret not in sealed
    assert security.unseal_secret(sealed) == secret
    assert security.unseal_secret("garbage") is None


# --- two-step sign-in flows --------------------------------------------------------------------

def test_admin_must_set_up_two_step_before_anything_is_signed_in(app, client, db, super_admin):
    app.config["ADMIN_2FA_REQUIRED"] = True
    res = _login(client, "firm@example.com", "SuperSecret123", follow=False)
    assert res.status_code == 302 and res.headers["Location"].endswith("/auth/two-factor/setup")
    assert _signed_in_id(client) is None  # password alone signs nothing in

    page = client.get("/auth/two-factor/setup")
    assert b"needs two-step sign-in" in page.data
    with client.session_transaction() as sess:
        secret = security.unseal_secret(sess["2fa_new_secret"])
    res = client.post("/auth/two-factor/setup", data={"code": "000000" if security.code_for(secret) != "000000" else "111111"})
    assert _signed_in_id(client) is None
    res = client.post("/auth/two-factor/setup", data={"code": security.code_for(secret)})
    assert res.status_code == 200 and b"recovery codes" in res.data
    assert _signed_in_id(client) == str(super_admin.id)
    db.session.refresh(super_admin)
    assert super_admin.has_two_factor and len(super_admin.recovery_codes) == security.RECOVERY_CODE_COUNT
    assert secret not in (super_admin.totp_secret or "")
    assert AuditLog.query.filter_by(action="two_factor_enabled", actor_user_id=super_admin.id).count() == 1


def _enable(db, user):
    secret = security.new_secret()
    codes = security.enable_two_factor(user, secret)
    user.totp_last_step = None
    db.session.commit()
    return secret, codes


def test_sign_in_with_code_and_with_a_one_time_recovery_code(app, client, db, super_admin):
    app.config["ADMIN_2FA_REQUIRED"] = True
    secret, codes = _enable(db, super_admin)
    res = _login(client, "firm@example.com", "SuperSecret123", follow=False)
    assert res.headers["Location"].endswith("/auth/two-factor")
    assert _signed_in_id(client) is None
    client.post("/auth/two-factor", data={"code": security.code_for(secret)})
    assert _signed_in_id(client) == str(super_admin.id)
    client.get("/auth/logout")

    # The same code can't be used again (replay).
    _login(client, "firm@example.com", "SuperSecret123")
    client.post("/auth/two-factor", data={"code": security.code_for(secret)})
    assert _signed_in_id(client) is None

    client.post("/auth/two-factor", data={"code": codes[0]})
    assert _signed_in_id(client) == str(super_admin.id)
    db.session.refresh(super_admin)
    assert len(super_admin.recovery_codes) == security.RECOVERY_CODE_COUNT - 1
    client.get("/auth/logout")
    _login(client, "firm@example.com", "SuperSecret123")
    client.post("/auth/two-factor", data={"code": codes[0]})  # used up
    assert _signed_in_id(client) is None


def test_five_wrong_codes_send_you_back_to_the_password(app, client, db, super_admin):
    app.config["ADMIN_2FA_REQUIRED"] = True
    secret, _ = _enable(db, super_admin)
    _login(client, "firm@example.com", "SuperSecret123")
    for _ in range(5):
        client.post("/auth/two-factor", data={"code": "000001"})
    with client.session_transaction() as sess:
        assert "2fa_user_id" not in sess
    res = client.post("/auth/two-factor", data={"code": security.code_for(secret)}, follow_redirects=False)
    assert res.headers["Location"].endswith("/auth/login") and _signed_in_id(client) is None


def test_admin_already_signed_in_without_two_step_is_sent_to_set_it_up(app, client, db, super_admin):
    _login(client, "firm@example.com", "SuperSecret123")  # signed in before it was compulsory
    app.config["ADMIN_2FA_REQUIRED"] = True
    res = client.get("/admin/clients", follow_redirects=False)
    assert res.status_code == 302 and "/auth/two-factor/setup" in res.headers["Location"]


def test_two_step_is_optional_for_a_client_admin_and_off_by_default(app, client, db, client_admin):
    app.config["ADMIN_2FA_REQUIRED"] = True
    res = _login(client, "admin@acme.example.com", "ClientSecret123", follow=False)
    assert "/auth/two-factor" not in res.headers["Location"]  # unchanged sign-in
    assert b"Two-step sign-in" in client.get("/auth/security").data
    secret = security.new_secret()
    client.get("/auth/two-factor/setup")
    with client.session_transaction() as sess:
        secret = security.unseal_secret(sess["2fa_new_secret"])
    client.post("/auth/two-factor/setup", data={"code": security.code_for(secret)})
    db.session.refresh(client_admin)
    assert client_admin.has_two_factor
    # Turning it off needs the password and a fresh code.
    client_admin.totp_last_step = None
    db.session.commit()
    client.post("/auth/security", data={"action": "disable", "password": "wrong", "code": security.code_for(secret)})
    db.session.refresh(client_admin)
    assert client_admin.has_two_factor
    client.post("/auth/security", data={"action": "disable", "password": "ClientSecret123",
                                        "code": security.code_for(secret)})
    db.session.refresh(client_admin)
    assert not client_admin.has_two_factor


def test_admin_cannot_turn_compulsory_two_step_off(app, client, db, super_admin):
    app.config["ADMIN_2FA_REQUIRED"] = True
    secret, _ = _enable(db, super_admin)
    _login(client, "firm@example.com", "SuperSecret123")
    client.post("/auth/two-factor", data={"code": security.code_for(secret)})
    db.session.refresh(super_admin)
    super_admin.totp_last_step = None
    db.session.commit()
    client.post("/auth/security", data={"action": "disable", "password": "SuperSecret123",
                                        "code": security.code_for(secret)})
    db.session.refresh(super_admin)
    assert super_admin.has_two_factor


def test_branch_logins_have_no_two_step_page(client, db, staff):
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    assert client.get("/auth/security").status_code == 404
    assert client.get("/auth/two-factor/setup").status_code == 403


def test_firm_resets_a_client_admins_lost_two_step(app, client, db, super_admin, tenant, client_admin):
    _enable(db, client_admin)
    _login(client, "firm@example.com", "SuperSecret123")
    client.post(f"/admin/clients/{tenant.id}/users/{client_admin.id}/reset-two-factor")
    db.session.refresh(client_admin)
    assert not client_admin.has_two_factor


# --- lockout ---------------------------------------------------------------------------------

def test_wrong_passwords_lock_the_account_and_a_reset_opens_it(app, client, db, client_admin):
    for _ in range(app.config["LOGIN_LOCKOUT_ATTEMPTS"]):
        _login(client, "admin@acme.example.com", "nope")
    db.session.refresh(client_admin)
    assert security.locked_until(client_admin) is not None
    res = _login(client, "admin@acme.example.com", "ClientSecret123")
    assert _signed_in_id(client) is None
    assert b"locked" in res.data or b"Too many attempts" in res.data
    assert AuditLog.query.filter_by(action="account_locked").count() == 1
    client_admin.set_password("ClientSecret456")
    db.session.commit()
    assert security.locked_until(client_admin) is None


def test_a_right_password_clears_the_wrong_ones(app, client, db, client_admin):
    for _ in range(3):
        _login(client, "admin@acme.example.com", "nope")
    _login(client, "admin@acme.example.com", "ClientSecret123")
    db.session.refresh(client_admin)
    assert client_admin.failed_login_count == 0


def test_one_network_is_limited_across_accounts(app, client, db, client_admin):
    app.config["LOGIN_IP_LIMIT"] = 3
    for n in range(3):
        _login(client, f"nobody{n}@example.com", "nope")
    res = _login(client, "admin@acme.example.com", "ClientSecret123")
    assert b"this network" in res.data and _signed_in_id(client) is None


# --- idle sign-out ---------------------------------------------------------------------------

def _age_session(client, seconds):
    with client.session_transaction() as sess:
        sess["last_active"] = time.time() - seconds


def test_admin_is_signed_out_after_idle_minutes(app, client, db, super_admin):
    app.config["ADMIN_IDLE_MINUTES"] = 30
    _login(client, "firm@example.com", "SuperSecret123")
    _age_session(client, 29 * 60)
    assert client.get("/admin/clients").status_code == 200
    _age_session(client, 31 * 60)
    res = client.get("/admin/clients", follow_redirects=False)
    assert res.status_code == 302 and "/auth/login" in res.headers["Location"]
    assert _signed_in_id(client) is None


def test_client_logins_never_time_out_unless_the_client_sets_it(app, client, db, tenant, client_admin):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    _age_session(client, 10 * 3600)
    assert client.get("/settings/").status_code == 200  # unchanged behaviour
    save_module_settings(tenant, "security", {"idle_minutes": 15})
    db.session.commit()
    _age_session(client, 16 * 60)
    res = client.get("/settings/", headers={"X-Requested-With": "XMLHttpRequest"})
    assert res.status_code == 401 and res.get_json()["signed_out"]


def test_idle_setting_saved_from_settings(client, db, tenant, client_admin):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    client.post("/settings/security", data={"idle_minutes": "30"})
    db.session.refresh(tenant)
    assert tenant.module_settings["security"]["idle_minutes"] == 30
    client.post("/settings/security", data={"idle_minutes": "7"})  # not a choice -> off
    db.session.refresh(tenant)
    assert tenant.module_settings["security"]["idle_minutes"] == 0


# --- cashier PINs ----------------------------------------------------------------------------

def test_pin_rules():
    assert security.pin_error("12") and security.pin_error("1234567") and security.pin_error("12a4")
    assert security.pin_error("1111") and security.pin_error("1234") and security.pin_error("4321")
    assert security.pin_error("2580") is None


def _product(db, tenant):
    p = Product(tenant_id=tenant.id, name="Tea", hsn_or_sac_code="0902", gst_rate=D("5"), unit="NOS",
                sale_price=D("20"))
    db.session.add(p)
    db.session.commit()
    return p


def _checkout(client, product):
    return client.post("/pos/checkout", data={"line_product_id[]": str(product.id), "line_description[]": "Tea",
                                              "line_hsn[]": "0902", "line_qty[]": "1", "line_rate[]": "20",
                                              "line_gst_rate[]": "5", "line_unit[]": "NOS", "payment_mode": "cash"},
                       headers={"X-Requested-With": "XMLHttpRequest"})


def test_counter_with_cashiers_needs_a_pin_and_records_the_cashier(app, client, db, tenant, staff):
    tenant.set_feature("cashier_pin", True)
    db.session.commit()
    product = _product(db, tenant)
    with app.test_request_context():
        cashier_service.add_cashier(tenant, staff, "Ravi", "2580")
        cashier_service.add_cashier(tenant, staff, "Meena", "3691")
        db.session.commit()
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    page = client.get("/pos/")
    assert b'"cashier": {' in page.data
    res = _checkout(client, product)
    assert res.status_code == 400 and "PIN" in res.get_json()["error"]

    assert client.post("/pos/cashier/unlock", data={"pin": "9999"}).status_code == 400
    res = client.post("/pos/cashier/unlock", data={"pin": "3691"})
    assert res.get_json()["cashier"]["name"] == "Meena"
    res = _checkout(client, product)
    assert res.status_code == 200, res.get_json()
    bill = db.session.get(POSBill, res.get_json()["bill_id"])
    assert bill.cashier.name == "Meena"

    client.post("/pos/cashier/lock")
    assert _checkout(client, product).status_code == 400


def test_five_wrong_pins_make_the_counter_wait(app, client, db, tenant, staff):
    tenant.set_feature("cashier_pin", True)
    db.session.commit()
    with app.test_request_context():
        cashier_service.add_cashier(tenant, staff, "Ravi", "2580")
        db.session.commit()
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    for _ in range(5):
        client.post("/pos/cashier/unlock", data={"pin": "9753"})
    res = client.post("/pos/cashier/unlock", data={"pin": "2580"})
    assert res.status_code == 400 and res.get_json()["wait"] > 0


def test_idle_counter_locks_itself(app, client, db, tenant, staff):
    tenant.set_feature("cashier_pin", True)
    save_module_settings(tenant, "cashier", {"lock_minutes": 5})
    db.session.commit()
    product = _product(db, tenant)
    with app.test_request_context():
        cashier_service.add_cashier(tenant, staff, "Ravi", "2580")
        db.session.commit()
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    client.post("/pos/cashier/unlock", data={"pin": "2580"})
    with client.session_transaction() as sess:
        sess["cashier_at"] = time.time() - 6 * 60
    assert _checkout(client, product).status_code == 400


def test_pins_are_unique_per_counter_and_stored_hashed(app, db, tenant, staff, client_admin):
    with app.test_request_context():
        c = cashier_service.add_cashier(tenant, staff, "Ravi", "2580")
        assert "2580" not in c.pin_digest
        with pytest.raises(cashier_service.CashierError):
            cashier_service.add_cashier(tenant, staff, "Asha", "2580")
        # The same PIN at another counter is fine.
        cashier_service.add_cashier(tenant, client_admin, "Owner", "2580")


def test_without_the_flag_or_cashiers_the_counter_is_unchanged(app, client, db, tenant, staff):
    product = _product(db, tenant)
    with app.test_request_context():
        cashier_service.add_cashier(tenant, staff, "Ravi", "2580")
        db.session.commit()
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    assert b'"cashier"' not in client.get("/pos/").data
    assert _checkout(client, product).status_code == 200
    assert client.post("/pos/cashier/unlock", data={"pin": "2580"}).status_code == 404


def test_cashiers_page_is_tenant_isolated(app, client, db, tenant, client_admin, super_admin):
    tenant.set_feature("cashier_pin", True)
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
                   billing_cycle=BillingCycle.MONTHLY, features={"cashier_pin": True})
    db.session.add(other)
    db.session.flush()
    db.session.add(Gstin(tenant_id=other.id, gstin="29AAAAA0000A1Z5", state_code="29", state_name="Karnataka",
                         is_primary=True))
    other_branch = User(tenant_id=other.id, name="Other branch", email="b@other.example.com", role=UserRole.STAFF,
                        must_change_password=False)
    other_branch.set_password("OtherSecret123")
    db.session.add(other_branch)
    db.session.commit()
    with app.test_request_context():
        theirs = cashier_service.add_cashier(other, other_branch, "Theirs", "2580")
        db.session.commit()
    _login(client, "admin@acme.example.com", "ClientSecret123")
    page = client.get("/branches/cashiers")
    assert page.status_code == 200 and b"Theirs" not in page.data and b"Other branch" not in page.data
    # Guessing their ids: neither their cashier nor their branch can be touched.
    assert client.post("/branches/cashiers", data={"action": "pin", "cashier_id": theirs.id,
                                                   "pin": "3691"}).status_code == 404
    assert client.post("/branches/cashiers", data={"action": "add", "counter_id": other_branch.id, "name": "X",
                                                   "pin": "3691"}).status_code == 404
    db.session.refresh(theirs)
    assert theirs.pin_digest == db.session.get(Cashier, theirs.id).pin_digest
    assert Cashier.query.filter_by(tenant_id=other.id).count() == 1


def test_cashier_pages_are_hidden_with_the_flag_off(client, db, tenant, client_admin):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    assert client.get("/branches/cashiers").status_code == 404
