"""Purchase orders billed in parts, and a supplier's bill finding its order
(app/purchases/matching.py): by the number printed on it, else by its items,
quantities and rates - with what differs said in words, and a person
confirming before anything is posted."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.inventory import ledger
from app.models.product import Product
from app.models.purchase import PurchaseOrder, PurchaseOrderStatus, Supplier
from app.purchases import matching
from app.purchases import services as ps
from app.scan.engine import reader
from tests.test_scan import OWN, SELLER, FakeModel, _bill, _jpeg, _login, _purchase_form, _read, _upload

D = Decimal
TODAY = date.today()
S = PurchaseOrderStatus
ORDERED = date(2026, 10, 15)       # before the scanned bill's date (20 Oct 2026, tests/test_scan.py)


@pytest.fixture()
def buying(db, tenant, client_admin):
    for key in ("purchases", "inventory"):
        tenant.set_feature(key, True)
    supplier = Supplier(tenant_id=tenant.id, name="Tumkur Spices Pvt Ltd", gstin=SELLER, state_code="29",
                        state_name="Karnataka")
    other = Supplier(tenant_id=tenant.id, name="Chennai Mills", gstin="33CCCCC2222C1Z5", state_code="33",
                     state_name="Tamil Nadu")
    cumin = Product(tenant_id=tenant.id, name="Cumin seeds 1kg", hsn_or_sac_code="0909", gst_rate=D("5"), unit="KGS",
                    sale_price=D("500"))
    tin = Product(tenant_id=tenant.id, name="Steel container", hsn_or_sac_code="7323", gst_rate=D("18"), unit="NOS",
                  sale_price=D("700"))
    db.session.add_all([supplier, other, cumin, tin])
    db.session.commit()
    return type("B", (), dict(tenant=tenant, admin=client_admin, supplier=supplier, other=other, cumin=cumin, tin=tin))


def _pline(product, qty, rate, gst=None, **kw):
    return ps.PurchaseLineInput(product_id=product.id if product else None,
                                description=kw.pop("description", product.name if product else "Misc"),
                                hsn_or_sac_code=product.hsn_or_sac_code if product else "9999", qty=D(qty),
                                unit=product.unit if product else "NOS", rate=D(rate),
                                gst_rate=D(gst if gst is not None else product.gst_rate), **kw)


def _order(db, b, *lines, supplier=None, on=None):
    order = ps.create_purchase_order(b.tenant, b.admin, (supplier or b.supplier).id, b.tenant.gstins.first().id,
                                     list(lines), order_date=on or TODAY - timedelta(days=3))
    db.session.commit()
    return order


def _bill_for(db, b, order, *lines, invoice_no="TS/1", supplier=None):
    bill = ps.create_purchase_bill(b.tenant, b.admin, ps.PurchaseInput(
        supplier_id=(supplier or b.supplier).id, gstin_id=b.tenant.gstins.first().id, supplier_invoice_no=invoice_no,
        supplier_invoice_date=TODAY, location_id=ledger.default_location(b.tenant.id).id, lines=list(lines),
        purchase_order_id=order.id if order else None))
    db.session.commit()
    return bill


def _row(product, qty, rate, gst=None, description=None):
    return matching.Row(description or product.name, D(qty), D(rate), D(gst if gst is not None else product.gst_rate),
                        product.id if product else None, product.hsn_or_sac_code if product else "")


# --- billed in parts -----------------------------------------------------------------------------------

def test_an_order_is_settled_by_several_bills(db, buying):
    order = _order(db, buying, _pline(buying.cumin, 10, 400), _pline(buying.tin, 5, 500))
    assert order.status == S.OPEN

    first = _bill_for(db, buying, order, _pline(buying.cumin, 4, 400), invoice_no="TS/1")
    lines = {l.description: l for l in order.lines}
    assert order.status == S.PARTLY
    assert lines["Cumin seeds 1kg"].billed_qty == 4 and lines["Cumin seeds 1kg"].outstanding == 6
    assert lines["Steel container"].billed_qty == 0
    assert first.lines[0].order_line_id == lines["Cumin seeds 1kg"].id

    second = _bill_for(db, buying, order, _pline(buying.cumin, 6, 400), _pline(buying.tin, 5, 500), invoice_no="TS/2")
    assert order.status == S.CONVERTED and all(l.outstanding == 0 for l in order.lines)
    assert [b.number for b in order.bills] == [first.number, second.number]

    # A voided bill gives its share back.
    ps.void_purchase_bill(second, buying.admin, "Wrong bill")
    db.session.commit()
    assert order.status == S.PARTLY and lines["Cumin seeds 1kg"].billed_qty == 4 and lines["Steel container"].billed_qty == 0
    ps.void_purchase_bill(first, buying.admin, "Wrong bill")
    db.session.commit()
    assert order.status == S.OPEN and lines["Cumin seeds 1kg"].billed_qty == 0


def test_the_rest_not_coming_closes_the_order_short(client, db, buying):
    order = _order(db, buying, _pline(buying.cumin, 10, 400))
    with pytest.raises(ps.PurchaseError, match="partly billed"):
        matching.close_short(order, "Supplier out of stock")
    _bill_for(db, buying, order, _pline(buying.cumin, 4, 400))
    _login(client)
    page = client.get(f"/purchases/orders/{order.id}").get_data(as_text=True)
    assert "Partly billed" in page and "Still to come" in page and "Close order" in page
    client.post(f"/purchases/orders/{order.id}/close", data={"reason": "Supplier out of stock"})
    db.session.refresh(order)
    assert order.status == S.CONVERTED and "Closed short: Supplier out of stock" in order.cancel_reason


def test_the_bill_form_made_from_an_order_shows_what_is_still_to_come(client, db, buying):
    from app.purchases.routes import _rows_from_order

    order = _order(db, buying, _pline(buying.cumin, 10, 400), _pline(buying.tin, 5, 500))
    assert [(r["description"], r["qty"]) for r in _rows_from_order(order)] == [("Cumin seeds 1kg", "10"),
                                                                              ("Steel container", "5")]
    _bill_for(db, buying, order, _pline(buying.cumin, 4, 400), _pline(buying.tin, 5, 500))
    assert [(r["description"], r["qty"]) for r in _rows_from_order(order)] == [("Cumin seeds 1kg", "6")]
    _login(client)
    page = client.get(f"/purchases/new?order={order.id}").get_data(as_text=True)
    assert "Cumin seeds 1kg" in page and order.number in page


# --- finding the order -----------------------------------------------------------------------------------

@pytest.mark.parametrize("printed, number, same", [
    ("PO/2026-27/0001", "PO/2026-27/0001", True),
    ("po 2026-27 / 0001", "PO/2026-27/0001", True),
    ("Order No: PO-2026-27-0001", "PO/2026-27/0001", True),
    ("4500012345", "PO/4500012345", True),
    ("004500012345", "4500012345", True),
    ("PO/2026-27/0002", "PO/2026-27/0001", False),
    ("", "PO/2026-27/0001", False),
    ("12", "PO/2026-27/0012", False),                 # a short number alone proves nothing
])
def test_order_numbers_are_compared_after_cleaning(printed, number, same):
    assert matching.numbers_match(printed, number) is same


def test_the_number_on_the_bill_finds_the_order(db, buying):
    right = _order(db, buying, _pline(buying.cumin, 10, 400))
    _order(db, buying, _pline(buying.cumin, 10, 400))                  # an identical twin: only the number tells
    found = matching.candidates(buying.tenant, buying.supplier, right.number.replace("/", "-").lower(),
                                [_row(buying.cumin, 10, 400)], TODAY)
    assert found.proposed.order.id == right.id and found.proposed.by == "number" and found.proposed.clean
    assert "names order" in found.why and len(found.others) == 1


def test_without_a_number_the_lines_find_it_when_one_order_is_clearly_it(db, buying):
    spices = _order(db, buying, _pline(buying.cumin, 10, 400))
    _order(db, buying, _pline(buying.tin, 20, 500))
    found = matching.candidates(buying.tenant, buying.supplier, "", [_row(buying.cumin, 10, 400)], TODAY)
    assert found.proposed.order.id == spices.id and found.proposed.by == "lines" and found.proposed.score == 100
    assert "items, quantities and rates" in found.why


def test_two_orders_that_both_fit_are_offered_and_none_is_chosen(db, buying):
    a = _order(db, buying, _pline(buying.cumin, 10, 400))
    b = _order(db, buying, _pline(buying.cumin, 10, 400))
    found = matching.candidates(buying.tenant, buying.supplier, "", [_row(buying.cumin, 10, 400)], TODAY)
    assert found.proposed is None and {m.order.id for m in found.others} == {a.id, b.id}
    assert "pick one" in found.why


def test_only_that_suppliers_open_orders_are_considered(db, buying):
    theirs = _order(db, buying, _pline(buying.cumin, 10, 400), supplier=buying.other)
    done = _order(db, buying, _pline(buying.cumin, 10, 400))
    _bill_for(db, buying, done, _pline(buying.cumin, 10, 400))           # fully billed: not waiting any more
    found = matching.candidates(buying.tenant, buying.supplier, theirs.number, [_row(buying.cumin, 10, 400)], TODAY)
    assert found.proposed is None and found.others == []
    assert "no open order with that number" in found.why
    assert matching.candidates(buying.tenant, None, "", [], TODAY).proposed is None


def test_a_bill_dated_before_the_order_is_not_its_bill(db, buying):
    order = _order(db, buying, _pline(buying.cumin, 10, 400), on=TODAY)
    early = matching.compare(order, [_row(buying.cumin, 10, 400)], "lines", TODAY - timedelta(days=10))
    assert early.score == 70


# --- what differs, in words --------------------------------------------------------------------------------

def test_differences_are_said_in_words(db, buying):
    order = _order(db, buying, _pline(buying.cumin, 10, 400), _pline(buying.tin, 5, 500))
    m = matching.compare(order, [
        _row(buying.cumin, 12, 430, gst=12),                             # too many, dearer, wrong GST
        _row(None, 1, 250, gst=18, description="Packing charges"),       # not on the order
    ])
    problems = " | ".join(m.problems)
    assert "Cumin seeds 1kg: 12 billed, only 10 still to come on the order." in problems
    assert "Cumin seeds 1kg: billed at Rs. 430.00, ordered at Rs. 400.00." in problems
    assert "Cumin seeds 1kg: GST 12% on the bill, 5% on the order." in problems
    assert "Packing charges is not on the order." in problems
    assert any("Steel container (5 NOS) is on the order but not on this bill" in n for n in m.notes)
    assert not m.clean

    part = matching.compare(order, [_row(buying.cumin, 4, 403)])         # within 1% of the rate
    assert part.clean and "4 of 10 billed now - 6 still to come" in " ".join(part.notes)


def test_units_scanned_in_at_the_warehouse_make_it_a_three_way_check(db, buying):
    from app.models.inventory import LocationKind, StockLocation
    from app.models.warehouse import ReceiptStatus, ReceivedUnit, WarehouseReceipt

    order = _order(db, buying, _pline(buying.cumin, 10, 400))
    godown = StockLocation(tenant_id=buying.tenant.id, name="Godown", kind=LocationKind.GODOWN)
    db.session.add(godown)
    db.session.flush()
    receipt = WarehouseReceipt(tenant_id=buying.tenant.id, purchase_order_id=order.id, location_id=godown.id,
                               status=ReceiptStatus.OPEN, created_by_id=buying.admin.id)
    db.session.add(receipt)
    db.session.flush()
    for n in range(3):
        db.session.add(ReceivedUnit(tenant_id=buying.tenant.id, receipt_id=receipt.id, order_line_id=order.lines[0].id,
                                    product_id=buying.cumin.id, serial_no=f"SN{n}"))
    db.session.commit()
    m = matching.compare(order, [_row(buying.cumin, 5, 400)])
    assert m.pairs[0].received == 3
    assert "5 billed, 3 received at the warehouse so far." in " ".join(m.problems)


# --- the scanned bill on the check screen ----------------------------------------------------------------------

@pytest.fixture()
def scan_buying(app, db, buying):
    from app.models.tenant import Gstin

    gstin = Gstin.query.filter_by(tenant_id=buying.tenant.id).first()
    gstin.gstin, gstin.state_code = OWN, "27"
    for key in ("doc_scan", "expenses"):
        buying.tenant.set_feature(key, True)
    db.session.commit()
    app.config["GEMINI_API_KEY"] = "test-key"
    return buying


def _scanned(client, monkeypatch, **over):
    monkeypatch.setattr(reader, "call_model", FakeModel(_bill(**over)))
    doc_id = _upload(client, _jpeg()).get_json()["id"]
    _read(client, doc_id)
    return doc_id


def test_a_scanned_bill_finds_its_order_by_number_and_settles_it(client, db, scan_buying, monkeypatch):
    b = scan_buying
    order = _order(db, b, _pline(b.cumin, 25, 400), _pline(b.tin, 2, 500), on=ORDERED)
    _login(client)
    doc_id = _scanned(client, monkeypatch, po_number=order.number)
    page = client.get(f"/scan/{doc_id}").get_data(as_text=True)
    panel = page.split('class="card stack po-match"', 1)[1].split("</section>", 1)[0]
    assert order.number in panel and "Matched by the order number on the bill." in panel and "Agrees" in panel
    assert "10 of 25 billed now - 15 still to come" in panel
    assert f'name="purchase_order_id" value="{order.id}"' in panel

    resp = client.post(f"/scan/{doc_id}/post", data=_purchase_form(purchase_order_id=str(order.id),
                                                                    **{"line_product_id[]": [str(b.cumin.id),
                                                                                             str(b.tin.id)]}))
    assert resp.status_code == 302
    db.session.refresh(order)
    by_name = {l.description: l for l in order.lines}
    assert order.status == S.PARTLY and by_name["Cumin seeds 1kg"].billed_qty == 10
    assert by_name["Steel container"].outstanding == 0
    assert order.bills[0].supplier_invoice_no == "TS/26-27/0042"


def test_a_scanned_bill_with_no_number_is_matched_by_its_lines_and_shows_what_differs(client, db, scan_buying,
                                                                                    monkeypatch):
    b = scan_buying
    order = _order(db, b, _pline(b.cumin, 8, 400), _pline(b.tin, 2, 480), on=ORDERED)   # fewer, and cheaper, than billed
    _login(client)
    doc_id = _scanned(client, monkeypatch)
    panel = client.get(f"/scan/{doc_id}").get_data(as_text=True).split('class="card stack po-match"', 1)[1] \
        .split("</section>", 1)[0]
    assert order.number in panel and "Matched by its items, quantities and rates." in panel and "Differs" in panel
    assert "10 billed, only 8 still to come on the order" in panel
    assert "billed at Rs. 500.00, ordered at Rs. 480.00" in panel
    # The owner may post without an order instead.
    assert "Post without an order" in panel
    picked = client.get(f"/scan/{doc_id}?po={order.id}").get_data(as_text=True)
    assert "Picked by you." in picked
    none = client.get(f"/scan/{doc_id}?po=none").get_data(as_text=True)
    assert "You chose to post this bill without an order." in none
    assert 'name="purchase_order_id" value=""' in none


def test_a_bill_cannot_be_tied_to_another_suppliers_order(client, db, scan_buying, monkeypatch):
    b = scan_buying
    theirs = _order(db, b, _pline(b.cumin, 10, 400), supplier=b.other, on=ORDERED)
    _login(client)
    doc_id = _scanned(client, monkeypatch)
    resp = client.post(f"/scan/{doc_id}/post", data=_purchase_form(purchase_order_id=str(theirs.id)))
    assert resp.status_code == 422 and "not this supplier" in resp.get_data(as_text=True)
    db.session.refresh(theirs)
    assert theirs.status == S.OPEN and theirs.bills == []


def test_a_supplier_with_no_order_waiting_posts_as_before(client, db, scan_buying, monkeypatch):
    _login(client)
    doc_id = _scanned(client, monkeypatch)
    page = client.get(f"/scan/{doc_id}").get_data(as_text=True)
    assert "This supplier has no purchase order waiting for a bill." in page
    assert client.post(f"/scan/{doc_id}/post", data=_purchase_form()).status_code == 302
    assert PurchaseOrder.query.count() == 0


def test_one_order_that_only_half_fits_is_offered_not_chosen(db, buying):
    order = _order(db, buying, _pline(buying.cumin, 5, 300), _pline(buying.tin, 2, 500))
    found = matching.candidates(buying.tenant, buying.supplier, "", [_row(buying.cumin, 10, 400), _row(buying.tin, 9, 900)],
                                TODAY)
    assert found.proposed is None and [m.order.id for m in found.others] == [order.id]
    assert "may be this bill's" in found.why
