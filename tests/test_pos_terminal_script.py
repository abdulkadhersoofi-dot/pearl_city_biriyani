"""The POS terminal is one big Alpine component fed by a script block the
server fills in from the client's own data. A single bad value in that
block (an HTML-escaped quote, a Python None, a stray comma) is a
JavaScript syntax error, and the whole screen then renders blank - no
items, no totals - for that client. So: render the page for a range of
client setups and have Node syntax-check every inline script."""

import re
import shutil
import subprocess
from decimal import Decimal

import pytest

from app.models.category import Category, SubCategory
from app.models.customer import Customer
from app.models.pos_bill import PaymentMode, POSBill
from app.models.price_list import PriceList, PriceListItem
from app.models.product import Product
from app.pos.services import CartInput, CartLineInput, checkout, hold_cart

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _inline_scripts(html: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S) if m.group(1).strip()]


def _assert_scripts_parse(html: str, tmp_path):
    scripts = _inline_scripts(html)
    assert scripts, "the POS page has no inline scripts"
    for i, source in enumerate(scripts):
        assert "&#34;" not in source and "&quot;" not in source and "&#39;" not in source, "HTML-escaped text inside a script"
        path = tmp_path / f"inline_{i}.js"
        path.write_text(source)
        result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, f"inline script {i} is not valid JavaScript:\n{result.stderr}"


@pytest.fixture()
def menu(db, tenant):
    items = [
        Product(tenant_id=tenant.id, name="Chicken Biriyani", hsn_or_sac_code="996331", gst_rate=Decimal("5"), unit="NOS", sale_price=Decimal("150")),
        Product(tenant_id=tenant.id, name="Loose Rice", hsn_or_sac_code="1006", gst_rate=Decimal("5"), unit="KGS", sale_price=Decimal("60"), price_includes_tax=True, sku="R1", barcode="8901"),
        Product(tenant_id=tenant.id, name='Quote "Dosa" & O\'Neil <b>', hsn_or_sac_code="2106", gst_rate=Decimal("5"), unit="NOS", sale_price=Decimal("40")),
    ]
    db.session.add_all(items)
    db.session.commit()
    return items


def test_default_client_without_upi_or_modules(client, db, tenant, client_admin, menu, tmp_path):
    assert tenant.upi_vpa is None
    _login(client, client_admin.email, "ClientSecret123")
    _assert_scripts_parse(client.get("/pos/").data.decode(), tmp_path)


def test_branch_login(client, db, tenant, staff, menu, tmp_path):
    _login(client, staff.email, "CashierSecret123")
    _assert_scripts_parse(client.get("/pos/").data.decode(), tmp_path)


def test_everything_switched_on(client, db, tenant, client_admin, staff, menu, tmp_path):
    tenant.upi_vpa = "shop@okhdfcbank"
    tenant.pos_payment_modes = ["cash", "upi", "card", "wallet", "credit"]
    for flag in ("categories", "favourites", "credit_sales", "multi_price_list", "day_close"):
        tenant.set_feature(flag, True)
    category = Category(tenant_id=tenant.id, name="Mains")
    db.session.add(category)
    db.session.flush()
    db.session.add(SubCategory(tenant_id=tenant.id, category_id=category.id, name="Rice"))
    menu[0].category_id = category.id
    menu[1].is_favourite = True
    price_list = PriceList(tenant_id=tenant.id, name="Wholesale")
    db.session.add(price_list)
    db.session.flush()
    db.session.add(PriceListItem(tenant_id=tenant.id, price_list_id=price_list.id, product_id=menu[0].id, price=Decimal("120")))
    staff.price_list_id = price_list.id
    staff.can_give_discount = True
    staff.max_discount_percent = Decimal("12.5")
    staff.can_edit_price_at_pos = True
    db.session.commit()
    for user, password in ((client_admin, "ClientSecret123"), (staff, "CashierSecret123")):
        client.get("/auth/logout")
        _login(client, user.email, password)
        _assert_scripts_parse(client.get("/pos/").data.decode(), tmp_path)


def test_resuming_a_held_bill_with_a_customer_and_discounts(client, db, tenant, client_admin, menu, tmp_path):
    ravi = Customer(tenant_id=tenant.id, name='Ravi "Traders"', phone="9876543210", gstin="29ABCDE1234F1Z5")
    db.session.add(ravi)
    db.session.commit()
    line = CartLineInput(
        description=menu[1].name, hsn_or_sac_code="1006", qty=Decimal("0.75"), rate=Decimal("60"), gst_rate=Decimal("5"),
        product_id=menu[1].id, unit="KGS", discount_kind="amount", discount_value=Decimal("5"),
    )
    held = hold_cart(tenant, client_admin, CartInput(lines=[line], customer_id=ravi.id, bill_discount_kind="percent", bill_discount_value=Decimal("5")), "T4")
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    html = client.get(f"/pos/?resume={held.id}").data.decode()
    _assert_scripts_parse(html, tmp_path)
    assert "Ravi" in html


def test_after_a_sale_with_a_previous_bill(client, db, tenant, client_admin, menu, tmp_path):
    line = CartLineInput(description=menu[0].name, hsn_or_sac_code="996331", qty=Decimal("1"), rate=Decimal("150"), gst_rate=Decimal("5"), product_id=menu[0].id, unit="NOS")
    checkout(tenant, client_admin, CartInput(lines=[line]), PaymentMode.CASH)
    db.session.commit()
    assert POSBill.query.count() == 1
    _login(client, client_admin.email, "ClientSecret123")
    html = client.get("/pos/").data.decode()
    assert "Reprint last bill" in html
    _assert_scripts_parse(html, tmp_path)


def test_invoice_form_scripts_also_parse(client, db, tenant, client_admin, menu, tmp_path):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get("/invoices/new")
    assert resp.status_code == 200
    _assert_scripts_parse(resp.data.decode(), tmp_path)


def test_pos_shows_which_version_is_running(client, db, tenant, client_admin, menu):
    _login(client, client_admin.email, "ClientSecret123")
    html = client.get("/pos/").data.decode()
    assert re.search(r"ARFA POS &middot; version \S+", html)
    assert "pos-js-error" in html  # the on-screen error banner is wired in
