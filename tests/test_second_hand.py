"""Second-hand goods (margin scheme) as its own module: buy on the
dealer's own voucher (from a person with ID proof, or from a dealer with
their invoice), used stock unit by unit, sell with the IMEI and a live
preview, e-way bills out and in, the non-margin part reported as exempt,
the menu, the module flag and tenant isolation.

The GST rule checked throughout (Rule 32(5)): GST on the margin only -
selling price less purchase price - at the item's rate, inside the price;
a loss has no GST; no input tax credit on the purchase."""

from datetime import date
from decimal import Decimal

import pytest

from app.einvoice import eway
from app.models.customer import Customer
from app.models.einvoice import EWayBill
from app.models.inventory import ProductSerial, SerialStatus
from app.models.invoice import Invoice
from app.models.invoice_series import DocumentType
from app.models.product import Product
from app.models.purchase import PurchaseBill, Supplier
from app.models.tenant import BillingCycle, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.reports.gstr import gstr1_data, gstr3b_data

D = Decimal
PERIOD = date.today().strftime("%Y-%m")


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


@pytest.fixture()
def shop(db, tenant, client_admin):
    for key in ("margin_scheme", "serial_imei", "eway_bill"):
        tenant.set_feature(key, True)
    phone = Product(tenant_id=tenant.id, name="Used iPhone 12", hsn_or_sac_code="85171300", gst_rate=D("18"),
                    unit="PCS", sale_price=D("0"), price_includes_tax=True, is_margin_scheme=True, track_serial=True)
    db.session.add(phone)
    db.session.commit()
    return {"phone": phone}


def _buy_form(**extra):
    data = {"seller_kind": "person", "seller_name": "Ravi Kumar", "seller_phone": "9876543210",
            "seller_address": "12 MG Road, Pune 411001", "seller_state": "27", "id_type": "aadhaar",
            "id_no": "1234 5678 9012", "paid_by": "cash", "bill_date": date.today().isoformat(),
            "line_product_id[]": [""], "line_new_name[]": [""], "line_new_hsn[]": [""], "line_new_rate[]": ["18"],
            "line_qty[]": ["1"], "line_price[]": ["10000"], "line_seller_gst[]": ["0"], "line_serials[]": [""]}
    data.update(extra)
    return data


def _bought(client, shop, serial="351234567890123", price="10000", **extra):
    res = client.post("/second-hand/buy/new", data=_buy_form(
        **{"line_product_id[]": [str(shop["phone"].id)], "line_price[]": [price], "line_serials[]": [serial], **extra}))
    assert res.status_code == 302, res.get_data(as_text=True)[:500]
    return PurchaseBill.query.filter_by(kind="margin_voucher").order_by(PurchaseBill.id.desc()).first()


# --- buying -------------------------------------------------------------------------------------------

def test_buying_from_a_person_makes_a_voucher(client, db, tenant, client_admin, shop):
    _login(client, client_admin.email, "ClientSecret123")
    bill = _bought(client, shop)
    assert bill.number.startswith("SHP/") and bill.supplier_invoice_no == bill.number
    assert bill.seller_details["id_type"] == "aadhaar" and not bill.seller_details["registered"]
    assert bill.itc_cgst + bill.itc_sgst + bill.itc_igst == 0 and bill.total_cgst + bill.total_sgst + bill.total_igst == 0
    assert bill.grand_total == D("10000")
    line = bill.lines[0]
    assert line.is_margin_scheme and line.margin_unit_price == D("10000")
    unit = ProductSerial.query.filter_by(serial_no="351234567890123").one()
    assert unit.status == SerialStatus.IN_STOCK and unit.purchase_bill_id == bill.id
    seller = Supplier.query.filter_by(name="Ravi Kumar").one()
    assert seller.gstin is None and seller.state_code == "27"
    page = client.get(f"/second-hand/buy/{bill.id}").get_data(as_text=True)
    assert bill.number in page and "No input tax credit" in page
    pdf = client.get(f"/second-hand/buy/{bill.id}/voucher.pdf")
    assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"


def test_the_sellers_id_proof_is_asked_for(client, db, tenant, client_admin, shop):
    _login(client, client_admin.email, "ClientSecret123")
    client.post("/second-hand/buy/new", data=_buy_form(**{"id_type": "", "id_no": "",
                                                         "line_product_id[]": [str(shop["phone"].id)],
                                                         "line_serials[]": ["111111111111111"]}))
    assert PurchaseBill.query.count() == 0
    client.post("/second-hand/settings", data={"remainder_as": "exempt"})          # ID proof no longer asked
    client.post("/second-hand/buy/new", data=_buy_form(**{"id_type": "", "id_no": "",
                                                         "line_product_id[]": [str(shop["phone"].id)],
                                                         "line_serials[]": ["111111111111111"]}))
    assert PurchaseBill.query.count() == 1


def test_buying_from_a_dealer_puts_their_gst_into_the_cost(client, db, tenant, client_admin, shop):
    _login(client, client_admin.email, "ClientSecret123")
    bill = _bought(client, shop, serial="352222222222222", **{
        "seller_kind": "dealer", "seller_name": "Mobile Mart", "seller_gstin": "27BBBBB1111B1Z5",
        "invoice_no": "MM/77", "invoice_date": date.today().isoformat(), "line_seller_gst[]": ["18"]})
    assert bill.supplier_invoice_no == "MM/77" and bill.seller_details["registered"]
    assert bill.lines[0].margin_unit_price == D("11800.00")          # 10,000 + 18% - no credit taken
    assert bill.itc_cgst + bill.itc_sgst + bill.itc_igst == 0


def test_a_new_used_item_is_made_from_the_voucher(client, db, tenant, client_admin, shop):
    _login(client, client_admin.email, "ClientSecret123")
    client.post("/second-hand/buy/new", data=_buy_form(**{
        "line_new_name[]": ["Samsung S21 (used)"], "line_new_hsn[]": ["85171300"], "line_new_rate[]": ["18"],
        "line_serials[]": ["353333333333333"], "line_price[]": ["9000"]}))
    item = Product.query.filter_by(name="Samsung S21 (used)").one()
    assert item.is_margin_scheme and item.track_serial and item.price_includes_tax and item.gst_rate == D("18")


def test_regular_goods_cant_go_on_a_second_hand_voucher(client, db, tenant, client_admin, shop):
    charger = Product(tenant_id=tenant.id, name="New charger", hsn_or_sac_code="85044030", gst_rate=D("18"),
                      unit="PCS", sale_price=D("590"))
    db.session.add(charger)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    res = client.post("/second-hand/buy/new", data=_buy_form(**{"line_product_id[]": [str(charger.id)]}),
                      follow_redirects=True)
    assert "isn&#39;t a second-hand" in res.get_data(as_text=True) and PurchaseBill.query.count() == 0


# --- used stock and selling ------------------------------------------------------------------------------

def test_used_stock_lists_each_unit_with_its_cost(client, db, tenant, client_admin, shop):
    _login(client, client_admin.email, "ClientSecret123")
    _bought(client, shop)
    page = client.get("/second-hand/stock").get_data(as_text=True)
    assert "351234567890123" in page and "10000.00" in page and "Ravi Kumar" in page


def _sell(client, shop, serial="351234567890123", price="11800", **extra):
    key = f"s-{shop['phone'].id}-{serial}"
    return client.post("/second-hand/sell/new", data={
        "pick": [key], f"price_{key}": price, "buyer_kind": "new", "buyer_name": "Anita", "buyer_phone": "9000000000",
        "buyer_state": "27", "invoice_date": date.today().isoformat(), **extra})


def test_selling_taxes_only_the_margin(client, db, tenant, client_admin, shop):
    _login(client, client_admin.email, "ClientSecret123")
    _bought(client, shop)
    preview = client.post("/second-hand/sell/preview", data={
        "pick": [f"s-{shop['phone'].id}-351234567890123"], f"price_s-{shop['phone'].id}-351234567890123": "11800",
        "buyer_kind": "new", "buyer_state": "27"}).get_data(as_text=True)
    assert "1800.00" in preview and "1525.42" in preview and "274.58" in preview and "10000.00" in preview
    assert Invoice.query.count() == 0                                      # the preview saves nothing
    res = _sell(client, shop)
    assert res.status_code == 302
    invoice = Invoice.query.one()
    assert invoice.document_type == DocumentType.MARGIN_SCHEME_INVOICE and invoice.invoice_number.startswith("MSI/")
    assert invoice.grand_total == D("11800")
    assert invoice.total_taxable_value == D("1525.42")
    assert invoice.total_cgst + invoice.total_sgst == D("274.58") and invoice.total_igst == 0
    line = invoice.lines[0]
    assert "351234567890123" in line.description and line.margin_value == D("1800.00")
    assert ProductSerial.query.filter_by(serial_no="351234567890123").one().status == SerialStatus.SOLD
    assert "351234567890123" not in client.get("/second-hand/stock").get_data(as_text=True)


def test_a_loss_has_no_gst(client, db, tenant, client_admin, shop):
    _login(client, client_admin.email, "ClientSecret123")
    _bought(client, shop)
    _sell(client, shop, price="9500")
    invoice = Invoice.query.one()
    assert invoice.grand_total == D("9500") and invoice.total_taxable_value == 0
    assert invoice.total_cgst + invoice.total_sgst + invoice.total_igst == 0


def test_inter_state_buyer_pays_igst_on_the_margin(client, db, tenant, client_admin, shop):
    _login(client, client_admin.email, "ClientSecret123")
    _bought(client, shop)
    _sell(client, shop, buyer_state="29")
    invoice = Invoice.query.one()
    assert invoice.total_igst == D("274.58") and invoice.total_cgst == 0


def test_the_old_invoice_form_points_imei_goods_to_second_hand(db, tenant, client_admin, shop):
    from app.invoicing.services import InvoiceInput, InvoiceValidationError, LineInput, create_invoice

    buyer = Customer(tenant_id=tenant.id, name="B", state_code="27", state_name="Maharashtra")
    db.session.add(buyer)
    db.session.commit()
    with pytest.raises(InvoiceValidationError, match="Second-hand goods -> Sell"):
        create_invoice(tenant, client_admin, InvoiceInput(
            gstin_id=tenant.gstins.first().id, document_type=DocumentType.TAX_INVOICE, place_of_supply_state_code="27",
            invoice_date=date.today(), notes=None, customer_id=buyer.id,
            lines=[LineInput(description="x", hsn_or_sac_code="85171300", qty=D("1"), rate=D("11800"),
                             discount_percent=D("0"), gst_rate=D("18"), product_id=shop["phone"].id,
                             price_includes_tax=True)]))


def test_voiding_follows_what_was_sold(client, db, tenant, client_admin, shop):
    from app.invoicing.services import void_invoice

    _login(client, client_admin.email, "ClientSecret123")
    bill = _bought(client, shop)
    _sell(client, shop)
    client.post(f"/second-hand/buy/{bill.id}/void", data={"reason": "mistake"})
    db.session.expire_all()
    assert bill.status.value == "posted"                                  # its unit was sold
    void_invoice(Invoice.query.one(), client_admin, "buyer returned it")
    db.session.commit()
    assert ProductSerial.query.filter_by(serial_no="351234567890123").one().status == SerialStatus.IN_STOCK
    client.post(f"/second-hand/buy/{bill.id}/void", data={"reason": "mistake"})
    db.session.expire_all()
    assert bill.status.value == "voided"


# --- e-way bills -------------------------------------------------------------------------------------------------

def test_e_way_bill_for_a_sale_adds_up_to_the_invoice(client, db, tenant, client_admin, shop):
    _login(client, client_admin.email, "ClientSecret123")
    _bought(client, shop, price="60000")
    _sell(client, shop, price="70800")
    invoice = Invoice.query.one()
    ewb = eway.draft_from(tenant, invoice, client_admin)
    assert ewb.total_value == D("9152.54") and ewb.cgst_value + ewb.sgst_value == D("1647.46")
    assert ewb.other_value == D("60000.00") and ewb.total_invoice_value == D("70800")
    assert ewb.total_value + ewb.cgst_value + ewb.sgst_value + ewb.igst_value + ewb.other_value == ewb.total_invoice_value
    payload = eway.api_payload(ewb)
    assert payload["otherValue"] == 60000.0 and payload["totInvValue"] == 70800.0
    assert eway.bulk_entry(ewb)["OthValue"] == 60000.0
    page = client.get("/second-hand/eway").get_data(as_text=True)
    assert invoice.invoice_number in page and "Make e-way bill" in page


def test_inward_e_way_bill_for_goods_bought_from_a_person(client, db, tenant, client_admin, shop):
    _login(client, client_admin.email, "ClientSecret123")
    bill = _bought(client, shop, price="60000")
    assert eway.eligible(bill) and eway.offer(tenant, bill)
    ewb = eway.draft_from(tenant, bill, client_admin)
    assert ewb.supply_type == "I" and ewb.doc_type == "BIL" and ewb.document_number == bill.number
    assert ewb.from_party["gstin"] == "URP" and ewb.from_party["pincode"] == "411001"
    assert ewb.to_party["gstin"] == tenant.gstins.first().gstin
    assert ewb.total_invoice_value == D("60000") and ewb.cgst_value + ewb.sgst_value + ewb.igst_value == 0
    res = client.get(f"/einvoice/eway-bills/new/purchase_bill/{bill.id}")
    assert res.status_code == 200 and "the seller" in res.get_data(as_text=True)


# --- returns -------------------------------------------------------------------------------------------------

def test_the_non_margin_part_is_reported_as_exempt(client, db, tenant, client_admin, shop):
    _login(client, client_admin.email, "ClientSecret123")
    _bought(client, shop)
    _sell(client, shop)
    gstin = tenant.gstins.first()
    three_b = gstr3b_data(tenant, gstin, PERIOD)
    assert three_b["outward_taxable"]["taxable_value"] == D("1525.42")
    assert three_b["outward_nil_rated"]["taxable_value"] == D("10000.00")
    one = gstr1_data(tenant, gstin, PERIOD)
    assert one["nil_exempt"]["rows"]["intra_unregistered"]["exempted"] == D("10000.00")
    assert one["nil_exempt"]["total"]["exempted"] == D("10000.00")
    page = client.get(f"/reports/gstr1?period={PERIOD}").get_data(as_text=True)
    assert "table 8" in page
    # The client's choice: leave it out of tables 8 and 3.1(c).
    client.post("/second-hand/settings", data={"remainder_as": "none", "id_proof_required": "y"})
    db.session.expire_all()
    assert gstr3b_data(tenant, gstin, PERIOD)["outward_nil_rated"]["taxable_value"] == 0
    assert "nil_exempt" not in gstr1_data(tenant, gstin, PERIOD)


# --- the module's place in the app ---------------------------------------------------------------------------

def test_overview_menu_and_order(client, db, tenant, client_admin, shop):
    _login(client, client_admin.email, "ClientSecret123")
    _bought(client, shop)
    page = client.get("/second-hand/").get_data(as_text=True)
    assert "1 · Buy" in page and "2 · Used stock" in page and "3 · Sell" in page and "4 · e-Way bills" in page
    assert "10,000" in page                                                 # stock bought for
    home = client.get("/reports/").get_data(as_text=True)
    assert 'href="/second-hand/"' in home and "Second-hand goods" in home


def test_with_the_module_off_the_area_is_gone(client, db, tenant, client_admin, shop):
    tenant.set_feature("margin_scheme", False)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    for url in ("/second-hand/", "/second-hand/buy/new", "/second-hand/stock", "/second-hand/sell/new",
                "/second-hand/eway", "/second-hand/settings"):
        assert client.get(url).status_code == 404
    assert 'href="/second-hand/"' not in client.get("/reports/").get_data(as_text=True)


def test_a_voucher_stays_with_its_client(client, db, tenant, client_admin, shop, super_admin):
    _login(client, client_admin.email, "ClientSecret123")
    bill = _bought(client, shop)
    client.get("/auth/logout")
    other = Tenant(legal_name="Other Mobiles", registration_type=RegistrationType.REGULAR, billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.flush()
    db.session.add(Gstin(tenant_id=other.id, gstin="29CCCCC2222C1Z5", state_code="29", state_name="Karnataka",
                         is_primary=True))
    owner = User(tenant_id=other.id, name="Other", email="o@other.example.com", role=UserRole.CLIENT_ADMIN,
                 must_change_password=False)
    owner.set_password("OtherSecret123")
    db.session.add(owner)
    other.set_feature("margin_scheme", True)
    db.session.commit()
    _login(client, "o@other.example.com", "OtherSecret123")
    assert client.get(f"/second-hand/buy/{bill.id}").status_code == 404
    assert client.get(f"/einvoice/eway-bills/new/purchase_bill/{bill.id}").status_code == 404
    assert "351234567890123" not in client.get("/second-hand/stock").get_data(as_text=True)
