"""Warehouse dispatch, stages 5-7: the warehouse computer invoices what was
picked (serials SOLD with the invoice, stock out of that warehouse; voiding
puts them back there), purchases are received by scanning, counts find
mismatches, and the reports answer "where did this unit go"."""

import io
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db as _db
from app.inventory import ledger
from app.models.invoice import Invoice
from app.models.inventory import ProductSerial, SerialStatus
from app.models.purchase import PurchaseBill, PurchaseOrder, PurchaseOrderLine
from app.models.warehouse import DispatchStatus, ReceiptStatus, StockCount, WarehouseReceipt
from tests.test_warehouse_api import API, auth, make_login, pickers  # noqa: F401
from tests.test_warehouse_dispatch import line, order, wh  # noqa: F401

D = Decimal


def _picked(client, w, doc, serials, who="ravi"):
    headers, _ = auth(client, w, who)
    client.post(f"{API}/orders/{doc.id}/claim", headers=headers)
    for serial in serials:
        res = client.post(f"{API}/orders/{doc.id}/lines/{doc.lines[0].id}/picks", json={"serial_no": serial},
                          headers=headers)
        assert res.get_json()["pick"]["ok"], res.get_json()
    assert client.post(f"{API}/orders/{doc.id}/confirm", headers=headers).status_code == 200
    return headers


@pytest.fixture()
def computer(wh, staff, db):
    """The branch login that is Warehouse A's computer."""
    wh.a.branch_user_id = staff.id
    db.session.commit()
    return staff


def _web(client, email, password):
    client.post("/auth/login", data={"email": email, "password": password})


# --- invoicing at the warehouse ---------------------------------------------------------------------

def test_the_warehouse_computer_invoices_the_scanned_serials(client, wh, pickers, computer):
    doc = order(wh)
    _picked(client, wh, doc, ["ACA2", "ACA4"])
    _web(client, computer.email, "CashierSecret123")
    assert client.get("/", follow_redirects=False).headers["Location"].endswith("/warehouse/")
    page = client.get("/warehouse/").get_data(as_text=True)
    assert doc.number in page and "Make invoice" in page and "Ravi" in page
    assert client.get("/warehouse/queue.json").get_json()["ready"][0]["number"] == doc.number
    res = client.post(f"/warehouse/orders/{doc.id}/invoice", data={"invoice_date": date.today().isoformat()},
                      follow_redirects=True)
    assert "made from" in res.get_data(as_text=True)
    invoice = Invoice.query.one()
    assert doc.dispatch_status == DispatchStatus.INVOICED and doc.converted_invoice_id == invoice.id
    units = ProductSerial.query.filter(ProductSerial.serial_no.in_(["ACA2", "ACA4"])).all()
    assert {u.status for u in units} == {SerialStatus.SOLD}
    assert {u.sold_invoice_id for u in units} == {invoice.id} and units[0].sold_invoice_line_id == invoice.lines[0].id
    assert ledger.stock_on_hand(wh.tenant.id, [wh.ac.id], wh.a.id)[wh.ac.id] == D("3")   # out of Warehouse A
    assert ledger.stock_on_hand(wh.tenant.id, [wh.ac.id], wh.b.id)[wh.ac.id] == D("2")
    slip = client.get(f"/warehouse/orders/{doc.id}/slip.pdf")
    assert slip.status_code == 200 and slip.mimetype == "application/pdf"
    assert client.get(f"/warehouse/orders/{doc.id}/invoice.pdf").status_code == 200


def test_voiding_the_invoice_puts_the_units_back_at_the_warehouse(client, wh, pickers, computer):
    from app.invoicing.services import void_invoice
    from app.sales import services as ss

    doc = order(wh)
    _picked(client, wh, doc, ["ACA1", "ACA3"])
    invoice = ss.convert_to_invoice(wh.tenant, wh.admin, doc)
    _db.session.commit()
    void_invoice(invoice, wh.admin, "wrong customer")
    _db.session.commit()
    for serial in ("ACA1", "ACA3"):
        unit = ProductSerial.query.filter_by(serial_no=serial).one()
        assert unit.status == SerialStatus.IN_STOCK and unit.location_id == wh.a.id and unit.sold_invoice_id is None
    assert ledger.stock_on_hand(wh.tenant.id, [wh.ac.id], wh.a.id)[wh.ac.id] == D("5")


def test_an_order_is_invoiced_only_once_picked_and_with_its_units_still_held(client, wh, pickers):
    from app.sales import services as ss

    doc = order(wh)
    with pytest.raises(ss.SalesError, match="still at the warehouse"):
        ss.convert_to_invoice(wh.tenant, wh.admin, doc)
    _picked(client, wh, doc, ["ACA1", "ACA2"])
    unit = ProductSerial.query.filter_by(serial_no="ACA2").one()
    unit.location_id = wh.b.id                                  # moved off since (a wrong-location serial)
    _db.session.commit()
    with pytest.raises(ss.SalesError, match="ACA2 is no longer held at Warehouse A"):
        ss.convert_to_invoice(wh.tenant, wh.admin, doc)
    assert Invoice.query.count() == 0


def test_a_computer_sees_only_its_own_warehouses_orders(client, wh, pickers, computer):
    at_b = order(wh, location=wh.b)
    make_login(wh, "Bala", "bala2", wh.b)
    _picked(client, wh, at_b, ["ACB1", "ACB2"], who="bala2")
    _web(client, computer.email, "CashierSecret123")
    assert at_b.number not in client.get("/warehouse/").get_data(as_text=True)
    assert client.get(f"/warehouse/orders/{at_b.id}").status_code == 404
    assert client.post(f"/warehouse/orders/{at_b.id}/invoice").status_code == 404
    assert client.get("/warehouse/logins").status_code == 403           # the owner's page
    client.get("/auth/logout")
    _web(client, wh.admin.email, "ClientSecret123")
    assert at_b.number in client.get("/warehouse/").get_data(as_text=True)    # the owner sees every warehouse


def test_a_branch_login_that_isnt_a_warehouse_computer_is_turned_away(client, wh, db):
    from app.models.user import User, UserRole

    other = User(tenant_id=wh.tenant.id, name="Branch 2", email="b2@acme.example.com", role=UserRole.STAFF,
                 must_change_password=False)
    other.set_password("Branch2Secret123")
    db.session.add(other)
    db.session.commit()
    _web(client, other.email, "Branch2Secret123")
    assert client.get("/warehouse/").status_code == 403


def test_the_invoices_eway_bill_leaves_from_the_warehouse(client, wh, pickers):
    from app.einvoice import eway
    from app.sales import services as ss

    doc = order(wh)
    _picked(client, wh, doc, ["ACA1", "ACA2"])
    invoice = ss.convert_to_invoice(wh.tenant, wh.admin, doc)
    _db.session.commit()
    draft = eway.draft_from(wh.tenant, invoice, wh.admin)
    assert "Plot 4, Thoothukudi" in draft.from_party["addr1"] and draft.from_party["pincode"] == "628001"


def test_the_order_page_shows_the_picks(client, wh, pickers):
    doc = order(wh)
    _picked(client, wh, doc, ["ACA1", "ACA2"])
    _web(client, wh.admin.email, "ClientSecret123")
    page = client.get(f"/sales/documents/{doc.id}").get_data(as_text=True)
    assert "Picked - at Warehouse A for invoice" in page and "ACA1, ACA2" in page
    assert "Edit &amp; invoice" not in page
    page = client.get(f"/warehouse/orders/{doc.id}").get_data(as_text=True)
    assert "Every scan" in page and "ACA1" in page


# --- receiving -------------------------------------------------------------------------------------------

@pytest.fixture()
def po(wh):
    from app.models.purchase import Supplier
    from app.utils.numbering import allocate_number, get_or_create_series
    from app.models.invoice_series import DocumentType
    from app.utils.financial_year import financial_year_for

    supplier = Supplier.query.filter_by(tenant_id=wh.tenant.id).first()
    series = get_or_create_series(wh.tenant.id, wh.tenant.gstins.first().id, DocumentType.PURCHASE_ORDER,
                                  financial_year_for(date.today()))
    order_ = PurchaseOrder(tenant_id=wh.tenant.id, gstin_id=wh.tenant.gstins.first().id, invoice_series_id=series.id,
                           number=allocate_number(series), supplier_id=supplier.id, order_date=date.today(),
                           created_by_id=wh.admin.id)
    order_.lines.append(PurchaseOrderLine(product_id=wh.ac.id, description=wh.ac.name, hsn_or_sac_code="8415",
                                          qty=D("3"), unit="PCS", rate=D("30000"), gst_rate=D("28")))
    _db.session.add(order_)
    _db.session.commit()
    return order_


def test_a_purchase_is_received_by_scanning_and_billed_with_the_serials(client, wh, pickers, po):
    _web(client, wh.admin.email, "ClientSecret123")
    assert "Receive at warehouse" in client.get(f"/purchases/orders/{po.id}").get_data(as_text=True)
    client.post("/warehouse/receipts/new", data={"purchase_order_id": po.id, "location_id": wh.a.id})
    receipt = WarehouseReceipt.query.one()
    headers, _ = auth(client, wh, "ravi")
    listed = client.get(f"{API}/receipts", headers=headers).get_json()["receipts"]
    assert listed[0]["order_number"] == po.number and listed[0]["units"] == 3
    line_id = po.lines[0].id
    url = f"{API}/receipts/{receipt.id}/lines/{line_id}/units"
    assert client.post(url, json={"serial_no": "NEW1", "client_unit_id": "u1"}, headers=headers).status_code == 201
    assert client.post(url, json={"serial_no": "NEW1", "client_unit_id": "u1"}, headers=headers).status_code == 201  # a retry
    assert client.post(url, json={"serial_no": "new1"}, headers=headers).status_code == 409         # twice in the batch
    assert "already in your records" in client.post(url, json={"serial_no": "ACA1"}, headers=headers).get_json()["error"]
    assert "model number" in client.post(url, json={"serial_no": "AR18TY3QBWK"}, headers=headers).get_json()["error"]
    client.post(url, json={"serial_no": "NEW2"}, headers=headers)
    assert client.get(f"{API}/receipts/{receipt.id}", headers=headers).get_json()["received"] == 2
    page = client.get(f"/purchases/new?receipt={receipt.id}").get_data(as_text=True)
    assert "NEW1\\nNEW2" in page or "NEW1\nNEW2" in page
    form = {"supplier_id": str(po.supplier_id), "gstin_id": str(po.gstin_id), "supplier_invoice_no": "LG-778",
            "supplier_invoice_date": date.today().isoformat(), "location_id": str(wh.a.id),
            "line_product_id[]": [str(wh.ac.id)], "line_description[]": [wh.ac.name], "line_hsn[]": ["8415"],
            "line_qty[]": ["2"], "line_unit[]": ["PCS"], "line_rate[]": ["30000"], "line_discount[]": [""],
            "line_discount_flat[]": [""], "line_gst_rate[]": ["28"], "line_itc[]": ["eligible"],
            "line_serials[]": ["NEW1\nNEW2"]}
    res = client.post(f"/purchases/new?receipt={receipt.id}", data=form, follow_redirects=True)
    assert "posted" in res.get_data(as_text=True), res.get_data(as_text=True)[:2000]
    assert receipt.status == ReceiptStatus.DONE and receipt.purchase_bill_id == PurchaseBill.query.order_by(PurchaseBill.id.desc()).first().id
    assert {u.location_id for u in ProductSerial.query.filter(ProductSerial.serial_no.in_(["NEW1", "NEW2"]))} == {wh.a.id}


# --- counting ---------------------------------------------------------------------------------------------

def test_a_count_lists_whats_missing_and_whats_extra(client, wh, pickers):
    headers, _ = auth(client, wh, "ravi")
    c = client.post(f"{API}/counts", json={}, headers=headers).get_json()
    for serial in ("ACA1", "ACA2", "ACA3", "ACA4", "FRA1", "ACB1", "MYSTERY9"):
        client.post(f"{API}/counts/{c['id']}/scans", json={"serial_no": serial}, headers=headers)
    dup = client.post(f"{API}/counts/{c['id']}/scans", json={"serial_no": "aca1"}, headers=headers).get_json()
    assert dup["duplicate"]
    done = client.post(f"{API}/counts/{c['id']}/finish", headers=headers).get_json()
    assert (done["in_stock"], done["matched"], done["missing"], done["extra"]) == (6, 5, 1, 2)
    _web(client, wh.admin.email, "ClientSecret123")
    page = client.get(f"/warehouse/counts/{c['id']}").get_data(as_text=True)
    assert "ACA5" in page and "MYSTERY9" in page and "Not in ARFA" in page and "at Warehouse B" in page
    xlsx = client.get(f"/warehouse/counts/{c['id']}?format=xlsx")
    assert xlsx.status_code == 200 and xlsx.data[:2] == b"PK"


# --- reports ------------------------------------------------------------------------------------------------

def test_the_serial_history_and_the_other_reports(client, wh, pickers):
    from app.sales import services as ss

    doc = order(wh)
    _picked(client, wh, doc, ["ACA1", "ACA2"])
    invoice = ss.convert_to_invoice(wh.tenant, wh.admin, doc)
    _db.session.commit()
    _web(client, wh.admin.email, "ClientSecret123")
    page = client.get("/warehouse/reports?serial=aca1").get_data(as_text=True)
    assert "Bought" in page and "Picked" in page and "Ravi" in page and invoice.invoice_number in page
    assert "Ravi Stores" in page
    assert client.get("/warehouse/reports/picking").status_code == 200
    page = client.get("/warehouse/reports/devices").get_data(as_text=True)
    assert "Redmi 12" in page and "Ravi" in page
    assert client.get("/warehouse/receipts").status_code == 200
