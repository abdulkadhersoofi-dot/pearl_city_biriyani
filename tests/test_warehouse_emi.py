"""Warehouse dispatch, stage 5B - EMI (finance) sales: the order rings first,
the warehouse locks the real unit (EMI HOLD) while the loan is processed,
the showroom invoices with that serial, the receivable splits between the
customer and the financier, the delivery check blocks any other unit, a swap
needs approval and is recorded on the slip (the invoice stays), and racks
make a showroom-chosen serial findable."""

import io
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db as _db
from app.inventory import ledger
from app.models.inventory import ProductSerial, SerialStatus
from app.models.invoice import Invoice
from app.models.sales import SalesDocument, SalesDocumentKind
from app.models.warehouse import (
    EMI_HOLD, EMI_READY, DispatchStatus, SerialSwap, SwapStatus, WarehouseNotice,
)
from app.sales import services as ss
from app.warehouse import dispatch
from app.warehouse.emi import add_financier
from tests.test_warehouse_api import API, auth, make_login, pickers  # noqa: F401
from tests.test_warehouse_dispatch import line, order, wh  # noqa: F401

D = Decimal
TODAY = date.today()


@pytest.fixture()
def bajaj(wh):
    f = add_financier(wh.tenant, "Bajaj Finserv", "BFL")
    _db.session.commit()
    return f


def emi_order(w, financier, finance="40000", qty="1", user=None, loan="LAN-7781", commit=True):
    from app.invoicing.services import LineInput  # noqa: F401

    doc = ss.create_sales_document(w.tenant, user or w.admin, ss.SalesDocInput(
        kind=SalesDocumentKind.SALES_ORDER, gstin_id=w.tenant.gstins.first().id, customer_id=w.buyer.id,
        place_of_supply_state_code="27", doc_date=TODAY, invoice_document_type=ss.DocumentType.TAX_INVOICE,
        lines=[line(w.ac, qty)], dispatch_location_id=w.a.id, is_finance_sale=True, financier_id=financier.id,
        loan_ref=loan, finance_amount=D(finance)))
    if commit:
        _db.session.commit()
    return doc


def lock(client, w, doc, serials, who="ravi"):
    headers, _ = auth(client, w, who)
    client.post(f"{API}/orders/{doc.id}/claim", headers=headers)
    for s in serials:
        assert client.post(f"{API}/orders/{doc.id}/lines/{doc.lines[0].id}/picks", json={"serial_no": s},
                           headers=headers).get_json()["pick"]["ok"]
    res = client.post(f"{API}/orders/{doc.id}/confirm", headers=headers)
    assert res.status_code == 200
    return headers, res.get_json()


def _web(client, email, password):
    client.post("/auth/login", data={"email": email, "password": password})


# --- the finance order ---------------------------------------------------------------------------------

def test_an_emi_order_rings_first_and_sits_above_the_others(client, wh, pickers, bajaj):
    normal = order(wh)
    emi = emi_order(wh, bajaj)
    assert emi.priority == "high" and emi.is_finance_sale and emi.down_payment == emi.grand_total - D("40000")
    notice = WarehouseNotice.query.filter_by(sales_document_id=emi.id).one()
    assert notice.kind == "new_emi_order" and not notice.silent and "customer waiting" in notice.title
    from app.warehouse.notify import _message

    assert _message(notice, "tok")["message"]["data"]["priority"] == "high"
    headers, _ = auth(client, wh, "ravi")
    cards = client.get(f"{API}/pipeline", headers=headers).get_json()["orders"]
    assert [c["id"] for c in cards] == [emi.id, normal.id]          # pinned on top though newer
    assert cards[0]["emi"] and cards[0]["financier"] == "Bajaj Finserv"


def test_an_emi_order_needs_a_financier_and_a_sensible_split(wh, bajaj):
    with pytest.raises(ss.SalesError, match="Pick the financier"):
        emi_order(wh, type("X", (), {"id": None})(), commit=False)
    _db.session.rollback()
    with pytest.raises(ss.SalesError, match="at most the order total"):
        emi_order(wh, bajaj, finance="999999", commit=False)
    _db.session.rollback()
    with pytest.raises(ss.SalesError, match="should make the order total"):
        ss.create_sales_document(wh.tenant, wh.admin, ss.SalesDocInput(
            kind=SalesDocumentKind.SALES_ORDER, gstin_id=wh.tenant.gstins.first().id, customer_id=wh.buyer.id,
            place_of_supply_state_code="27", doc_date=TODAY, invoice_document_type=ss.DocumentType.TAX_INVOICE,
            lines=[line(wh.ac)], dispatch_location_id=wh.a.id, is_finance_sale=True, financier_id=bajaj.id,
            finance_amount=D("40000"), down_payment=D("10")))
    _db.session.rollback()
    assert SalesDocument.query.count() == 0


def test_lock_for_emi_puts_the_unit_in_emi_hold_and_the_showroom_sees_it(client, wh, pickers, bajaj, staff):
    doc = emi_order(wh, bajaj, user=staff)
    _, body = lock(client, wh, doc, ["ACA3"])
    assert "Locked for EMI" in body["message"]
    unit = ProductSerial.query.filter_by(serial_no="ACA3").one()
    assert unit.status == SerialStatus.PICKED and unit.bin_code == EMI_HOLD
    assert doc.dispatch_status == DispatchStatus.PICKED
    _web(client, staff.email, "CashierSecret123")
    page = client.get(f"/sales/documents/{doc.id}").get_data(as_text=True)
    assert "Locked for EMI at Warehouse A" in page and "SN ACA3" in page and "Make invoice" in page
    stamp = client.get(f"/sales/documents/{doc.id}/dispatch.json").get_json()["stamp"]
    assert "picked" in stamp
    # The warehouse computer doesn't invoice it - the showroom does.
    client.get("/auth/logout")
    _web(client, wh.admin.email, "ClientSecret123")
    assert doc.number not in client.get("/warehouse/").get_data(as_text=True).split("At the warehouse")[0]


def test_the_showroom_invoices_only_once_every_line_is_locked(client, wh, pickers, bajaj, staff):
    doc = emi_order(wh, bajaj, qty="2", finance="80000", user=staff)
    headers, _ = auth(client, wh, "ravi")
    client.post(f"{API}/orders/{doc.id}/claim", headers=headers)
    client.post(f"{API}/orders/{doc.id}/lines/{doc.lines[0].id}/picks", json={"serial_no": "ACA1"}, headers=headers)
    _web(client, staff.email, "CashierSecret123")
    page = client.post(f"/sales/documents/{doc.id}/emi-invoice", follow_redirects=True).get_data(as_text=True)
    assert "hasn&#39;t locked every unit" in page or "hasn't locked every unit" in page
    assert Invoice.query.count() == 0
    client.post(f"{API}/orders/{doc.id}/lines/{doc.lines[0].id}/picks", json={"serial_no": "ACA2"}, headers=headers)
    client.post(f"{API}/orders/{doc.id}/confirm", headers=headers)
    page = client.post(f"/sales/documents/{doc.id}/emi-invoice", follow_redirects=True).get_data(as_text=True)
    assert "made with the locked serials" in page
    invoice = Invoice.query.one()
    assert invoice.financier_id == bajaj.id and invoice.finance_amount == D("80000") and invoice.loan_ref == "LAN-7781"
    assert {u.serial_no for u in ProductSerial.query.filter_by(sold_invoice_id=invoice.id)} == {"ACA1", "ACA2"}
    assert ledger.stock_on_hand(wh.tenant.id, [wh.ac.id], wh.a.id)[wh.ac.id] == D("3")   # out of the warehouse
    assert doc.invoiced_at_showroom and doc.dispatch_status == DispatchStatus.PICKED and dispatch.to_deliver(doc)
    # A locked serial can't be changed now: no undo, no release.
    pick = doc.lines[0].document and dispatch.good_picks(doc)[0]
    with pytest.raises(dispatch.DispatchError):
        dispatch.undo(pick)
    with pytest.raises(dispatch.DispatchError):
        dispatch.release(doc, "x")
    pdf = client.get(f"/sales/documents/{doc.id}/invoice.pdf")
    assert pdf.status_code == 200 and pdf.mimetype == "application/pdf"


def test_the_receivable_splits_between_customer_and_financier(client, wh, pickers, bajaj):
    from app.accounts import services as accounts
    from app.accounts.receivables import customer_account
    from app.models.accounts import MoneyMode

    doc = emi_order(wh, bajaj)
    lock(client, wh, doc, ["ACA1"])
    invoice = ss.convert_to_invoice(wh.tenant, wh.admin, doc)
    _db.session.commit()
    customer = customer_account(wh.tenant, wh.buyer, with_entries=False)
    financier = customer_account(wh.tenant, bajaj.customer, with_entries=False)
    assert customer.balance == invoice.grand_total - D("40000") == doc.down_payment
    assert financier.balance == D("40000")
    # The financier's payout is an ordinary receipt on its account, set against the same invoice.
    accounts.create_receipt(wh.tenant, wh.admin, accounts.VoucherInput(
        party_id=bajaj.customer_id, voucher_date=TODAY, amount=D("40000"), mode=MoneyMode.BANK,
        allocations=[accounts.Allocation("invoice", invoice.id, D("40000"))]))
    _db.session.commit()
    assert customer_account(wh.tenant, bajaj.customer, with_entries=False).balance == 0
    assert customer_account(wh.tenant, wh.buyer, with_entries=False).balance == doc.down_payment


# --- the delivery check and swaps ------------------------------------------------------------------------

@pytest.fixture()
def invoiced(client, wh, pickers, bajaj):
    doc = emi_order(wh, bajaj)
    lock(client, wh, doc, ["ACA1"])
    ss.convert_to_invoice(wh.tenant, wh.admin, doc)
    _db.session.commit()
    return doc


def test_a_different_serial_at_dispatch_is_blocked(client, wh, invoiced):
    headers, _ = auth(client, wh, "siva")
    listed = client.get(f"{API}/pipeline?tab=deliver", headers=headers).get_json()["orders"]
    assert [c["id"] for c in listed] == [invoiced.id] and listed[0]["to_deliver"]
    detail = client.get(f"{API}/orders/{invoiced.id}", headers=headers).get_json()
    assert detail["deliver"] == [{"serial_no": "ACA1", "line_id": invoiced.lines[0].id, "rack": EMI_HOLD,
                                  "product": "LG AC 1.5T", "checked": False}]
    url = f"{API}/orders/{invoiced.id}"
    wrong = client.post(f"{url}/deliver-scans", json={"serial_no": "ACA2"}, headers=headers).get_json()
    assert not wrong["ok"] and "BLOCKED" in wrong["message"]
    assert client.post(f"{url}/deliver", headers=headers).status_code == 409
    assert client.post(f"{url}/deliver-scans", json={"serial_no": "aca1"}, headers=headers).get_json()["ok"]
    res = client.post(f"{url}/deliver", headers=headers)
    assert res.status_code == 200 and invoiced.dispatch_status == DispatchStatus.DELIVERED


def test_a_swap_needs_approval_and_is_recorded_on_the_slip(client, wh, invoiced, staff):
    headers, _ = auth(client, wh, "siva")
    url = f"{API}/orders/{invoiced.id}"
    assert client.post(f"{url}/swaps", json={"old_serial": "ACA1", "new_serial": "ACA2", "reason": ""},
                       headers=headers).status_code == 400
    res = client.post(f"{url}/swaps", json={"old_serial": "ACA1", "new_serial": "ACA2", "reason": "Box crushed"},
                      headers=headers)
    assert res.status_code == 201
    swap = SerialSwap.query.one()
    assert ProductSerial.query.filter_by(serial_no="ACA2").one().status == SerialStatus.PICKED   # held
    client.post(f"{url}/deliver-scans", json={"serial_no": "ACA1"}, headers=headers)
    assert "approval" in client.post(f"{url}/deliver", headers=headers).get_json()["error"]
    # Not the owner's or the showroom's? Then no.
    _web(client, staff.email, "CashierSecret123")
    assert client.post(f"/warehouse/swaps/{swap.id}", data={"decision": "approve"}).status_code == 403
    client.get("/auth/logout")
    _web(client, wh.admin.email, "ClientSecret123")
    assert "Swaps waiting for approval" in client.get("/warehouse/").get_data(as_text=True)
    client.post(f"/warehouse/swaps/{swap.id}", data={"decision": "approve", "note": "ok"})
    assert swap.status == SwapStatus.APPROVED
    invoice = Invoice.query.one()
    old, new = (ProductSerial.query.filter_by(serial_no=s).one() for s in ("ACA1", "ACA2"))
    assert old.status == SerialStatus.IN_STOCK and old.sold_invoice_id is None and old.location_id == wh.a.id
    assert new.status == SerialStatus.SOLD and new.sold_invoice_id == invoice.id
    assert invoice.status.value == "issued" and invoice.lines[0].qty == 1             # the invoice stays
    assert ledger.stock_on_hand(wh.tenant.id, [wh.ac.id], wh.a.id)[wh.ac.id] == D("4")
    slip = client.get(f"/warehouse/orders/{invoiced.id}/slip.pdf")
    assert slip.status_code == 200
    # Now the replacement is what must leave.
    assert not client.post(f"{url}/deliver-scans", json={"serial_no": "ACA1"}, headers=headers).get_json()["ok"]
    assert client.post(f"{url}/deliver-scans", json={"serial_no": "ACA2"}, headers=headers).get_json()["ok"]
    assert client.post(f"{url}/deliver", headers=headers).status_code == 200


def test_voiding_after_a_swap_puts_everything_back(client, wh, invoiced):
    from app.invoicing.services import void_invoice

    headers, _ = auth(client, wh, "siva")
    client.post(f"{API}/orders/{invoiced.id}/swaps", json={"old_serial": "ACA1", "new_serial": "ACA2",
                                                          "reason": "dent"}, headers=headers)
    dispatch.decide_swap(SerialSwap.query.one(), wh.admin, True)
    _db.session.commit()
    void_invoice(Invoice.query.one(), wh.admin, "loan rejected")
    _db.session.commit()
    assert {u.status for u in ProductSerial.query.filter(ProductSerial.serial_no.in_(["ACA1", "ACA2"]))} == {
        SerialStatus.IN_STOCK}
    assert ledger.stock_on_hand(wh.tenant.id, [wh.ac.id], wh.a.id)[wh.ac.id] == D("5")


def test_a_rejected_swap_frees_the_replacement(client, wh, invoiced):
    headers, _ = auth(client, wh, "siva")
    client.post(f"{API}/orders/{invoiced.id}/swaps", json={"old_serial": "ACA1", "new_serial": "ACA2",
                                                          "reason": "dent"}, headers=headers)
    dispatch.decide_swap(SerialSwap.query.one(), wh.admin, False)
    _db.session.commit()
    assert ProductSerial.query.filter_by(serial_no="ACA2").one().status == SerialStatus.IN_STOCK
    assert ProductSerial.query.filter_by(serial_no="ACA1").one().status == SerialStatus.SOLD


def test_an_ordinary_order_is_delivery_checked_too(client, wh, pickers):
    from tests.test_warehouse_flow import _picked

    doc = order(wh)
    _picked(client, wh, doc, ["ACA1", "ACA2"])
    ss.convert_to_invoice(wh.tenant, wh.admin, doc)
    _db.session.commit()
    headers, _ = auth(client, wh, "ravi")
    assert [c["id"] for c in client.get(f"{API}/pipeline?tab=deliver", headers=headers).get_json()["orders"]] == [doc.id]
    for s in ("ACA1", "ACA2"):
        client.post(f"{API}/orders/{doc.id}/deliver-scans", json={"serial_no": s}, headers=headers)
    assert client.post(f"{API}/orders/{doc.id}/deliver", headers=headers).status_code == 200


# --- racks, EMI READY and the showroom's own pick ----------------------------------------------------------

def test_putaway_sets_the_rack_and_move_changes_it(client, wh, pickers):
    from tests.test_warehouse_flow import po  # noqa: F401 - fixture function, called below
    from app.models.purchase import PurchaseOrder

    headers, _ = auth(client, wh, "ravi")
    res = client.post(f"{API}/bins/move", json={"serial_no": "ACA1", "rack": "RACK:C3"}, headers=headers)
    assert res.get_json()["rack"] == "C3" and ProductSerial.query.filter_by(serial_no="ACA1").one().bin_code == "C3"
    client.post(f"{API}/bins/move", json={"serial_no": "ACA1", "rack": "RACK:EMI-READY"}, headers=headers)
    assert ProductSerial.query.filter_by(serial_no="ACA1").one().bin_code == EMI_READY
    assert client.post(f"{API}/bins/move", json={"serial_no": "ACB1", "rack": "C3"}, headers=headers).status_code == 409
    assert client.post(f"{API}/bins/move", json={"serial_no": "ACA2", "rack": "EMI-HOLD"},
                       headers=headers).status_code == 400
    assert PurchaseOrder.query.count() == 0


def test_putaway_while_receiving(client, wh, pickers):
    from app.models.purchase import PurchaseOrder, PurchaseOrderLine, Supplier
    from app.models.invoice_series import DocumentType
    from app.utils.financial_year import financial_year_for
    from app.utils.numbering import allocate_number, get_or_create_series
    from app.models.warehouse import ReceivedUnit, WarehouseReceipt
    from app.warehouse.receiving import mark_billed

    supplier = Supplier.query.filter_by(tenant_id=wh.tenant.id).first()
    series = get_or_create_series(wh.tenant.id, wh.tenant.gstins.first().id, DocumentType.PURCHASE_ORDER,
                                  financial_year_for(TODAY))
    po = PurchaseOrder(tenant_id=wh.tenant.id, gstin_id=wh.tenant.gstins.first().id, invoice_series_id=series.id,
                       number=allocate_number(series), supplier_id=supplier.id, order_date=TODAY,
                       created_by_id=wh.admin.id)
    po.lines.append(PurchaseOrderLine(product_id=wh.ac.id, description=wh.ac.name, qty=D("1"), unit="PCS",
                                      rate=D("30000"), gst_rate=D("28")))
    _db.session.add(po)
    _db.session.commit()
    _web(client, wh.admin.email, "ClientSecret123")
    client.post("/warehouse/receipts/new", data={"purchase_order_id": po.id, "location_id": wh.a.id})
    receipt = WarehouseReceipt.query.one()
    headers, _ = auth(client, wh, "ravi")
    client.post(f"{API}/receipts/{receipt.id}/lines/{po.lines[0].id}/units",
                json={"serial_no": "NEWX1", "rack": "RACK:D2"}, headers=headers)
    assert ReceivedUnit.query.one().bin_code == "D2"
    from tests.test_warehouse_dispatch import buy

    buy(wh.tenant, wh.admin, wh.a, wh.ac, "NEWX1")
    from app.models.purchase import PurchaseBill

    mark_billed(receipt, PurchaseBill.query.order_by(PurchaseBill.id.desc()).first())
    _db.session.commit()
    assert ProductSerial.query.filter_by(serial_no="NEWX1").one().bin_code == "D2"


def test_the_showroom_pick_lists_emi_ready_then_racked_and_hides_the_rest(client, wh, pickers, bajaj, staff):
    for serial, rack in (("ACA4", "C3"), ("ACA2", EMI_READY), ("ACA5", "A1")):
        ProductSerial.query.filter_by(serial_no=serial).one().bin_code = rack
    _db.session.commit()
    doc = emi_order(wh, bajaj, user=staff)
    choices = dispatch.showroom_choices(doc, doc.lines[0])
    assert [u.serial_no for u in choices] == ["ACA2", "ACA5", "ACA4"]           # EMI READY, then by rack
    assert len(dispatch.showroom_choices(doc, doc.lines[0], show_all=True)) == 5
    _web(client, staff.email, "CashierSecret123")
    page = client.get(f"/sales/documents/{doc.id}").get_data(as_text=True)
    assert "ACA2 - EMI READY" in page and "ACA1" not in page.split("Choose the serials here")[1]
    client.post(f"/sales/documents/{doc.id}/showroom-pick", data={f"serial_{doc.lines[0].id}": "ACA5"})
    assert doc.dispatch_status == DispatchStatus.PICKED
    unit = ProductSerial.query.filter_by(serial_no="ACA5").one()
    assert unit.status == SerialStatus.PICKED and unit.bin_code == EMI_HOLD
    # The showroom invoices at once; the warehouse is told where to fetch it.
    client.post(f"/sales/documents/{doc.id}/emi-invoice")
    headers, _ = auth(client, wh, "ravi")
    detail = client.get(f"{API}/orders/{doc.id}", headers=headers).get_json()
    assert detail["deliver"][0]["serial_no"] == "ACA5"


def test_financiers_suggestions_settlement_and_payout_import(client, wh, pickers, bajaj):
    from openpyxl import Workbook

    doc = emi_order(wh, bajaj, loan="LAN-1001")
    lock(client, wh, doc, ["ACA1"])
    invoice = ss.convert_to_invoice(wh.tenant, wh.admin, doc)
    _db.session.commit()
    _web(client, wh.admin.email, "ClientSecret123")
    page = client.get("/warehouse/financiers").get_data(as_text=True)
    assert "+ HDB Financial Services" in page and "+ Bajaj Finserv" not in page     # suggested, not fixed
    client.post("/warehouse/financiers", data={"name": "HDB Financial Services", "short_code": "HDB"})
    assert "HDB Financial Services" in client.get("/warehouse/financiers").get_data(as_text=True)
    wb = Workbook()
    ws = wb.active
    ws.append(["Bajaj Finserv - disbursement statement"])
    ws.append(["Disb Date", "Loan Account No", "Dealer Invoice", "Net Disbursed", "Subvention", "UTR No"])
    ws.append(["08-11-2026", "LAN-1001", "", 39500, 500, "UTR778899"])
    buf = io.BytesIO()
    wb.save(buf)
    page = client.post(f"/warehouse/financiers/{bajaj.id}/payouts",
                       data={"statement": (io.BytesIO(buf.getvalue()), "s.xlsx")},
                       content_type="multipart/form-data").get_data(as_text=True)
    assert "matched by loan ref" in page and "UTR778899" in page
    import json
    import re

    rows = re.search(r'name="rows" value="([^"]*)"', page).group(1).replace("&#34;", '"').replace("&quot;", '"')
    res = client.post(f"/warehouse/financiers/{bajaj.id}/payouts",
                      data={"step": "confirm", "rows": rows, "invoice_0": str(invoice.id)}, follow_redirects=True)
    text = res.get_data(as_text=True)
    assert "1 payout recorded" in text and "500.00 of charges" in text
    from app.accounts.receivables import customer_account

    assert customer_account(wh.tenant, bajaj.customer, with_entries=False).balance == D("500")
    assert json.loads(rows)[0]["amount"] == "39500.00"


def test_the_invoice_says_financed_by(client, wh, pickers, bajaj):
    doc = emi_order(wh, bajaj)
    lock(client, wh, doc, ["ACA1"])
    invoice = ss.convert_to_invoice(wh.tenant, wh.admin, doc)
    _db.session.commit()
    from flask import render_template

    from app.models.invoice_series import DOCUMENT_TYPE_LABELS

    html = render_template("invoicing/pdf.html", invoice=invoice, document_label=DOCUMENT_TYPE_LABELS[invoice.document_type],
                           firm_name="Acme", logo_data_uri=None)
    assert "Financed by:" in html and "Bajaj Finserv" in html and "LAN-7781" in html


def test_rack_labels_print(client, wh):
    _web(client, wh.admin.email, "ClientSecret123")
    from app.warehouse.emi import rack_codes

    assert rack_codes("A1-A3, c3,EMI-READY") == ["A1", "A2", "A3", "C3", "EMI-READY"]
    res = client.post("/warehouse/racks", data={"codes": "A1-A3", "emi_ready": "1"})
    assert res.status_code == 200 and res.mimetype == "application/pdf"


def test_the_order_form_takes_the_emi_fields(client, wh, bajaj, staff):
    from tests.test_warehouse_dispatch import _form

    _web(client, staff.email, "CashierSecret123")
    page = client.get("/sales/documents/new?kind=sales_order").get_data(as_text=True)
    assert "Finance (EMI) sale" in page and "Bajaj Finserv" in page
    res = client.post("/sales/documents/new", data=_form(wh, dispatch_location_id=str(wh.a.id), is_finance_sale="1",
                                                        financier_id=str(bajaj.id), loan_ref="LAN-55",
                                                        finance_amount="50000"), follow_redirects=True)
    assert "sent to Warehouse A" in res.get_data(as_text=True)
    doc = SalesDocument.query.one()
    assert doc.is_finance_sale and doc.loan_ref == "LAN-55" and doc.finance_amount == D("50000")
