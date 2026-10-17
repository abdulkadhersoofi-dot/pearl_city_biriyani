"""Packages and powers, stage 6: the checks across every layer.

- A client can't reach any module page by its URL when their package
  leaves the module out - every route behind a module answers "not found",
  for reading and for saving.
- Each client sees only its own package rows, requests, switches and
  change history.
- Locked settings refuse their saves; the "Changes" list shows what was
  changed, when and by whom.
"""

from decimal import Decimal

import pytest

from app.models.audit_log import AuditLog
from app.models.package import ModuleRequest, Package, TenantPackage
from app.models.tenant import BillingCycle, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.utils.changes import client_changes, describe
from app.utils.entitlements import (
    ARFA, INCLUDED, OFF, SETTINGS, assign_package, request_module, set_auditor_powers, set_login_switches,
    set_module_exception,
)
from app.utils.features import FEATURES

# Module routes that check their module inside the view instead of with
# @feature_required - each was read and is listed here on purpose, so a new
# module page without a guard fails this test.
GUARDED_IN_VIEW = {
    "einvoice.index", "einvoice.log", "einvoice.settings",      # either e-Invoice or e-Way Bill (either_feature)
    "accounts.books_opening",                                  # any of the three account modules
    "accounts.shared_statement",                               # public signed link; needs receivables
}
MODULE_BLUEPRINTS = ("purchases", "expenses", "accounts", "sales", "loyalty", "jobs", "restaurant", "pharmacy",
                     "jewellery", "wholesale", "einvoice", "inventory")


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _url(app, rule):
    args = {a: 1 for a in rule.arguments}
    for a in rule.arguments:
        conv = rule._converters.get(a)
        if conv is not None and getattr(conv, "items", None):
            args[a] = sorted(conv.items)[0]                        # <any(pdf, xlsx):fmt>
        elif conv is not None and conv.__class__.__name__ not in ("IntegerConverter",):
            args[a] = "x"
    with app.test_request_context():
        from flask import url_for

        return url_for(rule.endpoint, **args)


def _module_rules(app):
    for rule in app.url_map.iter_rules():
        key = getattr(app.view_functions[rule.endpoint], "required_feature", None)
        if key:
            yield rule, key


def _nothing_package(db):
    p = Package(name="Nothing", modules={}, unlisted_modules=OFF, settings={}, limits={})
    db.session.add(p)
    db.session.commit()
    return p


# --- no module by its URL ------------------------------------------------------------------------

def test_every_module_page_is_guarded(app):
    unguarded = sorted(
        rule.endpoint for rule in app.url_map.iter_rules()
        if rule.endpoint.split(".")[0] in MODULE_BLUEPRINTS
        and not getattr(app.view_functions[rule.endpoint], "required_feature", None)
        and rule.endpoint not in GUARDED_IN_VIEW)
    assert unguarded == [], f"Module pages without @feature_required: {unguarded}"
    assert sum(1 for _ in _module_rules(app)) > 150


def test_a_client_cant_reach_a_module_outside_their_package_by_its_url(app, client, db, tenant, client_admin,
                                                                      super_admin):
    for key in FEATURES:
        tenant.set_feature(key, True)                  # switched on before - the package must still win
    db.session.commit()
    assign_package(tenant, _nothing_package(db), super_admin)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    reached = []
    for rule, key in _module_rules(app):
        url = _url(app, rule)
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            res = client.open(url, method=method, data={})
            if res.status_code != 404:
                reached.append(f"{method} {url} ({key}) -> {res.status_code}")
    for endpoint in GUARDED_IN_VIEW - {"accounts.shared_statement"}:
        rule = next(r for r in app.url_map.iter_rules() if r.endpoint == endpoint)
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            res = client.open(_url(app, rule), method=method, data={})
            if res.status_code != 404:
                reached.append(f"{method} {rule.rule} -> {res.status_code}")
    assert reached == []


def test_the_same_pages_open_when_the_package_includes_them(app, client, db, tenant, client_admin, super_admin):
    """The sweep above isn't empty-handed: with every module included, the
    pages that need no record open."""
    p = Package(name="All in", modules={k: INCLUDED for k in FEATURES}, unlisted_modules=INCLUDED, settings={}, limits={})
    db.session.add(p)
    db.session.commit()
    assign_package(tenant, p, super_admin)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    opened = 0
    for rule, key in _module_rules(app):
        if rule.arguments or "GET" not in rule.methods:
            continue
        if client.get(_url(app, rule)).status_code != 404:
            opened += 1
    assert opened > 50


def test_a_branch_login_cant_reach_a_module_hidden_at_its_counter(app, client, db, tenant, staff, super_admin):
    tenant.set_feature("job_cards", True)
    tenant.set_feature("restaurant_mode", True)
    db.session.commit()
    set_login_switches(staff, super_admin, hidden_modules=["job_cards", "restaurant_mode"])
    db.session.commit()
    _login(client, staff.email, "CashierSecret123")
    for rule, key in _module_rules(app):
        if key in ("job_cards", "restaurant_mode") and "GET" in rule.methods:
            assert client.get(_url(app, rule)).status_code in (403, 404), rule.rule


# --- refused saves ---------------------------------------------------------------------------------

def test_every_locked_setting_refuses_its_save(client, db, tenant, client_admin, super_admin):
    tenant.aato = Decimal("1000000")
    db.session.commit()
    p = Package(name="All locked", modules={}, unlisted_modules="choice", settings={k: ARFA for k in SETTINGS},
                limits={})
    db.session.add(p)
    db.session.commit()
    assign_package(tenant, p, super_admin)
    db.session.commit()
    before = (tenant.business_type, tenant.aato, tenant.default_price_includes_tax, tenant.primary_color,
              tenant.default_receipt_format)
    _login(client, client_admin.email, "ClientSecret123")
    assert client.post("/settings/appearance", data={"primary_color": "#000000"}).status_code == 403
    assert client.post("/settings/counter", data={"pos_payment_modes": ["cash"]}).status_code == 403
    assert client.post("/settings/security", data={"idle_minutes": "15"}).status_code == 403
    client.post("/settings/", data={"section": "business", "aato": "99999999", "business_type": "pharmacy",
                                    "default_price_includes_tax": "y", "apply_business_preset": "y"})
    client.post("/settings/documents", data={"default_receipt_format": "a4"})
    client.post("/settings/", data={"aato": "5", "business_type": "pharmacy", "primary_color": "#000000"})
    db.session.expire_all()
    assert (tenant.business_type, tenant.aato, tenant.default_price_includes_tax, tenant.primary_color,
            tenant.default_receipt_format) == before


def test_a_client_cant_switch_a_module_outside_their_package(client, db, tenant, client_admin, super_admin):
    assign_package(tenant, _nothing_package(db), super_admin)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    client.post("/settings/features", data={"feature_purchases": "y", "feature_loyalty": "y"})
    db.session.expire_all()
    assert not tenant.has_feature("purchases") and not tenant.has_feature("loyalty")


# --- each client only its own -----------------------------------------------------------------------

@pytest.fixture()
def other(db, super_admin):
    t = Tenant(legal_name="Bhavani Stores", registration_type=RegistrationType.REGULAR,
               onboarded_by_id=super_admin.id, billing_cycle=BillingCycle.MONTHLY)
    db.session.add(t)
    db.session.flush()
    db.session.add(Gstin(tenant_id=t.id, gstin=None, state_code="29", state_name="Karnataka", is_primary=True))
    admin = User(tenant_id=t.id, name="Bhavani Owner", email="owner@bhavani.example.com", role=UserRole.CLIENT_ADMIN,
                 must_change_password=False)
    admin.set_password("BhavaniSecret123")
    db.session.add(admin)
    db.session.commit()
    return t


def test_each_client_sees_only_its_own_grants(client, db, tenant, client_admin, other, super_admin):
    secret = Package(name="Acme special", modules={"loyalty": INCLUDED}, unlisted_modules="choice", settings={},
                     limits={})
    db.session.add(secret)
    db.session.commit()
    assign_package(tenant, secret, super_admin)
    set_module_exception(tenant, "purchases", OFF, super_admin)
    request_module(tenant, "pharmacy", client_admin)
    db.session.commit()
    _login(client, "owner@bhavani.example.com", "BhavaniSecret123")
    page = client.get("/settings/features").get_data(as_text=True)
    assert "Not in your package" not in page and "Acme special" not in page
    assert other.package_grant is None and other.has_feature("loyalty") is False
    # A client admin can't open the console's change history at all.
    assert client.get(f"/admin/clients/{tenant.id}/changes").status_code in (302, 403)
    assert client.get(f"/admin/clients/{other.id}/changes").status_code in (302, 403)
    rows, total = client_changes(other)
    assert total == 0 and rows == []
    assert TenantPackage.query.filter_by(tenant_id=other.id).count() == 0
    assert ModuleRequest.query.filter_by(tenant_id=other.id).count() == 0


def test_an_auditor_reads_only_their_own_clients_changes(client, db, tenant, other, super_admin):
    auditor = User(name="Aud", email="aud6@example.com", role=UserRole.AUDITOR, must_change_password=False,
                   billing_cycle=BillingCycle.MONTHLY)
    auditor.set_password("AuditorPass123")
    db.session.add(auditor)
    db.session.flush()
    other.auditor_id = auditor.id
    db.session.commit()
    assign_package(tenant, _nothing_package(db), super_admin)
    db.session.commit()
    _login(client, auditor.email, "AuditorPass123")
    assert client.get(f"/admin/clients/{other.id}/changes").status_code == 200
    assert client.get(f"/admin/clients/{tenant.id}/changes").status_code in (403, 404)


# --- the Changes list --------------------------------------------------------------------------------

def test_the_changes_list_shows_what_when_and_who(client, db, tenant, client_admin, staff, super_admin):
    retail = Package(name="Retail test", modules={"inventory": INCLUDED}, unlisted_modules=OFF, settings={},
                     limits={})
    db.session.add(retail)
    db.session.commit()
    assign_package(tenant, retail, super_admin)
    set_module_exception(tenant, "loyalty", "choice", super_admin)
    set_login_switches(staff, super_admin, permissions={"refund": False})
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    page = client.get(f"/admin/clients/{tenant.id}").get_data(as_text=True)
    assert 'id="sec-changes"' in page and "Put on the Retail test package" in page
    full = client.get(f"/admin/clients/{tenant.id}/changes").get_data(as_text=True)
    assert "Loyalty points: as the package → Client&#39;s choice" in full
    assert f"ARFA&#39;s switches for {staff.name}: Refund bills always off" in full
    assert super_admin.name in full
    only_logins = client.get(f"/admin/clients/{tenant.id}/changes?kind=logins").get_data(as_text=True)
    assert "switches for" in only_logins and "Put on the" not in only_logins
    # Editing the package the client is on shows in their list too.
    from app.utils.features import AVAILABLE_FEATURES
    client.post(f"/admin/packages/{retail.id}", data={"name": "Retail test", "unlisted_modules": "off",
                                                      **{f"mod_{f.key}": "inc" if f.key == "inventory" else "off"
                                                         for f in AVAILABLE_FEATURES}})
    rows, _ = client_changes(tenant)
    assert rows[0]["text"] == "The Retail test package itself was edited"


def test_client_side_changes_show_too(client, db, tenant, client_admin, super_admin, staff):
    _login(client, client_admin.email, "ClientSecret123")
    client.post("/settings/features", data={"feature_loyalty": "y"})
    client.post(f"/branches/{staff.id}/permissions", data={"role": "manager"})
    db.session.expire_all()
    assert staff.hidden_modules is None                    # a save without the module ticks hides nothing
    texts = [r["text"] for r in client_changes(tenant)[0]]
    assert any(t.startswith("Modules switched: Loyalty points on") for t in texts)
    assert any(t == f"Permissions for {staff.name}: Manager" for t in texts)


def test_auditor_power_changes_on_the_auditor_page(client, db, super_admin):
    auditor = User(name="Office", email="office6@example.com", role=UserRole.AUDITOR, must_change_password=False,
                   billing_cycle=BillingCycle.MONTHLY)
    auditor.set_password("AuditorPass123")
    db.session.add(auditor)
    db.session.commit()
    set_auditor_powers(auditor, {"act_as": False}, super_admin)
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    page = client.get(f"/admin/auditors/{auditor.id}").get_data(as_text=True)
    assert 'id="sec-power-changes"' in page and "Act as a client or branch login off" in page


def test_every_change_kind_reads_in_plain_words():
    for action, details in [
        ("limit_exception_set", {"limit": "branches", "from": "package", "to": 3}),
        ("security_exception_set", {"minimum": "idle_minutes", "from": "package", "to": 30}),
        ("counter_permissions_exception_set", {"to": ["refund"]}),
        ("branding_exception_set", {"from": None, "to": False}),
        ("module_request_resolved", {"module": "loyalty", "action": "decline"}),
        ("client_modules_switched", {"changed": ["loyalty"], "on": ["loyalty"]}),
        ("security_settings_saved", {"idle_minutes": 30}),
    ]:
        text = describe(AuditLog(action=action, details=details))
        assert "_" not in text and text[0].isupper(), (action, text)
