"""Phase 3 purchases: the ITC math (app.utils.itc), purchase bills (stock,
weighted-average cost, landed cost, RCM, ITC eligibility), returns to the
supplier (debit notes reversing ITC), purchase orders, the private
supplier-bill attachment, GSTR-2B parse + match, ITC in GSTR-3B, and the
Purchases module flag."""

import json
from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest
from openpyxl import Workbook

from app.inventory import ledger
from app.models.audit_log import AuditLog
from app.models.inventory import ProductSerial, SerialStatus
from app.models.media import UploadedImage
from app.models.product import Product
from app.models.purchase import (
    ItcEligibility,
    MatchStatus,
    PurchaseBill,
    PurchaseOrderStatus,
    PurchaseStatus,
    Supplier,
)
from app.purchases import gstr2b
from app.purchases import services as ps
from app.reports.gstr import gstr3b_data
from app.utils import itc

D = Decimal
E, I, B = ItcEligibility.ELIGIBLE, ItcEligibility.INELIGIBLE, ItcEligibility.BLOCKED


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _on(db, tenant, *keys):
    for key in keys:
        tenant.set_feature(key, True)
    db.session.commit()


# --- the ITC math -----------------------------------------------------------------------------

def test_intra_state_line_splits_cgst_sgst_and_inter_state_is_igst():
    intra = itc.compute_purchase_line(D("10"), D("100"), D("0"), D("18"), True)
    assert (intra["taxable_value"], intra["cgst_amount"], intra["sgst_amount"], intra["igst_amount"]) == (
        D("1000.00"), D("90.00"), D("90.00"), D("0.00"))
    inter = itc.compute_purchase_line(D("10"), D("100"), D("0"), D("18"), False)
    assert (inter["cgst_amount"], inter["igst_amount"]) == (D("0.00"), D("180.00"))


def test_zero_rated_purchase_line_has_no_tax_and_no_itc():
    line = itc.compute_purchase_line(D("3"), D("40"), D("0"), D("0"), True)
    assert itc.line_tax(line) == D("0") and itc.claimable(line, E) == (D("0"), D("0"), D("0"))


def test_only_eligible_lines_give_credit():
    line = itc.compute_purchase_line(D("1"), D("1000"), D("0"), D("18"), True)
    assert itc.claimable(line, E) == (D("90.00"), D("90.00"), D("0.00"))
    assert itc.claimable(line, I) == (0, 0, 0)
    assert itc.claimable(line, B) == (0, 0, 0)


def test_totals_round_to_rupee_and_rcm_excludes_tax_from_supplier_payable():
    lines = [itc.compute_purchase_line(D("1"), D("100.40"), D("0"), D("5"), True)]
    normal = itc.purchase_totals(lines, [E], D("0"), is_rcm=False)
    assert normal["grand_total"] == D("105.00") and normal["round_off"] == D("-0.42")  # 100.40 + 5.02 = 105.42
    rcm = itc.purchase_totals(lines, [E], D("0"), is_rcm=True)
    assert rcm["grand_total"] == D("100.00")  # supplier gets the value; the tax is paid to the government
    assert rcm["itc_cgst"] == D("2.51") and rcm["itc_sgst"] == D("2.51")


def test_landed_cost_spreads_freight_and_adds_unclaimable_gst():
    lines = [itc.compute_purchase_line(D("10"), D("100"), D("0"), D("18"), True),
             itc.compute_purchase_line(D("5"), D("200"), D("0"), D("18"), True)]
    costs = itc.landed_costs(lines, [D("10"), D("5")], [E, B], D("100"))
    # Freight 100 split by taxable value 1000:1000 = 50 each.
    assert costs[0] == (D("50.00"), D("105.0000"))  # (1000 + 50) / 10, GST claimed back
    assert costs[1] == (D("50.00"), D("246.0000"))  # (1000 + 50 + 180 blocked GST) / 5


def test_discount_and_odd_paise_round_half_up():
    line = itc.compute_purchase_line(D("3"), D("33.33"), D("10"), D("12"), True)
    # 99.99 less 10% = 89.991 -> 89.99; 6% of that = 5.3994 -> 5.40 each half
    assert line["taxable_value"] == D("89.99")
    assert line["cgst_amount"] == D("5.40") and line["sgst_amount"] == D("5.40")


def test_return_itc_is_the_credit_the_returned_goods_gave():
    line = itc.compute_purchase_line(D("2"), D("500"), D("0"), D("18"), False)
    assert itc.return_itc(line, E) == (D("0"), D("0"), D("180.00"))
    assert itc.return_itc(line, B) == (0, 0, 0)


# --- fixtures ---------------------------------------------------------------------------------

@pytest.fixture()
def supplier(db, tenant):
    s = Supplier(tenant_id=tenant.id, name="Sri Balaji Distributors", gstin="27BBBBB1111B1Z5", state_code="27",
                 state_name="Maharashtra")
    db.session.add(s)
    db.session.commit()
    return s


@pytest.fixture()
def other_state_supplier(db, tenant):
    s = Supplier(tenant_id=tenant.id, name="Chennai Mills", gstin="33CCCCC2222C1Z5", state_code="33",
                 state_name="Tamil Nadu")
    db.session.add(s)
    db.session.commit()
    return s


@pytest.fixture()
def rice(db, tenant):
    p = Product(tenant_id=tenant.id, name="Rice 25kg", hsn_or_sac_code="1006", gst_rate=D("5"), unit="BAG",
                sale_price=D("1500"))
    db.session.add(p)
    db.session.commit()
    return p


def _pline(product=None, qty="10", rate="1000", gst="5", eligibility=E, description=None, **kw):
    return ps.PurchaseLineInput(
        product_id=product.id if product else None, description=description or (product.name if product else "Misc"),
        hsn_or_sac_code=product.hsn_or_sac_code if product else "9999", qty=D(qty), unit="BAG", rate=D(rate),
        gst_rate=D(gst), itc_eligibility=eligibility, **kw)


def _bill(db, tenant, user, supplier, *lines, invoice_no="SB/101", other="0", rcm=False, bill_date=None):
    bill = ps.create_purchase_bill(tenant, user, ps.PurchaseInput(
        supplier_id=supplier.id, gstin_id=tenant.gstins.first().id, supplier_invoice_no=invoice_no,
        supplier_invoice_date=bill_date or date.today(), location_id=ledger.default_location(tenant.id).id,
        lines=list(lines), bill_date=bill_date, is_rcm=rcm, other_charges=D(other),
    ))
    db.session.commit()
    return bill


def _stock(tenant, product):
    return ledger.stock_on_hand(tenant.id, [product.id]).get(product.id, D("0"))


# --- purchase bills -----------------------------------------------------------------------------

def test_purchase_bill_receives_stock_at_landed_cost_and_records_itc(db, tenant, client_admin, supplier, rice):
    bill = _bill(db, tenant, client_admin, supplier, _pline(rice, "10", "1000"), other="500")
    assert bill.number.startswith("PB")
    assert bill.total_taxable_value == D("10000.00") and bill.total_cgst == D("250.00") and bill.total_igst == 0
    assert bill.grand_total == D("11000.00")  # 10000 + 500 GST + 500 freight
    assert bill.itc_total == D("500.00")
    assert bill.lines[0].unit_cost == D("1050.0000")  # freight in, credit-able GST out
    assert _stock(tenant, rice) == D("10")
    assert rice.cost_price == D("1050.00") and rice.purchase_rate == D("1000")


def test_inter_state_supplier_charges_igst(db, tenant, client_admin, other_state_supplier, rice):
    bill = _bill(db, tenant, client_admin, other_state_supplier, _pline(rice, "2", "1000"))
    assert bill.total_igst == D("100.00") and bill.total_cgst == 0


def test_blocked_credit_becomes_part_of_cost(db, tenant, client_admin, supplier, rice):
    bill = _bill(db, tenant, client_admin, supplier, _pline(rice, "10", "1000", eligibility=B))
    assert bill.itc_total == 0
    assert bill.lines[0].unit_cost == D("1050.0000")


def test_rcm_bill_owes_supplier_only_the_value(db, tenant, client_admin, supplier):
    bill = _bill(db, tenant, client_admin, supplier, _pline(None, "1", "10000", gst="18", description="Legal fees"),
                 rcm=True)
    assert bill.grand_total == D("10000.00") and bill.total_tax == D("1800.00") and bill.itc_total == D("1800.00")


def test_duplicate_supplier_invoice_is_refused(db, tenant, client_admin, supplier, rice):
    _bill(db, tenant, client_admin, supplier, _pline(rice), invoice_no="SB/7")
    with pytest.raises(ps.PurchaseError, match="already entered"):
        _bill(db, tenant, client_admin, supplier, _pline(rice), invoice_no="sb/7")


def test_bad_gst_rate_and_zero_qty_are_refused(db, tenant, client_admin, supplier, rice):
    with pytest.raises(ps.PurchaseError, match="isn't a GST rate"):
        _bill(db, tenant, client_admin, supplier, _pline(rice, gst="7"))
    db.session.rollback()
    with pytest.raises(ps.PurchaseError, match="more than 0"):
        _bill(db, tenant, client_admin, supplier, _pline(rice, qty="0"))


def test_void_bill_takes_goods_out_and_restores_cost(db, tenant, client_admin, supplier, rice):
    _bill(db, tenant, client_admin, supplier, _pline(rice, "10", "1000"), invoice_no="A1")
    bill = _bill(db, tenant, client_admin, supplier, _pline(rice, "10", "1200"), invoice_no="A2")
    assert rice.cost_price == D("1100.00")
    ps.void_purchase_bill(bill, client_admin, "entered twice")
    db.session.commit()
    assert _stock(tenant, rice) == D("10") and rice.cost_price == D("1000.00")
    assert bill.status == PurchaseStatus.VOIDED
    with pytest.raises(ps.PurchaseError, match="already voided"):
        ps.void_purchase_bill(bill, client_admin, "again")


def test_batch_and_serial_requirements_on_purchase(db, tenant, client_admin, supplier):
    _on(db, tenant, "batch_expiry", "serial_imei")
    med = Product(tenant_id=tenant.id, name="Syrup", hsn_or_sac_code="3004", gst_rate=D("12"), track_batch=True)
    phone = Product(tenant_id=tenant.id, name="Phone", hsn_or_sac_code="8517", gst_rate=D("18"), track_serial=True,
                    warranty_months=6)
    db.session.add_all([med, phone])
    db.session.commit()
    with pytest.raises(ps.PurchaseError, match="batch number"):
        _bill(db, tenant, client_admin, supplier, _pline(med, "5", "50", gst="12"))
    db.session.rollback()
    with pytest.raises(ps.PurchaseError, match="one serial"):
        _bill(db, tenant, client_admin, supplier, _pline(phone, "2", "9000", gst="18", serials=["A"]))
    db.session.rollback()
    bill = _bill(db, tenant, client_admin, supplier, _pline(med, "5", "50", gst="12", batch_no="B7",
                                                            expiry_date=date(2030, 1, 1)),
                 _pline(phone, "2", "9000", gst="18", serials=["IM1", "IM2"]))
    assert bill.lines[0].batch.batch_no == "B7"
    assert ProductSerial.query.filter_by(purchase_bill_id=bill.id).count() == 2
    with pytest.raises(ps.PurchaseError, match="already in your records"):
        _bill(db, tenant, client_admin, supplier, _pline(phone, "1", "9000", gst="18", serials=["IM1"]),
              invoice_no="X2")


# --- returns to the supplier --------------------------------------------------------------------

def test_purchase_return_reverses_itc_and_stock_and_can_be_voided(db, tenant, client_admin, supplier, rice):
    bill = _bill(db, tenant, client_admin, supplier, _pline(rice, "10", "1000"))
    ret = ps.create_purchase_return(bill, client_admin, {bill.lines[0].id: D("4")}, "damaged bags")
    db.session.commit()
    assert ret.number.startswith("PDN")
    assert ret.total_taxable_value == D("4000.00")
    assert (ret.itc_cgst, ret.itc_sgst) == (D("100.00"), D("100.00"))
    assert _stock(tenant, rice) == D("6")
    with pytest.raises(ps.PurchaseError, match="Only 6"):
        ps.create_purchase_return(bill, client_admin, {bill.lines[0].id: D("7")}, "more")
    with pytest.raises(ps.PurchaseError, match="void those returns first"):
        ps.void_purchase_bill(bill, client_admin, "x")
    ps.void_purchase_return(ret, client_admin, "supplier refused")
    db.session.commit()
    assert _stock(tenant, rice) == D("10")


def test_last_return_takes_the_exact_remainder(db, tenant, client_admin, supplier, rice):
    bill = _bill(db, tenant, client_admin, supplier, _pline(rice, "3", "33.33"))
    r1 = ps.create_purchase_return(bill, client_admin, {bill.lines[0].id: D("1")}, "a")
    r2 = ps.create_purchase_return(bill, client_admin, {bill.lines[0].id: D("2")}, "b")
    assert r1.total_taxable_value + r2.total_taxable_value == bill.total_taxable_value
    assert r1.total_cgst + r2.total_cgst == bill.total_cgst


def test_returning_a_serial_item_picks_unsold_units(db, tenant, client_admin, supplier):
    _on(db, tenant, "serial_imei")
    phone = Product(tenant_id=tenant.id, name="Phone", hsn_or_sac_code="8517", gst_rate=D("18"), track_serial=True)
    db.session.add(phone)
    db.session.commit()
    bill = _bill(db, tenant, client_admin, supplier, _pline(phone, "2", "9000", gst="18", serials=["S1", "S2"]))
    ps.create_purchase_return(bill, client_admin, {bill.lines[0].id: D("1")}, "faulty")
    statuses = sorted(u.status for u in ProductSerial.query.filter_by(purchase_bill_id=bill.id))
    assert statuses == sorted([SerialStatus.IN_STOCK, SerialStatus.RETURNED_TO_SUPPLIER])


# --- purchase orders -------------------------------------------------------------------------------

def test_order_converts_to_a_bill(client, db, tenant, client_admin, supplier, rice):
    _on(db, tenant, "purchases")
    order = ps.create_purchase_order(tenant, client_admin, supplier.id, tenant.gstins.first().id,
                                     [_pline(rice, "20", "950")])
    db.session.commit()
    assert order.number.startswith("PO") and order.estimated_total == D("19950.00")
    _login(client, "admin@acme.example.com", "ClientSecret123")
    page = client.get(f"/purchases/new?order={order.id}")
    assert page.status_code == 200 and b"Rice 25kg" in page.data
    _bill_via_form(client, tenant, supplier, rice, extra={"order": str(order.id)})
    db.session.refresh(order)
    assert order.status == PurchaseOrderStatus.CONVERTED and len(order.bills) == 1
    with pytest.raises(ps.PurchaseError):
        ps.cancel_purchase_order(order, "too late")


def _bill_via_form(client, tenant, supplier, product, invoice_no="F-1", extra=None, files=None):
    data = {
        "supplier_id": str(supplier.id), "gstin_id": str(tenant.gstins.first().id),
        "supplier_invoice_no": invoice_no, "supplier_invoice_date": date.today().isoformat(),
        "location_id": str(ledger.default_location(tenant.id).id),
        "line_product_id[]": [str(product.id)], "line_description[]": [product.name], "line_hsn[]": ["1006"],
        "line_qty[]": ["5"], "line_unit[]": ["BAG"], "line_rate[]": ["1000"], "line_discount[]": [""],
        "line_gst_rate[]": ["5"], "line_itc[]": ["eligible"], "other_charges": "0",
        **(extra or {}),
    }
    if files:
        data.update(files)
    return client.post("/purchases/new", data=data, content_type="multipart/form-data")


# --- screens, flag, attachment ------------------------------------------------------------------------

PURCHASE_PAGES = ["/purchases/", "/purchases/?rcm=1", "/purchases/new", "/purchases/returns", "/purchases/orders",
                  "/purchases/orders/new", "/purchases/suppliers", "/purchases/suppliers/new", "/purchases/gstr2b",
                  "/bulk/suppliers/"]


def test_purchases_is_404_and_hidden_when_off(client, db, tenant, client_admin):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    for url in PURCHASE_PAGES + ["/bulk/suppliers/template.xlsx"]:
        assert client.get(url).status_code == 404, url
    assert b'href="/purchases/"' not in client.get("/").data


def test_purchase_screens_render_and_a_bill_posts_through_the_form(client, db, tenant, client_admin, supplier, rice):
    _on(db, tenant, "purchases")
    _login(client, "admin@acme.example.com", "ClientSecret123")
    for url in PURCHASE_PAGES:
        assert client.get(url).status_code == 200, url
    pdf = b"%PDF-1.4\n%fake but starts right\n"
    resp = _bill_via_form(client, tenant, supplier, rice, files={"attachment": (BytesIO(pdf), "bill.pdf")})
    assert resp.status_code == 302
    bill = PurchaseBill.query.one()
    assert AuditLog.query.filter_by(action="purchase_bill_posted", entity_id=bill.id).count() == 1
    assert client.get(f"/purchases/{bill.id}").status_code == 200
    assert client.get(f"/purchases/{bill.id}/return").status_code == 200
    # The attachment is private: never on the public media route, only via the bill.
    image = db.session.get(UploadedImage, bill.attachment_id)
    assert image.is_private
    assert client.get(f"/media/{image.id}").status_code == 404
    att = client.get(f"/purchases/{bill.id}/attachment")
    assert att.status_code == 200 and att.data == pdf and att.mimetype == "application/pdf"

    # Refused form keeps the lines.
    resp = _bill_via_form(client, tenant, supplier, rice, invoice_no="F-1")
    assert resp.status_code == 400 and b"already entered" in resp.data and b"Rice 25kg" in resp.data

    resp = client.post(f"/purchases/{bill.id}/return", data={f"qty_{bill.lines[0].id}": "2", "reason": "torn"})
    assert resp.status_code == 302
    resp = client.post(f"/purchases/{bill.id}/void", data={"reason": "x"})
    assert b"void those returns first" in client.get(f"/purchases/{bill.id}").data


def test_bad_attachment_type_is_refused(client, db, tenant, client_admin, supplier, rice):
    _on(db, tenant, "purchases")
    _login(client, "admin@acme.example.com", "ClientSecret123")
    resp = _bill_via_form(client, tenant, supplier, rice, files={"attachment": (BytesIO(b"MZ..."), "bill.exe")})
    assert resp.status_code == 400 and PurchaseBill.query.count() == 0


def test_supplier_crud_and_gstin_state_check(client, db, tenant, client_admin):
    _on(db, tenant, "purchases")
    _login(client, "admin@acme.example.com", "ClientSecret123")
    resp = client.post("/purchases/suppliers/new", data={"name": "X", "gstin": "33CCCCC2222C1Z5", "state_code": "27"})
    assert resp.status_code == 200 and b"match the state in the GSTIN" in resp.data
    resp = client.post("/purchases/suppliers/new", data={"name": "X", "gstin": "33CCCCC2222C1Z5", "state_code": "33",
                                                         "payment_terms_days": "30", "opening_balance": "1200"})
    assert resp.status_code == 302
    s = Supplier.query.one()
    assert s.state_name == "Tamil Nadu" and s.opening_balance == D("1200.00")


def test_suppliers_bulk_import(client, db, tenant, client_admin):
    from app.bulk.specs import SPECS

    _on(db, tenant, "purchases")
    spec = SPECS["suppliers"]
    wb = Workbook()
    ws = wb.active
    ws.append([c.header for c in spec.columns])
    ws.append(["Ganesh Agencies", "27DDDDD3333D1Z5", "", "Main Rd", "Pune", "411001", "Ganesh", "98", "", "15", "250"])
    buf = BytesIO()
    wb.save(buf)
    _login(client, "admin@acme.example.com", "ClientSecret123")
    resp = client.post("/bulk/suppliers/upload", data={"file": (BytesIO(buf.getvalue()), "s.xlsx")},
                       content_type="multipart/form-data")
    assert resp.status_code in (200, 302)
    client.post("/bulk/suppliers/confirm")
    s = Supplier.query.filter_by(name="Ganesh Agencies").one()
    assert s.state_code == "27" and s.payment_terms_days == 15 and s.opening_balance == D("250.00")


# --- GSTR-2B ------------------------------------------------------------------------------------------

def _json_2b(*invoices):
    """invoices: (ctin, inum, val, txval, cgst, sgst, igst, rev)"""
    by_ctin = {}
    for ctin, inum, val, txval, cgst, sgst, igst, rev in invoices:
        by_ctin.setdefault(ctin, []).append({
            "inum": inum, "dt": date.today().strftime("%d-%m-%Y"), "val": val, "rev": rev, "itcavl": "Y",
            "items": [{"txval": txval, "cgst": cgst, "sgst": sgst, "igst": igst, "cess": 0}],
        })
    return json.dumps({"data": {"docdata": {"b2b": [{"ctin": c, "trdnm": f"Supplier {c[:4]}", "inv": inv}
                                                      for c, inv in by_ctin.items()]}}}).encode()


def test_invoice_key_ignores_punctuation_case_and_leading_zeros():
    keys = {gstr2b.invoice_key(x) for x in ("INV/0042", "inv-42", "INV 42", "INV0042")}
    assert keys == {"INV42"}


def test_json_parse_and_auto_match(db, tenant, client_admin, supplier, rice):
    b_exact = _bill(db, tenant, client_admin, supplier, _pline(rice, "1", "1000"), invoice_no="SB/0042")
    b_fuzzy = _bill(db, tenant, client_admin, supplier, _pline(rice, "2", "1000"), invoice_no="SB-1043A")
    b_diff = _bill(db, tenant, client_admin, supplier, _pline(rice, "3", "1000"), invoice_no="SB/77")
    b_books = _bill(db, tenant, client_admin, supplier, _pline(rice, "1", "500"), invoice_no="NOT-FILED")
    b_rcm = _bill(db, tenant, client_admin, supplier, _pline(None, "1", "1000", gst="18", description="Freight"),
                  invoice_no="GTA/9", rcm=True)
    data = _json_2b(
        ("27BBBBB1111B1Z5", "sb-42", 1050, 1000, 25, 25, 0, "N"),
        ("27BBBBB1111B1Z5", "SB/1043", 2100.5, 2000, 50, 50, 0, "N"),  # typo'd no., value within Re 1
        ("27BBBBB1111B1Z5", "SB/77", 3500, 3200, 150, 150, 0, "N"),  # value off
        ("27BBBBB1111B1Z5", "SB/999", 500, 476, 12, 12, 0, "N"),  # never entered
        ("27BBBBB1111B1Z5", "GTA/9", 1180, 1000, 90, 90, 0, "Y"),
    )
    source, rows = gstr2b.parse("2b.json", data)
    assert source == "json" and len(rows) == 5
    imp = gstr2b.save_import(tenant, tenant.gstins.first(), date.today().strftime("%Y-%m"), "2b.json", source, rows,
                             client_admin)
    db.session.commit()
    by_no = {e.invoice_no: e for e in imp.entries}
    assert (by_no["sb-42"].status, by_no["sb-42"].matched_bill_id) == (MatchStatus.MATCHED, b_exact.id)
    assert (by_no["SB/1043"].status, by_no["SB/1043"].matched_bill_id) == (MatchStatus.MATCHED, b_fuzzy.id)
    assert "written differently" in by_no["SB/1043"].match_note
    assert (by_no["SB/77"].status, by_no["SB/77"].matched_bill_id) == (MatchStatus.MISMATCH, b_diff.id)
    assert by_no["SB/999"].status == MatchStatus.ONLY_IN_2B
    assert by_no["GTA/9"].is_rcm and by_no["GTA/9"].matched_bill_id == b_rcm.id
    assert gstr2b.only_in_books(imp) == [b_books]

    # Manual match survives a re-run of auto matching; unmatch frees it.
    gstr2b.manual_match(by_no["SB/999"], b_books)
    gstr2b.auto_match(imp)
    assert by_no["SB/999"].matched_bill_id == b_books.id and by_no["SB/999"].manually_matched
    gstr2b.unmatch(by_no["SB/999"])
    assert by_no["SB/999"].status == MatchStatus.ONLY_IN_2B


def test_rcm_rows_never_match_regular_bills(db, tenant, client_admin, supplier, rice):
    _bill(db, tenant, client_admin, supplier, _pline(rice, "1", "1000"), invoice_no="R1")
    _, rows = gstr2b.parse("x.json", _json_2b(("27BBBBB1111B1Z5", "R1", 1050, 1000, 25, 25, 0, "Y")))
    imp = gstr2b.save_import(tenant, tenant.gstins.first(), "2026-01", "x.json", "json", rows, client_admin)
    assert imp.entries[0].status == MatchStatus.ONLY_IN_2B


def test_excel_parse_merges_rows_per_rate():
    wb = Workbook()
    ws = wb.active
    ws.title = "B2B"
    ws.append(["Goods and Services Tax - GSTR-2B"])
    ws.append(["GSTIN of supplier", "Trade/Legal name", "Invoice details", None, None, None, "Place of supply",
               "Supply Attract Reverse Charge", "Rate(%)", "Taxable Value (₹)", "Tax Amount", None, None, None,
               "GSTR-1/IFF/GSTR-5 Period", "GSTR-1/IFF/GSTR-5 Filing Date", "ITC Availability"])
    ws.append([None, None, "Invoice number", "Invoice type", "Invoice Date", "Invoice Value(₹)", None, None, None, None,
               "Integrated Tax(₹)", "Central Tax(₹)", "State/UT Tax(₹)", "Cess(₹)"])
    ws.append(["27BBBBB1111B1Z5", "Balaji", "SB/5", "Regular", "05-01-2026", 1170, "Maharashtra", "No", 5, 600, 0, 15, 15, 0, "", "", "Yes"])
    ws.append(["27BBBBB1111B1Z5", "Balaji", "SB/5", "Regular", "05-01-2026", 1170, "Maharashtra", "No", 18, 400, 0, 36, 36, 0, "", "", "Yes"])
    ws.append(["33CCCCC2222C1Z5", "Chennai", "CM/1", "Regular", "06-01-2026", 236, "Maharashtra", "Yes", 18, 200, 36, 0, 0, 0, "", "", "No"])
    buf = BytesIO()
    wb.save(buf)
    source, rows = gstr2b.parse("2b.xlsx", buf.getvalue())
    assert source == "excel" and len(rows) == 2
    sb5 = next(r for r in rows if r.invoice_no == "SB/5")
    assert (sb5.taxable_value, sb5.cgst, sb5.invoice_value) == (D("1000.00"), D("51.00"), D("1170.00"))
    cm1 = next(r for r in rows if r.invoice_no == "CM/1")
    assert cm1.is_rcm and not cm1.itc_available and cm1.igst == D("36.00") and cm1.invoice_date == date(2026, 1, 6)


def test_bad_2b_files_are_refused():
    with pytest.raises(gstr2b.Gstr2bError):
        gstr2b.parse("x.csv", b"a,b")
    with pytest.raises(gstr2b.Gstr2bError):
        gstr2b.parse("x.json", b"{not json")
    with pytest.raises(gstr2b.Gstr2bError):
        gstr2b.parse("x.xlsx", b"not a zip")


def test_gstr2b_screens(client, db, tenant, client_admin, supplier, rice):
    _on(db, tenant, "purchases")
    bill = _bill(db, tenant, client_admin, supplier, _pline(rice, "1", "1000"), invoice_no="SB/1")
    _login(client, "admin@acme.example.com", "ClientSecret123")
    data = _json_2b(("27BBBBB1111B1Z5", "SB/2", 99, 90, 2, 2, 0, "N"))
    resp = client.post("/purchases/gstr2b", data={"gstin_id": str(tenant.gstins.first().id),
                                                  "period": date.today().strftime("%Y-%m"),
                                                  "file": (BytesIO(data), "2b.json")}, content_type="multipart/form-data")
    assert resp.status_code == 302
    page = client.get(resp.headers["Location"])
    assert page.status_code == 200 and b"SB/2" in page.data
    from app.models.purchase import Gstr2bEntry

    entry = Gstr2bEntry.query.one()
    client.post(f"/purchases/gstr2b/entries/{entry.id}/match", data={"bill_id": str(bill.id)})
    assert entry.matched_bill_id == bill.id and entry.manually_matched
    client.post(f"/purchases/gstr2b/entries/{entry.id}/unmatch")
    assert entry.matched_bill_id is None


# --- ITC in GSTR-3B -------------------------------------------------------------------------------------

def test_gstr3b_itc_only_with_purchases_on(db, tenant, client_admin, supplier, rice):
    gstin = tenant.gstins.first()
    period = date.today().strftime("%Y-%m")
    _bill(db, tenant, client_admin, supplier, _pline(rice, "10", "1000"), invoice_no="I1")  # 250 + 250 eligible
    _bill(db, tenant, client_admin, supplier, _pline(rice, "2", "1000", eligibility=B), invoice_no="I2")  # 50 + 50 blocked
    rcm = _bill(db, tenant, client_admin, supplier, _pline(None, "1", "1000", gst="18", description="GTA"),
                invoice_no="I3", rcm=True)  # 90 + 90 RCM
    bill = PurchaseBill.query.filter_by(supplier_invoice_no="I1").one()
    ps.create_purchase_return(bill, client_admin, {bill.lines[0].id: D("2")}, "short")  # reverses 50 + 50
    db.session.commit()

    assert "itc" not in gstr3b_data(tenant, gstin, period)  # module off: exactly as before

    _on(db, tenant, "purchases")
    data = gstr3b_data(tenant, gstin, period)["itc"]
    assert data["rcm_inward"]["taxable_value"] == D("1000.00") and data["rcm_tax_payable"] == D("180.00")
    assert data["a3_rcm"]["cgst"] == D("90.00")
    assert data["a5_other"]["cgst"] == D("300.00")  # 250 + 50
    assert data["b1_ineligible"]["cgst"] == D("50.00")
    assert data["b2_returns"]["cgst"] == D("50.00")
    assert data["net"]["cgst"] == D("290.00")  # 90 + 300 - 50 - 50
    assert data["net"]["igst"] == 0
    assert rcm.itc_total == D("180.00")


def test_gstr3b_pages_show_itc(client, db, tenant, client_admin, supplier, rice):
    _on(db, tenant, "purchases")
    _bill(db, tenant, client_admin, supplier, _pline(rice, "1", "1000"))
    _login(client, "admin@acme.example.com", "ClientSecret123")
    q = f"gstin_id={tenant.gstins.first().id}&period={date.today().strftime('%Y-%m')}"
    page = client.get(f"/reports/gstr3b?{q}")
    assert page.status_code == 200 and b"All other ITC" in page.data
    body = page.data.decode()
    assert "Input Tax Credit isn't tracked" not in body  # the old caveat is replaced, not stacked
    assert body.index("GSTR-3B working paper") < body.index("All other ITC")  # inside the page, after its header
    assert client.get(f"/reports/gstr3b.xlsx?{q}").status_code == 200
    assert client.get(f"/reports/gstr3b.pdf?{q}").data.startswith(b"%PDF")
    assert json.loads(client.get(f"/reports/gstr3b.json?{q}").data)["itc"]["a5_other"]["cgst"] in ("25.00", 25.0, "25")


def test_alpine_attributes_survive_html_parsing(client, db, tenant, client_admin, supplier):
    """A |tojson string inside a double-quoted x-data attribute ends the
    attribute early and the whole form goes dead - every x-data on the
    new screens must come through the HTML parser whole."""
    from html.parser import HTMLParser

    class Collect(HTMLParser):
        def __init__(self):
            super().__init__()
            self.values = []

        def handle_starttag(self, tag, attrs):
            self.values += [v for k, v in attrs if (k.startswith("x-") or k.startswith(":") or k.startswith("@")) and v]

    for key in ("inventory", "purchases", "batch_expiry", "serial_imei"):
        tenant.set_feature(key, True)
    db.session.commit()
    _login(client, "admin@acme.example.com", "ClientSecret123")
    for url in ("/purchases/new", "/purchases/orders/new", "/inventory/adjustments/new", "/inventory/transfers/new",
                "/inventory/labels"):
        parser = Collect()
        parser.feed(client.get(url).data.decode())
        assert parser.values, url
        for value in parser.values:
            assert value.count("(") == value.count(")") and value.count("{") == value.count("}"), (url, value)


@pytest.mark.parametrize("reg", ["composition", "unregistered"])
def test_no_itc_for_composition_or_unregistered_clients(db, tenant, client_admin, supplier, rice, reg):
    from app.models.tenant import RegistrationType

    tenant.registration_type = RegistrationType(reg)
    if reg == "unregistered":
        tenant.gstins.first().gstin = None
    db.session.commit()
    assert not ps.can_claim_itc(tenant)
    bill = _bill(db, tenant, client_admin, supplier, _pline(rice, "10", "1000"))
    assert bill.itc_total == 0 and bill.total_tax == D("500.00")
    assert bill.lines[0].itc_eligibility == I
    assert bill.lines[0].unit_cost == D("1050.0000")  # the GST is part of the cost
    ret = ps.create_purchase_return(bill, client_admin, {bill.lines[0].id: D("1")}, "x")
    assert ret.itc_cgst + ret.itc_sgst + ret.itc_igst == 0
