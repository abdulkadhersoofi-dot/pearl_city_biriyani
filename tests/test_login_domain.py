"""A client's own sign-in address (app/auth/login_domain.py): the branded
sign-in page on the client's domain, then a one-time hand-over to the main
address, where the user is actually signed in."""

import re
from datetime import timedelta

import pytest

from app.auth import security
from app.models.audit_log import AuditLog
from app.models.mixins import utcnow
from app.models.platform import LoginHandoff
from app.models.tenant import RegistrationType, Tenant
from app.models.user import User, UserRole

DOMAIN = "login.acme-brand.com"
OWN = f"https://{DOMAIN}"


@pytest.fixture()
def branded(db, tenant):
    tenant.login_domain = DOMAIN
    tenant.trade_name = "Acme Biryani"
    db.session.commit()
    return tenant


def _signed_in_id(client):
    with client.session_transaction() as sess:
        return sess.get("_user_id")


def _sign_in_on_domain(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, base_url=OWN)


def _token(page) -> str:
    match = re.search(rb'name="token" value="([^"]+)"', page.data)
    assert match, page.data[:400]
    return match.group(1).decode()


def test_the_domain_shows_the_clients_own_sign_in_page(client, branded):
    res = client.get("/", base_url=OWN)
    assert res.status_code == 302 and res.headers["Location"].endswith("/auth/login")
    page = client.get("/auth/login", base_url=OWN)
    assert page.status_code == 200 and b"Acme Biryani" in page.data


def test_everything_else_on_the_domain_goes_to_the_main_address(app, client, branded):
    res = client.get("/invoicing/?page=2", base_url=OWN)
    assert res.status_code == 302
    assert res.headers["Location"] == app.config["PUBLIC_URL"].rstrip("/") + "/invoicing/?page=2"
    assert client.get("/health", base_url=OWN).status_code == 200


def test_signing_in_on_the_domain_hands_over_to_the_main_address(app, client, db, branded, client_admin):
    page = _sign_in_on_domain(client, client_admin.email, "ClientSecret123")
    assert page.status_code == 200
    assert (app.config["PUBLIC_URL"].rstrip("/") + "/auth/handoff").encode() in page.data
    token = _token(page)
    assert token.encode() not in (LoginHandoff.query.one().token_hash or "").encode()  # only the hash is kept
    assert _signed_in_id(client) is None  # nothing signed in on the client's domain

    res = client.post("/auth/handoff", data={"token": token})
    assert res.status_code == 302
    assert _signed_in_id(client) == str(client_admin.id)
    row = AuditLog.query.filter_by(action="login_success", actor_user_id=client_admin.id).one()
    assert row.details["method"] == "own_address"

    # Used once only.
    client.get("/auth/logout")
    client.post("/auth/handoff", data={"token": token})
    assert _signed_in_id(client) is None


def test_a_hand_over_expires_after_a_minute(client, db, branded, client_admin):
    token = _token(_sign_in_on_domain(client, client_admin.email, "ClientSecret123"))
    row = LoginHandoff.query.one()
    row.expires_at = utcnow() - timedelta(seconds=1)
    db.session.commit()
    res = client.post("/auth/handoff", data={"token": token}, follow_redirects=True)
    assert _signed_in_id(client) is None and b"sign in again" in res.data


def test_a_hand_over_cant_be_guessed_or_fetched(client, branded):
    client.post("/auth/handoff", data={"token": "made-up"})
    assert _signed_in_id(client) is None
    assert client.get("/auth/handoff").status_code == 302


def test_a_hand_over_is_refused_on_the_clients_domain_itself(client, branded, client_admin):
    token = _token(_sign_in_on_domain(client, client_admin.email, "ClientSecret123"))
    res = client.post("/auth/handoff", data={"token": token}, base_url=OWN)
    assert res.status_code == 302  # sent on to the main address, token not spent
    assert LoginHandoff.query.one().used_at is None


def test_only_the_clients_own_logins_sign_in_on_their_domain(client, db, branded, super_admin):
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id)
    db.session.add(other)
    db.session.flush()
    stranger = User(tenant_id=other.id, name="Other", email="owner@other.example.com", role=UserRole.CLIENT_ADMIN,
                    must_change_password=False)
    stranger.set_password("OtherSecret123")
    db.session.add(stranger)
    db.session.commit()

    for email, password in (("owner@other.example.com", "OtherSecret123"), ("firm@example.com", "SuperSecret123")):
        page = _sign_in_on_domain(client, email, password)
        assert b"doesn&#39;t sign in here" in page.data or b"doesn't sign in here" in page.data
    assert LoginHandoff.query.count() == 0


def test_a_wrong_password_on_the_domain_is_just_wrong(client, branded, client_admin):
    page = _sign_in_on_domain(client, client_admin.email, "nope-nope-1")
    assert b"Incorrect email or password" in page.data
    assert LoginHandoff.query.count() == 0


def test_two_step_sign_in_happens_on_the_domain_then_hands_over(client, db, branded, client_admin):
    secret = security.new_secret()
    security.enable_two_factor(client_admin, secret)
    client_admin.totp_last_step = None
    db.session.commit()

    res = _sign_in_on_domain(client, client_admin.email, "ClientSecret123")
    assert res.status_code == 302 and res.headers["Location"].endswith("/auth/two-factor")
    page = client.get("/auth/two-factor", base_url=OWN)
    assert page.status_code == 200 and b"Acme Biryani" in page.data  # still the client's own look
    page = client.post("/auth/two-factor", data={"code": security.code_for(secret)}, base_url=OWN)
    token = _token(page)
    client.post("/auth/handoff", data={"token": token})
    assert _signed_in_id(client) == str(client_admin.id)


def test_signing_out_goes_back_to_the_clients_own_address(client, branded, client_admin):
    client.post("/auth/handoff", data={"token": _token(_sign_in_on_domain(client, client_admin.email,
                                                                          "ClientSecret123"))})
    res = client.get("/auth/logout")
    assert res.headers["Location"] == f"{OWN}/auth/login"


def test_an_inactive_client_s_domain_sends_people_to_the_main_sign_in(app, client, db, branded):
    branded.is_active = False
    db.session.commit()
    res = client.get("/auth/login", base_url=OWN)
    assert res.headers["Location"] == app.config["PUBLIC_URL"].rstrip("/") + "/auth/login"


def test_the_main_address_is_unchanged(client, branded, client_admin):
    page = client.get("/auth/login")
    assert b"Acme Biryani" not in page.data
    client.post("/auth/login", data={"email": client_admin.email, "password": "ClientSecret123"})
    assert _signed_in_id(client) == str(client_admin.id)
    assert LoginHandoff.query.count() == 0


# --- the Ultra Admin sets it ----------------------------------------------------------------

def _admin(client):
    client.post("/auth/login", data={"email": "firm@example.com", "password": "SuperSecret123"})


def test_ultra_admin_sets_and_removes_the_address(client, db, tenant, super_admin):
    _admin(client)
    client.post(f"/admin/clients/{tenant.id}/login-domain", data={"login_domain": "https://Login.Acme-Brand.com/auth"})
    db.session.refresh(tenant)
    assert tenant.login_domain == DOMAIN
    assert AuditLog.query.filter_by(action="tenant_login_domain_changed").count() == 1
    client.post(f"/admin/clients/{tenant.id}/login-domain", data={"login_domain": ""})
    db.session.refresh(tenant)
    assert tenant.login_domain is None


@pytest.mark.parametrize("value", ["not a domain", "arfa.live", "x.arfa.live", "someone.web.app", "localhost"])
def test_unusable_addresses_are_refused(client, db, tenant, super_admin, value):
    _admin(client)
    client.post(f"/admin/clients/{tenant.id}/login-domain", data={"login_domain": value})
    db.session.refresh(tenant)
    assert tenant.login_domain is None


def test_one_address_belongs_to_one_client(client, db, tenant, super_admin):
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
                   login_domain=DOMAIN)
    db.session.add(other)
    db.session.commit()
    _admin(client)
    client.post(f"/admin/clients/{tenant.id}/login-domain", data={"login_domain": DOMAIN})
    db.session.refresh(tenant)
    assert tenant.login_domain is None


def test_only_the_ultra_admin_sets_it(client, db, tenant, client_admin):
    client.post("/auth/login", data={"email": client_admin.email, "password": "ClientSecret123"})
    res = client.post(f"/admin/clients/{tenant.id}/login-domain", data={"login_domain": DOMAIN})
    assert res.status_code == 403


def test_the_client_page_shows_the_address_field_to_the_ultra_admin(client, db, branded, super_admin):
    _admin(client)
    page = client.get(f"/admin/clients/{branded.id}")
    assert page.status_code == 200
    assert b"Own sign-in address" in page.data and DOMAIN.encode() in page.data
