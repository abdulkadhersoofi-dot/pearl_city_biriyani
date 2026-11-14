"""Scan bills beyond purchases and expenses: a sales invoice issued outside
ARFA (kept under its own number), a credit / debit note issued to a
customer (against its invoice), and a credit / debit note from a supplier
(SupplierNote: the supplier's account, the input tax credit, Tally)."""

from datetime import date
from decimal import Decimal

import pytest

from app.models.customer import Customer
from app.models.invoice import Invoice
from app.models.note import CreditDebitNote
from app.models.purchase import PurchaseBill, Supplier, SupplierNote
from app.models.scan import POSTED, ScanDocument
from tests.test_scan import OWN, SELLER, _bill, _login, _purchase_form, _ready_doc, scanning  # noqa: F401

CUSTOMER = SELLER        # a business in Karnataka buying from the client


def _sale(**over):
    return _bill(**{"supplier_name": "Acme Traders", "supplier_gstin": OWN, "receiver_name": "Tumkur Spices Pvt Ltd",
                    "receiver_gstin": CUSTOMER, "receiver_address": "12 Market Rd", "invoice_number": "AC/77",
                    "category": "Sales", **over})


def _sale_form(**over):
    form = _purchase_form(kind="sale", invoice_number="AC/77", customer_id="", customer_name="Tumkur Spices Pvt Ltd",
                          customer_gstin=CUSTOMER, customer_address="12 Market Rd")
    form.update(over)
    return form


def test_a_scanned_sales_invoice_goes_in_under_its_own_number(client, db, scanning, client_admin, monkeypatch):
    _login(client)
    doc_id = _ready_doc(client, monkeypatch, bill=_sale())
    doc = db.session.get(ScanDocument, doc_id)
    assert doc.category == "Sales"
    page = client.get(f"/scan/{doc_id}").get_data(as_text=True)
    assert "Sales invoice" in page and "Customer in your books" in page and "Tumkur Spices Pvt Ltd" in page
    res = client.post(f"/scan/{doc_id}/post", data=_sale_form())
    assert res.status_code == 302, res.data[:600]
    invoice = Invoice.query.one()
    assert invoice.invoice_number == "AC/77" and invoice.grand_total == Decimal("5380.00")
    assert invoice.place_of_supply_state_code == "29" and invoice.total_igst == Decimal("380.00")
    customer = Customer.query.one()
    assert customer.gstin == CUSTOMER and customer.state_code == "29"
    assert db.session.get(ScanDocument, doc_id).posted_type == "invoice"
    # the same invoice number can't go in twice
    again = _ready_doc(client, monkeypatch, bill=_sale(), photo=__import__("tests.test_scan", fromlist=["_jpeg"])._jpeg("x"))
    res = client.post(f"/scan/{again}/post", data=_sale_form())
    assert res.status_code == 422 and b"already in your books" in res.data
    assert Customer.query.count() == 1


def test_a_note_to_a_customer_goes_against_its_invoice(client, db, scanning, client_admin, monkeypatch):
    from tests.test_scan import _jpeg

    _login(client)
    first = _ready_doc(client, monkeypatch, bill=_sale())
    client.post(f"/scan/{first}/post", data=_sale_form())
    invoice = Invoice.query.one()
    note_read = _sale(category="Sales - Credit Note", invoice_number="CN/5", original_invoice_number="AC/77",
                      line_items=[{"description": "Rate difference", "hsn_sac": "0909", "quantity": 1, "unit": "NOS",
                                   "rate_per_unit": 100, "gst_rate": 5, "taxable_value": 100, "igst": 5}],
                      taxable_value=100, igst=5, invoice_value=105)
    doc_id = _ready_doc(client, monkeypatch, bill=note_read, photo=_jpeg("note"))
    page = client.get(f"/scan/{doc_id}").get_data(as_text=True)
    assert f'<option value="{invoice.id}" selected' in page          # found from "against AC/77"
    res = client.post(f"/scan/{doc_id}/post", data=_sale_form(
        kind="sales_note", invoice_number="CN/5", original_invoice_id=str(invoice.id), note_type="credit",
        reason="Rate difference", bill_total="105", **{
            "line_description[]": ["Rate difference"], "line_hsn[]": ["0909"], "line_qty[]": ["1"],
            "line_unit[]": ["NOS"], "line_rate[]": ["100"], "line_discount[]": ["0"], "line_gst_rate[]": ["5"],
            "line_itc[]": ["eligible"]}))
    assert res.status_code == 302, res.data[:600]
    note = CreditDebitNote.query.one()
    assert note.note_number == "CN/5" and note.original_invoice_id == invoice.id and note.grand_total == Decimal("105.00")
    assert note.document_type.value == "credit_note"


def _supplier_note_doc(client, monkeypatch, kind="Credit", **over):
    from tests.test_scan import _jpeg

    read = _bill(category=f"Purchase - {kind} Note", invoice_number="TSCN/9", original_invoice_number="TS/26-27/0042",
                 line_items=[{"description": "Discount on cumin", "hsn_sac": "0909", "quantity": 1, "unit": "NOS",
                              "rate_per_unit": 400, "gst_rate": 5, "taxable_value": 400, "igst": 20}],
                 taxable_value=400, igst=20, invoice_value=420, **over)
    return _ready_doc(client, monkeypatch, bill=read, photo=_jpeg(kind))


NOTE_LINES = {"line_description[]": ["Discount on cumin"], "line_hsn[]": ["0909"], "line_qty[]": ["1"],
              "line_unit[]": ["NOS"], "line_rate[]": ["400"], "line_discount[]": ["0"], "line_gst_rate[]": ["5"],
              "line_itc[]": ["eligible"]}


def test_a_suppliers_credit_note_takes_off_what_is_owed_and_the_credit(client, db, scanning, client_admin, monkeypatch):
    from app.accounts.payables import supplier_account
    from app.finance import journal
    from app.reports.gstr import itc_summary

    _login(client)
    bill_doc = _ready_doc(client, monkeypatch)
    client.post(f"/scan/{bill_doc}/post", data=_purchase_form())
    bill = PurchaseBill.query.one()
    doc_id = _supplier_note_doc(client, monkeypatch)
    page = client.get(f"/scan/{doc_id}").get_data(as_text=True)
    assert "Credit / debit note from a supplier" in page and f'<option value="{bill.id}" selected' in page
    res = client.post(f"/scan/{doc_id}/post", data=_purchase_form(
        kind="supplier_note", invoice_number="TSCN/9", purchase_bill_id=str(bill.id), note_type="credit",
        reason="Discount", bill_total="420", **NOTE_LINES))
    assert res.status_code == 302, res.data[:600]
    note = SupplierNote.query.one()
    assert note.is_credit and note.note_number == "TSCN/9" and note.grand_total == Decimal("420.00")
    assert note.itc_igst == Decimal("20.00") and note.purchase_bill_id == bill.id
    assert Supplier.query.count() == 1
    account = supplier_account(scanning, bill.supplier)
    assert account.balance == Decimal("4960.00")                 # 5,380 less 420
    gstin = scanning.gstins.first()
    itc = itc_summary(scanning, gstin, note.note_date.strftime("%Y-%m"))
    # reversed in the note's own month (the bill counts in the month it was entered)
    assert itc["b2_returns"]["igst"] == Decimal("20.00")
    _, vouchers = journal.build(scanning, date(2000, 1, 1), date(2100, 1, 1))
    v = next(v for v in vouchers if v.number == "TSCN/9")
    assert v.vch_type == "Debit Note" and v.debits == v.credits
    assert client.get(f"/purchases/supplier-notes/{note.id}").status_code == 200
    assert b"TSCN/9" in client.get("/purchases/supplier-notes").data
    assert b"TSCN/9" in client.get(f"/purchases/{bill.id}").data


def test_a_suppliers_debit_note_adds_to_both(client, db, scanning, client_admin, monkeypatch):
    from app.accounts.payables import supplier_account
    from app.reports.gstr import itc_summary

    _login(client)
    doc_id = _supplier_note_doc(client, monkeypatch, kind="Debit")
    res = client.post(f"/scan/{doc_id}/post", data=_purchase_form(
        kind="supplier_note", invoice_number="TSDN/1", note_type="debit", bill_total="420", **NOTE_LINES))
    assert res.status_code == 302, res.data[:600]
    note = SupplierNote.query.one()
    assert not note.is_credit
    assert supplier_account(scanning, note.supplier).balance == Decimal("420.00")
    itc = itc_summary(scanning, scanning.gstins.first(), note.note_date.strftime("%Y-%m"))
    assert itc["a5_other"]["igst"] == Decimal("20.00")
    # voided, it counts no more
    client.post(f"/purchases/supplier-notes/{note.id}/void", data={"reason": "Entered twice"})
    assert supplier_account(scanning, note.supplier).balance == Decimal("0.00")


def test_the_phone_app_can_approve_a_sales_invoice(client, db, scanning, client_admin, monkeypatch):
    from tests.test_scan_app import API, _login as app_login, _ready

    auth = app_login(client)
    doc_id, _ = _ready(client, auth, monkeypatch, bill=_sale())
    body = client.get(f"{API}/bills/{doc_id}", headers=auth).get_json()
    assert body["kind"] == "sale" and "sale" in body["kinds"] and body["supplier"] == "Tumkur Spices Pvt Ltd"
    res = client.post(f"{API}/bills/{doc_id}/approve", json={}, headers=auth)
    assert res.status_code == 200, res.get_json()
    assert Invoice.query.one().invoice_number == "AC/77"
    assert db.session.get(ScanDocument, doc_id).status == POSTED
