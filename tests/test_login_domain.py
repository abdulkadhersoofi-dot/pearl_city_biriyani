"""Sign-in domains (app/auth/login_domain.py, app/tenants/domains.py): a
domain that shows only a sign-in page for the clients the Ultra Admin has
let onto it - every login of those clients - then hands over to the main
address, where the user is actually signed in."""

import re
from datetime import timedelta

import pytest

from app.auth import security
from app.models.audit_log import AuditLog
from app.models.mixins import utcnow
from app.models.platform import LoginHandoff, SignInDomain
from app.models.tenant import BillingCycle, RegistrationType, Tenant
from app.models.user import User, UserRole

DOMAIN = "pcbinvoice.com"
OWN = f"https://{DOMAIN}"


@pytest.fixture()
def domain(db, tenant, super_admin):
    tenant.trade_name = "Pearl City Biriyani"
    row = SignInDomain(domain=DOMAIN, created_by_id=super_admin.id)
    db.session.add(row)
    db.session.flush()
    tenant.sign_in_domain_id = row.id
    db.session.commit()
    return row


def _other_client(db, super_admin, name="Other Co", email="owner@other.example.com"):
    other = Tenant(legal_name=name, registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
                   billing_cycle=BillingCycle.MONTHLY)  # verified, like the main test client
    db.session.add(other)
    db.session.flush()
    user = User(tenant_id=other.id, name="Other", email=email, role=UserRole.CLIENT_ADMIN, must_change_password=False)
    user.set_password("OtherSecret123")
    db.session.add(user)
    db.session.commit()
    return other, user


def _signed_in_id(client):
    with client.session_transaction() as sess:
        return sess.get("_user_id")


def _sign_in_on_domain(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, base_url=OWN)


def _token(page) -> str:
    match = re.search(rb'name="token" value="([^"]+)"', page.data)
    assert match, page.data[:400]
    return match.group(1).decode()


def _hand_over(client, email, password):
    return client.post("/auth/handoff", data={"token": _token(_sign_in_on_domain(client, email, password))})


# --- the sign-in page ------------------------------------------------------------------------

def test_the_domain_shows_its_clients_sign_in_page(client, domain):
    res = client.get("/", base_url=OWN)
    assert res.status_code == 302 and res.headers["Location"].endswith("/auth/login")
    page = client.get("/auth/login", base_url=OWN)
    assert page.status_code == 200 and b"Pearl City Biriyani" in page.data


def test_everything_else_on_the_domain_goes_to_the_main_address(app, client, domain):
    res = client.get("/invoicing/?page=2", base_url=OWN)
    assert res.status_code == 302
    assert res.headers["Location"] == app.config["PUBLIC_URL"].rstrip("/") + "/invoicing/?page=2"
    assert client.get("/health", base_url=OWN).status_code == 200


def test_with_several_clients_the_look_is_the_chosen_one(client, db, domain, super_admin):
    other, _ = _other_client(db, super_admin)
    other.sign_in_domain_id = domain.id
    db.session.commit()
    page = client.get("/auth/login", base_url=OWN)
    assert b"Pearl City Biriyani" not in page.data  # two clients, none chosen: ARFA's own look
    domain.brand_tenant_id = other.id
    db.session.commit()
    assert b"Other Co" in client.get("/auth/login", base_url=OWN).data


# --- who signs in there ----------------------------------------------------------------------

def test_the_owner_signs_in_on_the_domain_and_lands_on_the_main_address(app, client, db, domain, client_admin):
    page = _sign_in_on_domain(client, client_admin.email, "ClientSecret123")
    assert page.status_code == 200
    assert (app.config["PUBLIC_URL"].rstrip("/") + "/auth/handoff").encode() in page.data
    token = _token(page)
    assert token not in LoginHandoff.query.one().token_hash  # only the hash is kept
    assert _signed_in_id(client) is None  # nothing signed in on the sign-in domain

    res = client.post("/auth/handoff", data={"token": token})
    assert res.status_code == 302
    assert _signed_in_id(client) == str(client_admin.id)
    row = AuditLog.query.filter_by(action="login_success", actor_user_id=client_admin.id).one()
    assert row.details["method"] == "own_address"

    client.get("/auth/logout")
    client.post("/auth/handoff", data={"token": token})  # used once only
    assert _signed_in_id(client) is None


def test_every_branch_login_of_the_client_signs_in_there_too(client, domain, staff):
    _hand_over(client, staff.email, "CashierSecret123")
    assert _signed_in_id(client) == str(staff.id)


def test_the_ultra_admin_signs_in_on_any_domain(client, domain, super_admin):
    _hand_over(client, super_admin.email, "SuperSecret123")
    assert _signed_in_id(client) == str(super_admin.id)


def test_logins_of_clients_not_on_the_domain_are_sent_to_the_main_address(client, db, domain, super_admin):
    _, stranger = _other_client(db, super_admin)
    page = _sign_in_on_domain(client, stranger.email, "OtherSecret123")
    assert b"doesn&#39;t sign in here" in page.data or b"doesn't sign in here" in page.data
    assert LoginHandoff.query.count() == 0


def test_a_second_client_on_the_same_domain_signs_in_there(client, db, domain, super_admin):
    other, user = _other_client(db, super_admin)
    other.sign_in_domain_id = domain.id
    db.session.commit()
    _hand_over(client, user.email, "OtherSecret123")
    assert _signed_in_id(client) == str(user.id)


def test_a_wrong_password_on_the_domain_is_just_wrong(client, domain, client_admin):
    page = _sign_in_on_domain(client, client_admin.email, "nope-nope-1")
    assert b"Incorrect email or password" in page.data
    assert LoginHandoff.query.count() == 0


def test_two_step_sign_in_happens_on_the_domain_then_hands_over(client, db, domain, client_admin):
    secret = security.new_secret()
    security.enable_two_factor(client_admin, secret)
    client_admin.totp_last_step = None
    db.session.commit()

    res = _sign_in_on_domain(client, client_admin.email, "ClientSecret123")
    assert res.status_code == 302 and res.headers["Location"].endswith("/auth/two-factor")
    page = client.get("/auth/two-factor", base_url=OWN)
    assert page.status_code == 200 and b"Pearl City Biriyani" in page.data  # still the domain's look
    page = client.post("/auth/two-factor", data={"code": security.code_for(secret)}, base_url=OWN)
    client.post("/auth/handoff", data={"token": _token(page)})
    assert _signed_in_id(client) == str(client_admin.id)


def test_signing_out_goes_back_to_the_domain(client, domain, client_admin):
    _hand_over(client, client_admin.email, "ClientSecret123")
    assert client.get("/auth/logout").headers["Location"] == f"{OWN}/auth/login"


# --- the hand-over ---------------------------------------------------------------------------

def test_a_hand_over_expires_after_a_minute(client, db, domain, client_admin):
    token = _token(_sign_in_on_domain(client, client_admin.email, "ClientSecret123"))
    row = LoginHandoff.query.one()
    row.expires_at = utcnow() - timedelta(seconds=1)
    db.session.commit()
    res = client.post("/auth/handoff", data={"token": token}, follow_redirects=True)
    assert _signed_in_id(client) is None and b"sign in again" in res.data


def test_a_hand_over_cant_be_guessed_or_fetched(client, domain):
    client.post("/auth/handoff", data={"token": "made-up"})
    assert _signed_in_id(client) is None
    assert client.get("/auth/handoff").status_code == 302


def test_a_hand_over_is_refused_on_the_sign_in_domain_itself(client, domain, client_admin):
    token = _token(_sign_in_on_domain(client, client_admin.email, "ClientSecret123"))
    res = client.post("/auth/handoff", data={"token": token}, base_url=OWN)
    assert res.status_code == 302  # sent on to the main address, token not spent
    assert LoginHandoff.query.one().used_at is None


def test_a_switched_off_domain_sends_people_to_the_main_sign_in(app, client, db, domain):
    domain.is_active = False
    db.session.commit()
    res = client.get("/auth/login", base_url=OWN)
    assert res.headers["Location"] == app.config["PUBLIC_URL"].rstrip("/") + "/auth/login"


def test_the_main_address_is_unchanged(client, domain, client_admin):
    assert b"Pearl City Biriyani" not in client.get("/auth/login").data
    client.post("/auth/login", data={"email": client_admin.email, "password": "ClientSecret123"})
    assert _signed_in_id(client) == str(client_admin.id)
    assert LoginHandoff.query.count() == 0


# --- the Ultra Admin manages them --------------------------------------------------------------

def _admin(client):
    client.post("/auth/login", data={"email": "firm@example.com", "password": "SuperSecret123"})


def test_ultra_admin_adds_a_domain_and_lets_clients_onto_it(client, db, tenant, super_admin):
    _admin(client)
    res = client.post("/admin/sign-in-domains", data={"domain": "https://PCBInvoice.com/login"})
    row = SignInDomain.query.one()
    assert row.domain == DOMAIN and res.headers["Location"].endswith(f"/admin/sign-in-domains/{row.id}")
    client.post(f"/admin/sign-in-domains/{row.id}", data={"action": "add_client", "tenant_id": tenant.id})
    db.session.refresh(tenant)
    assert tenant.sign_in_domain_id == row.id
    assert AuditLog.query.filter_by(action="tenant_sign_in_domain_changed", tenant_id=tenant.id).count() == 1
    page = client.get(f"/admin/sign-in-domains/{row.id}")
    assert page.status_code == 200 and tenant.display_name.encode() in page.data

    client.post(f"/admin/sign-in-domains/{row.id}", data={"action": "remove_client", "tenant_id": tenant.id})
    db.session.refresh(tenant)
    assert tenant.sign_in_domain_id is None


@pytest.mark.parametrize("value", ["not a domain", "arfa.live", "x.arfa.live", "someone.web.app", "localhost"])
def test_unusable_domains_are_refused(client, super_admin, value):
    _admin(client)
    client.post("/admin/sign-in-domains", data={"domain": value})
    assert SignInDomain.query.count() == 0


def test_a_domain_is_added_once(client, domain):
    _admin(client)
    client.post("/admin/sign-in-domains", data={"domain": DOMAIN})
    assert SignInDomain.query.count() == 1


def test_any_client_can_be_moved_onto_a_domain_from_its_page(client, db, domain, super_admin):
    other, _ = _other_client(db, super_admin, name="Plain Client")
    _admin(client)
    page = client.get(f"/admin/clients/{other.id}")
    assert b"Sign-in domain" in page.data and DOMAIN.encode() in page.data
    client.post(f"/admin/clients/{other.id}/sign-in-domain", data={"domain_id": domain.id})
    db.session.refresh(other)
    assert other.sign_in_domain_id == domain.id
    client.post(f"/admin/clients/{other.id}/sign-in-domain", data={"domain_id": ""})
    db.session.refresh(other)
    assert other.sign_in_domain_id is None


def test_moving_a_client_to_another_domain_takes_its_look_with_it(client, db, domain, tenant, super_admin):
    domain.brand_tenant_id = tenant.id
    second = SignInDomain(domain="login.second-brand.com")
    db.session.add(second)
    db.session.commit()
    _admin(client)
    client.post(f"/admin/sign-in-domains/{second.id}", data={"action": "add_client", "tenant_id": tenant.id})
    db.session.refresh(tenant)
    db.session.refresh(domain)
    assert tenant.sign_in_domain_id == second.id and domain.brand_tenant_id is None


def test_the_look_must_be_one_of_the_domains_clients(client, db, domain, super_admin):
    other, _ = _other_client(db, super_admin)
    _admin(client)
    client.post(f"/admin/sign-in-domains/{domain.id}", data={"action": "settings", "brand_tenant_id": other.id,
                                                            "is_active": "1"})
    db.session.refresh(domain)
    assert domain.brand_tenant_id is None


def test_switching_a_domain_off_and_removing_it(client, db, domain, tenant):
    _admin(client)
    client.post(f"/admin/sign-in-domains/{domain.id}", data={"action": "settings", "brand_tenant_id": tenant.id})
    db.session.refresh(domain)
    assert domain.is_active is False and domain.brand_tenant_id == tenant.id
    client.post(f"/admin/sign-in-domains/{domain.id}", data={"action": "delete"})
    assert SignInDomain.query.count() == 0
    db.session.refresh(tenant)
    assert tenant.sign_in_domain_id is None


def test_only_the_ultra_admin_manages_sign_in_domains(client, db, domain, tenant, client_admin):
    client.post("/auth/login", data={"email": client_admin.email, "password": "ClientSecret123"})
    assert client.get("/admin/sign-in-domains").status_code == 403
    assert client.post("/admin/sign-in-domains", data={"domain": "other-brand.com"}).status_code == 403
    assert client.post(f"/admin/clients/{tenant.id}/sign-in-domain", data={"domain_id": ""}).status_code == 403
