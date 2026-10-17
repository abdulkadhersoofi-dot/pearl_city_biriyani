"""The counter (redesign stage 5): full-screen layout, Counter menu, held
bills in a drawer with their count on the bar, category chips, one-tap
payment - and the counter's own script, run in Node, filtering by chip
and going straight to the confirm step."""

import json
import re
import shutil
import subprocess
from decimal import Decimal

import pytest

from app.models.category import Category, SubCategory
from app.models.product import Product
from app.pos.services import CartInput, CartLineInput, hold_cart

D = Decimal


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


@pytest.fixture()
def menu(db, tenant):
    mains = Category(tenant_id=tenant.id, name="Mains")
    drinks = Category(tenant_id=tenant.id, name="Drinks")
    db.session.add_all([mains, drinks])
    db.session.flush()
    rice = SubCategory(tenant_id=tenant.id, category_id=mains.id, name="Rice")
    db.session.add(rice)
    db.session.flush()
    items = [
        Product(tenant_id=tenant.id, name="Chicken Biriyani", hsn_or_sac_code="996331", gst_rate=D("5"), unit="NOS",
                sale_price=D("150"), category_id=mains.id, sub_category_id=rice.id),
        Product(tenant_id=tenant.id, name="Parotta", hsn_or_sac_code="996331", gst_rate=D("5"), unit="NOS",
                sale_price=D("20"), category_id=mains.id),
        Product(tenant_id=tenant.id, name="Lime Soda", hsn_or_sac_code="2202", gst_rate=D("5"), unit="NOS",
                sale_price=D("40"), category_id=drinks.id),
        Product(tenant_id=tenant.id, name="Pickle", hsn_or_sac_code="2001", gst_rate=D("5"), unit="NOS",
                sale_price=D("10")),
    ]
    db.session.add_all(items)
    db.session.commit()
    return {"mains": mains, "drinks": drinks, "rice": rice, "items": items}


def test_counter_is_full_screen_with_its_own_bar(client, db, tenant, client_admin, menu):
    _login(client, client_admin.email, "ClientSecret123")
    html = client.get("/pos/").get_data(as_text=True)
    assert re.search(r'<body class="[^"]*\bis-counter\b', html)
    assert 'class="pos-bar"' in html and "Held bills" in html and 'class="pos-count">0<' in html
    assert 'class="pay-row"' in html and "quickPay(m.value)" in html
    assert "Day-book" in html and "Z-report" in html and "Keyboard shortcuts" in html
    # Other counter pages keep the normal frame.
    day_book = client.get("/pos/bills").get_data(as_text=True)
    assert not re.search(r'<body class="[^"]*\bis-counter\b', day_book)


def test_held_bills_sit_in_the_drawer_with_their_count_on_the_bar(client, db, tenant, client_admin, menu):
    line = CartLineInput(description="Parotta", hsn_or_sac_code="996331", qty=D("2"), rate=D("20"), gst_rate=D("5"),
                         product_id=menu["items"][1].id, unit="NOS")
    hold_cart(tenant, client_admin, CartInput(lines=[line]), "Table 4")
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    html = client.get("/pos/").get_data(as_text=True)
    assert 'pos-held-btn has-held' in html and 'class="pos-count">1<' in html
    drawer = html.split('class="held-drawer"', 1)[1]
    assert "Table 4" in drawer and "Resume" in drawer and "Discard" in drawer


def test_chips_only_for_a_client_with_categories(client, db, tenant, client_admin, menu):
    _login(client, client_admin.email, "ClientSecret123")
    assert "const CATEGORY_WISE = false;" in client.get("/pos/").get_data(as_text=True)
    tenant.set_feature("categories", True)
    db.session.commit()
    assert "const CATEGORY_WISE = true;" in client.get("/pos/").get_data(as_text=True)


def test_branch_login_gets_the_same_counter(client, db, tenant, staff, menu):
    _login(client, staff.email, "CashierSecret123")
    html = client.get("/pos/").get_data(as_text=True)
    assert 'class="pos-bar"' in html and 'class="sidenav"' not in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_counter_script_filters_by_chip_and_pays_in_one_tap(client, db, tenant, client_admin, menu, tmp_path):
    """Runs the page's own inline scripts in Node (with a bare window /
    document stand-in) and drives posTerminal() directly."""
    tenant.set_feature("categories", True)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    html = client.get("/pos/").get_data(as_text=True)
    scripts = [m.group(1) for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
               if m.group(1).strip() and "palette-data" not in m.group(0)]
    stub = """
var window = globalThis; var self = globalThis;
var document = {addEventListener(){}, querySelector(){return null}, querySelectorAll(){return []},
  getElementById(){return null}, createElement(){return {style:{}, setAttribute(){}, appendChild(){}}},
  documentElement: {classList: {add(){}, toggle(){}, contains(){return false}}}, body: {appendChild(){}}};
var navigator = {onLine: true}; var localStorage = {getItem(){return null}, setItem(){}};
window.matchMedia = () => ({matches: false}); window.addEventListener = () => {};
var GstCalc = globalThis.GstCalc || {round2: x => Math.round(x * 100) / 100,
  line: (q, r) => ({taxable: q * r, cgst: 0, sgst: 0, igst: 0, total: q * r})};
"""
    ids = {k: menu[k].id for k in ("mains", "drinks", "rice")}
    driver = """
const t = posTerminal();
t.$refs = {}; t.$nextTick = f => f && f(); t.$watch = () => {};
const names = list => list.map(p => p.name).sort();
const out = {};
out.all = names(t.shownProducts());
t.pickCat(%(mains)d); out.mains = names(t.shownProducts()); out.subs = t.activeSubs().map(s => s.name);
t.activeSub = %(rice)d; out.rice = names(t.shownProducts());
t.pickCat(%(drinks)d); out.drinks = names(t.shownProducts()); out.subAfterPick = t.activeSub;
t.pickCat("uncat"); out.other = names(t.shownProducts());
t.search = "soda"; out.search = names(t.shownProducts()); t.search = "";
const p = PRODUCTS.find(x => x.name === "Parotta");
t.addToCart(p); t.addToCart(p);
t.quickPay(t.paymentModes[0].value);
out.step = t.checkoutStep; out.mode = t.paymentMode; out.qty = t.cart[0].qty;
console.log(JSON.stringify(out));
""" % ids
    path = tmp_path / "counter.js"
    path.write_text(stub + "\n;\n".join(scripts) + "\n" + driver)
    result = subprocess.run(["node", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["all"] == ["Chicken Biriyani", "Lime Soda", "Parotta", "Pickle"]
    assert out["mains"] == ["Chicken Biriyani", "Parotta"] and out["subs"] == ["Rice"]
    assert out["rice"] == ["Chicken Biriyani"]
    assert out["drinks"] == ["Lime Soda"] and out["subAfterPick"] is None
    assert out["other"] == ["Pickle"]
    assert out["search"] == ["Lime Soda"]
    assert out["step"] == "confirm" and out["mode"] == "cash" and out["qty"] == 2
