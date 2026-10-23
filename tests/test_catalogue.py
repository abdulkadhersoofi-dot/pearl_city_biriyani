"""Console cleanup, stage 1: the one catalogue. Every switch is sorted into
its kind (trade pack, module in one of six areas, counter option), each
business type brings its trade pack(s) and suggests a package, and the
package editor, a client's console page, onboarding and the client's own
Settings > Modules all show that one list in the same order - without
changing what any client has."""

import pytest

from app.models.package import Package
from app.models.tenant import BusinessType
from app.utils import catalogue
from app.utils.catalogue import AREAS, BUSINESS_TYPE_PACKAGE, BUSINESS_TYPE_PACKS, CATALOGUE, COUNTER, MODULE, TRADE
from app.utils.entitlements import STARTING_PACKAGES, assign_package, ensure_starting_packages
from app.utils.features import AVAILABLE_FEATURES, BUSINESS_TYPE_PRESETS, FEATURES


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


@pytest.fixture()
def packs(db):
    ensure_starting_packages()
    db.session.commit()
    return {p.key: p for p in Package.query.all()}


# --- the catalogue itself ---------------------------------------------------------------------------

def test_every_switch_has_one_place():
    keys = catalogue.ordered_keys()
    assert sorted(keys) == sorted(f.key for f in AVAILABLE_FEATURES)
    assert len(keys) == len(set(keys))
    for key, e in CATALOGUE.items():
        assert key in FEATURES
        assert e.kind in (TRADE, MODULE, COUNTER)
        assert (e.area in AREAS) if e.kind == MODULE else e.area is None
        assert all(k in FEATURES for k in e.needs)
        if e.kind == TRADE:
            assert e.short


def test_the_kinds_come_in_order_and_modules_in_six_areas():
    groups = catalogue.groups()
    kinds = [g["kind"] for g in groups]
    assert kinds == sorted(kinds, key=[TRADE, MODULE, COUNTER].index)
    assert [g["key"] for g in groups if g["kind"] == MODULE] == list(AREAS)
    assert [g["title"] for g in groups if g["kind"] == MODULE] == [
        "Selling", "Buying & suppliers", "Stock", "Accounts", "Reports", "GST compliance"]
    assert [g["first"] for g in groups].count(True) == 3


def test_what_the_owner_called_mixed_up_is_now_apart():
    """Suppliers sit together under Buying, stock tools under Stock, trades
    under Trade packs and counter switches under Counter options."""
    where = {item["feature"].key: (g["kind"], g["key"]) for g in catalogue.groups() for item in g["items"]}
    assert where["purchases"] == where["payables"] == (MODULE, "buying")
    assert where["inventory"] == where["batch_expiry"] == where["serial_imei"] == (MODULE, "stock")
    for key in ("restaurant_mode", "pharmacy", "jewellery", "electronics", "job_cards", "wholesale", "weighing",
                "margin_scheme"):
        assert where[key][0] == TRADE
    for key in ("favourites", "day_close", "cashier_pin", "offline_pos"):
        assert where[key][0] == COUNTER
    assert where["einvoice"] == where["eway_bill"] == (MODULE, "gst")


def test_an_add_on_sits_right_under_the_module_it_needs():
    stock = next(g for g in catalogue.groups() if g["key"] == "stock")
    order = [(i["feature"].key, i["depth"]) for i in stock["items"]]
    at = [k for k, _ in order].index("inventory")
    assert order[at + 1:at + 3] == [("batch_expiry", 1), ("serial_imei", 1)]
    assert catalogue.missing_needs("batch_expiry", lambda k: k != "inventory") == ["Inventory"]
    assert catalogue.missing_needs("batch_expiry", lambda k: True) == []


def test_a_switch_not_yet_placed_still_shows_under_modules(monkeypatch):
    from app.utils.features import Feature

    extra = Feature("brand_new", "Brand new", "Not placed yet.", available=True)
    monkeypatch.setattr(catalogue, "AVAILABLE_FEATURES", AVAILABLE_FEATURES + [extra])
    monkeypatch.setitem(catalogue.FEATURES, "brand_new", extra)
    groups = catalogue.groups()
    other = next(g for g in groups if g["key"] == "other")
    assert other["kind"] == MODULE and [i["feature"].key for i in other["items"]] == ["brand_new"]
    assert groups.index(other) < next(i for i, g in enumerate(groups) if g["kind"] == COUNTER)


def test_each_business_type_brings_its_trade_packs_and_a_real_package():
    keys = {spec["key"] for spec in STARTING_PACKAGES}
    for bt in BusinessType:
        assert set(BUSINESS_TYPE_PACKS[bt]) <= BUSINESS_TYPE_PRESETS[bt], bt
        assert all(CATALOGUE[k].kind == TRADE for k in BUSINESS_TYPE_PACKS[bt])
        assert BUSINESS_TYPE_PACKAGE[bt] in keys
    pharmacy = next(b for b in catalogue.business_types() if b["value"] == "pharmacy")
    assert pharmacy["packs"] == ["Pharmacy"] and pharmacy["package_key"] == "pharmacy"
    mobiles = next(b for b in catalogue.business_types() if b["value"] == "mobile_electronics")
    assert mobiles["packs"] == ["Mobiles & electronics", "Repairs", "Second-hand goods"]


# --- the screens read it ----------------------------------------------------------------------------

def _positions(html, *needles):
    return [html.index(n) for n in needles]


def test_the_package_editor_is_sorted_by_kind(client, db, super_admin, packs):
    _login(client, super_admin.email, "SuperSecret123")
    html = client.get(f"/admin/packages/{packs['retail'].id}").get_data(as_text=True)
    # In tabs since console cleanup stage 4: one tab per kind, areas in order inside Modules.
    a = _positions(html, 'id="ptab-trade"', 'id="ptab-modules"', ">Selling</h4>", ">Buying &amp; suppliers</h4>",
                   ">Stock</h4>", ">Accounts</h4>", ">Reports</h4>", ">GST compliance</h4>", 'id="ptab-counter"')
    assert a == sorted(a)
    trade = html.split('id="ptab-trade"', 1)[1].split('id="ptab-modules"', 1)[0]
    assert 'id="lbl_pharmacy"' in trade and 'id="lbl_inventory"' not in trade
    # Supplier accounts is listed next to Purchases, not under money and reports.
    assert html.index('id="lbl_purchases"') < html.index('id="lbl_payables"') < html.index(">Stock</h4>")
    # Batch & expiry is an add-on under Inventory, with its warning ready.
    row = html.split('id="lbl_batch_expiry"', 1)[0].rsplit('<div class="state-row', 1)[1]
    assert row.startswith(" is-addon")
    assert "Works with Inventory" in html.split('id="lbl_batch_expiry"', 1)[1].split("state-seg", 1)[0]


def test_saving_the_editor_still_keeps_every_module(client, db, super_admin, packs):
    """Re-sorting changed no field: a save without changes keeps the package as it was."""
    _login(client, super_admin.email, "SuperSecret123")
    retail = packs["retail"]
    before = dict(retail.modules)
    from app.utils.entitlements import package_module_state

    data = {"name": retail.name, "description": retail.description or "", "price_monthly": "", "price_yearly": "",
            "unlisted_modules": retail.unlisted_modules, "own_branding": "y" if retail.own_branding else ""}
    for f in AVAILABLE_FEATURES:
        data[f"mod_{f.key}"] = package_module_state(retail, f.key)
    for key, value in (retail.limits or {}).items():
        data[f"limit_{key}"] = str(value)
    for key, value in (retail.settings or {}).items():
        data[f"set_{key}"] = value
    assert client.post(f"/admin/packages/{retail.id}", data=data).status_code == 302
    db.session.refresh(retail)
    assert {k: package_module_state(retail, k) for k in before} == before


def test_the_client_page_shows_kinds_and_their_own_choices_by_kind(client, db, tenant, super_admin, packs):
    assign_package(tenant, packs["retail"], super_admin)
    tenant.set_feature("batch_expiry", True)   # an own-choice add-on... with Inventory included
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    html = client.get(f"/admin/clients/{tenant.id}").get_data(as_text=True)
    # The Ultra Admin's one module table (Off / On / Client decides), by kind.
    table = html.split('class="module-locks no-stack"', 1)[1].split("</table>", 1)[0]
    a = _positions(table, 'class="kind-row" x-show="!q"><th colspan="3" scope="colgroup">Trade packs',
                   'scope="colgroup">Modules', 'scope="colgroup">Selling', 'scope="colgroup">Counter options')
    assert a == sorted(a)
    tenant.set_feature("loyalty", True)   # the client's own choice in Retail, switched on
    db.session.commit()
    html = client.get(f"/admin/clients/{tenant.id}").get_data(as_text=True)
    table = html.split('class="module-locks no-stack"', 1)[1].split("</table>", 1)[0]
    assert 'name="mod_loyalty" value="choice" checked' in table and 'name="on_loyalty" value="1" checked' in table
    assert 'name="mod_inventory" value="inc" checked' in table and 'name="mod_batch_expiry" value="off" checked' in table


def test_a_client_page_warns_when_an_add_on_is_on_without_its_module(client, db, tenant, super_admin):
    tenant.features = {"batch_expiry": True}   # no package: every module their own choice
    db.session.commit()
    _login(client, super_admin.email, "SuperSecret123")
    html = client.get(f"/admin/clients/{tenant.id}").get_data(as_text=True)
    assert "Works with Inventory - off for them" in html


def test_onboarding_says_what_the_business_type_brings(client, db, super_admin):
    _login(client, super_admin.email, "SuperSecret123")
    html = client.get("/admin/clients/new").get_data(as_text=True)
    assert "No trade pack of its own" in html          # retail, the default
    assert "&#34;pharmacy&#34;: [&#34;Pharmacy&#34;]" in html or '"pharmacy": ["Pharmacy"]' in html


def test_settings_modules_follows_the_catalogue(client, db, tenant, client_admin):
    tenant.business_type = BusinessType.PHARMACY
    tenant.features = {"pharmacy": True, "batch_expiry": True, "inventory": True, "favourites": True,
                       "purchases": True}
    db.session.commit()
    before = dict(tenant.features)
    _login(client, client_admin.email, "ClientSecret123")
    html = client.get("/settings/features").get_data(as_text=True)
    assert "Business type: <strong>Pharmacy / medical store</strong>" in html
    assert "comes with the Pharmacy pack" in html
    a = _positions(html, 'class="kind-heading" x-show="!q">Trade packs', 'class="kind-heading" x-show="!q">Modules',
                   '<h3 class="module-group-title">Buying &amp; suppliers', '<h3 class="module-group-title">Stock',
                   'class="kind-heading" x-show="!q">Counter options')
    assert a == sorted(a)
    assert "Works with" not in html          # everything Pharmacy needs is on
    tenant.set_feature("inventory", False)
    db.session.commit()
    html = client.get("/settings/features").get_data(as_text=True)
    assert "Works with Inventory - switch it on too" in html
    db.session.refresh(tenant)
    assert {k: bool(v) for k, v in tenant.features.items()} == {**before, "inventory": False}
