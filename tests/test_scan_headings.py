"""Bill reader training: headings (app/scan/lessons.py, reader.apply_headings).
A supplier's heading is taught once ("Cust. PO" means your order number);
the reader is told, and the same rule is applied to what it returned."""

import pytest

from app.models.audit_log import AuditLog
from app.models.scan import LESSON_HEADING, ScanDocument, ScanLesson
from app.scan import lessons, services
from app.scan.engine import reader
from tests.test_scan import (  # noqa: F401
    OWN, SELLER, FakeModel, _bill, _jpeg, _login, _purchase_form, _read, _settings, _upload, scanning,
)
from tests.test_scan_lessons import Recorder

# The reader put the buyer's order number under the supplier's own order
# number, and said which label it read it from.
MISFILED = _bill(order_number="4500012345", headings={"order_number": "Cust. PO"})


# --- the rules, on what the reader returned ------------------------------------------------------------

def test_a_value_under_the_wrong_field_moves_to_the_taught_one():
    ex = {"order_number": "4500012345", "po_number": None, "headings": {"order_number": "Cust. PO"},
          "boxes": {"order_number": [0, 1, 2, 3, 4]}}
    notes = reader.apply_headings(ex, [("CUST PO :", "po_number")])
    assert ex["po_number"] == "4500012345" and ex["order_number"] is None
    assert ex["boxes"] == {"po_number": [0, 1, 2, 3, 4]} and ex["headings"] == {"po_number": "Cust. PO"}
    assert notes == ["'CUST PO :' is po number on this supplier's bills (taught): moved from order number."]


def test_a_label_left_in_other_references_is_taken():
    ex = {"other_references": "Indent: 77; Our Ref: X9", "supplier_reference": None}
    reader.apply_headings(ex, [("Our Ref.", "supplier_reference")])
    assert ex["supplier_reference"] == "X9" and ex["other_references"] == "Indent: 77"
    # A field already filled is not overwritten from there.
    ex = {"other_references": "Our Ref: X9", "supplier_reference": "ABC"}
    assert reader.apply_headings(ex, [("Our Ref.", "supplier_reference")]) == []
    assert ex["supplier_reference"] == "ABC"


@pytest.mark.parametrize("order", [("taxable_value", "amount"), ("amount", "taxable_value")])
def test_columns_read_under_the_wrong_names_are_put_right_in_any_order(order):
    said = {"taxable_value": "Basic", "amount": "Net Amt"}
    ex = {"line_items": [{"quantity": 2, "rate_per_unit": 0, "taxable_value": 500, "amount": 1180}],
          "column_headings": {k: said[k] for k in order}}
    reader.apply_headings(ex, [("Basic", "col:rate_per_unit"), ("Net Amt", "col:taxable_value")])
    assert ex["line_items"] == [{"quantity": 2, "rate_per_unit": 500, "taxable_value": 1180, "amount": 0}]


def test_two_columns_crossed_swap_back():
    ex = {"line_items": [{"quantity": 400, "rate_per_unit": 10}],
          "column_headings": {"quantity": "Price", "rate_per_unit": "Nos"}}
    notes = reader.apply_headings(ex, [("Price", "col:rate_per_unit"), ("Nos", "col:quantity")])
    assert ex["line_items"] == [{"quantity": 10, "rate_per_unit": 400}] and len(notes) == 1 and "swapped" in notes[0]


def test_nothing_is_moved_without_a_rule_or_across_kinds():
    ex = {"po_number": "1", "headings": {"po_number": "PO No"}, "invoice_value": 500.0}
    assert reader.apply_headings(ex, []) == []
    assert reader.apply_headings(ex, [("PO No", "po_number")]) == []            # already where it belongs
    assert reader.apply_headings(ex, [("PO No", "invoice_value")]) == []        # text never becomes money
    assert ex["po_number"] == "1" and ex["invoice_value"] == 500.0


def test_a_suppliers_own_rule_comes_before_every_suppliers():
    ctx = reader.Context(own_gstins=[OWN], headings={SELLER: [("Ref", "po_number")],
                                                     "": [("ref.", "supplier_reference"), ("Doc No", "invoice_number")]})
    assert reader.headings_for(ctx, [SELLER]) == [("Ref", "po_number"), ("Doc No", "invoice_number")]
    assert reader.headings_for(ctx, ["29ZZZZZ0000Z1Z5"]) == [("ref.", "supplier_reference"),
                                                              ("Doc No", "invoice_number")]


# --- teaching and reading ------------------------------------------------------------------------------

def test_a_taught_heading_is_told_to_the_reader_and_applied(client, db, scanning, client_admin, monkeypatch):
    lessons.add_heading(scanning.id, client_admin, "Cust. PO", "po_number", SELLER, "Tumkur Spices Pvt Ltd")
    lessons.add_heading(scanning.id, client_admin, "Basic", "col:rate_per_unit")
    db.session.commit()
    texts, ids = lessons.for_reading(scanning)
    assert 'the heading or label "Cust. PO" means po_number (Your order (PO) number)' in texts[SELLER]
    assert 'column headed "Basic" is rate_per_unit (Items: rate)' in texts[""]
    assert '"headings":{' in texts[SELLER] and len(ids[SELLER]) == 1
    assert lessons.heading_rules(scanning) == {SELLER: [("Cust. PO", "po_number")],
                                               "": [("Basic", "col:rate_per_unit")]}

    _login(client)
    model = Recorder(MISFILED)
    monkeypatch.setattr(reader, "call_model", model)
    doc_id = _upload(client, _jpeg()).get_json()["id"]
    _read(client, doc_id)
    doc = db.session.get(ScanDocument, doc_id)
    assert doc.extracted["po_number"] == "4500012345" and not doc.extracted["order_number"]
    assert any("(taught)" in r for r in doc.validation["repairs"])
    assert "Basic" in model.prompts[0]                       # every supplier's heading goes with the first read
    page = client.get(f"/scan/{doc_id}").get_data(as_text=True)
    assert 'value="4500012345"' in page


def test_without_heading_lessons_the_reader_is_asked_nothing_new(client, db, scanning, client_admin, monkeypatch):
    model = Recorder(_bill())
    monkeypatch.setattr(reader, "call_model", model)
    result = reader.read(_jpeg(), False, services._context(scanning), _settings())
    assert "column_headings" not in model.prompts[0] and "headings" not in result["extracted"]


def test_a_heading_means_one_thing_for_a_supplier(db, scanning, client_admin):
    first = lessons.add_heading(scanning.id, client_admin, "Our Ref", "supplier_reference", SELLER)
    db.session.commit()
    again = lessons.add_heading(scanning.id, client_admin, "OUR REF.:", "po_number", SELLER)
    db.session.commit()
    assert again.id == first.id and ScanLesson.query.count() == 1 and again.example == {"field": "po_number"}
    lessons.add_heading(scanning.id, client_admin, "Our Ref", "supplier_reference")     # every supplier: its own
    db.session.commit()
    assert ScanLesson.query.count() == 2
    for heading, field, gstin, message in [("", "po_number", "", "Type the heading"), ("PO", "nonsense", "", "Pick what"),
                                           ("x" * 61, "po_number", "", "60 characters"), ("#%", "po_number", "", "letters"),
                                           ("PO", "po_number", "NOTAGSTIN", "GSTIN format")]:
        with pytest.raises(lessons.LessonError, match=message):
            lessons.add_heading(scanning.id, client_admin, heading, field, gstin)


def test_a_switched_off_heading_is_not_used(db, scanning, client_admin):
    lesson = lessons.add_heading(scanning.id, client_admin, "Cust. PO", "po_number", SELLER)
    db.session.commit()
    lesson.active = False
    db.session.commit()
    assert lessons.heading_rules(scanning) == {} and SELLER not in lessons.for_reading(scanning)[0]


# --- the training screens ------------------------------------------------------------------------------

def _posted_bill(client, monkeypatch, bill=None):
    monkeypatch.setattr(reader, "call_model", FakeModel(bill or _bill()))
    doc_id = _upload(client, _jpeg()).get_json()["id"]
    _read(client, doc_id)
    assert client.post(f"/scan/{doc_id}/post", data=_purchase_form()).status_code == 302
    return doc_id


def test_the_training_page_teaches_a_heading(client, db, scanning, client_admin, monkeypatch):
    _login(client)
    doc_id = _posted_bill(client, monkeypatch)
    page = client.get("/scan/teach").get_data(as_text=True)
    assert "Bill reader training" in page and "Right first time" in page and "1 of 1" in page
    page = client.get(f"/scan/teach?supplier={SELLER}").get_data(as_text=True)
    assert "Train on a bill" in page and f"/scan/teach/bill/{doc_id}" in page and "Your order (PO) number" in page
    res = client.post("/scan/teach/heading", data={"heading": "Cust. PO", "field": "po_number", "gstin": SELLER,
                                                   "name": "Tumkur Spices Pvt Ltd", "back": SELLER})
    assert res.status_code == 302 and SELLER in res.headers["Location"]
    lesson = ScanLesson.query.filter_by(kind=LESSON_HEADING).one()
    assert (lesson.text, lesson.example, lesson.supplier_gstin, lesson.tenant_id) == \
        ("Cust. PO", {"field": "po_number"}, SELLER, scanning.id)
    page = client.get(f"/scan/teach?supplier={SELLER}").get_data(as_text=True)
    assert "Cust. PO</strong> means <strong>Your order (PO) number" in page
    assert "Pick what the heading means" in client.post(
        "/scan/teach/heading", data={"heading": "X", "field": ""}, follow_redirects=True).get_data(as_text=True)
    client.post(f"/scan/teach/{lesson.id}/delete", data={"back": SELLER})
    assert ScanLesson.query.filter_by(kind=LESSON_HEADING).count() == 0


def test_training_on_a_bill_click_teach_and_read_again(client, db, scanning, client_admin, monkeypatch):
    _login(client)
    doc_id = _posted_bill(client, monkeypatch, MISFILED)
    # A bill still to be checked can be trained on too, and names its supplier on the training page.
    monkeypatch.setattr(reader, "call_model", FakeModel(_bill(invoice_number="TS/26-27/0099")))
    waiting = _upload(client, _jpeg("NEXT BILL")).get_json()["id"]
    _read(client, waiting)
    assert f"/scan/teach/bill/{waiting}" in client.get(f"/scan/teach?supplier={SELLER}").get_data(as_text=True)
    page = client.get(f"/scan/teach/bill/{doc_id}").get_data(as_text=True)
    assert "Train on this bill" in page and "Supplier&#39;s own order number" in page and "4500012345" in page
    assert f"/scan/{doc_id}/page/" in page and "scan_teach.js" in page

    # Clicking the heading on the picture: the reader says what is printed there.
    seen = {}

    def label_model(parts, settings, models, deadline, max_tokens=8192, http=None):
        seen["prompt"] = parts[-1]["text"]
        return '{"text": "Cust. PO :"}', models[0]

    monkeypatch.setattr(reader, "call_model", label_model)
    res = client.post(f"/scan/teach/bill/{doc_id}/label", json={"n": 0, "x": 0.7, "y": 0.2})
    assert res.status_code == 200 and res.get_json() == {"text": "Cust. PO"}
    assert "the point a person clicked" in seen["prompt"]
    assert client.post(f"/scan/teach/bill/{doc_id}/label", json={"n": 0, "x": "left", "y": 0}).status_code == 400

    res = client.post("/scan/teach/heading", data={"heading": "Cust. PO", "field": "po_number", "gstin": SELLER,
                                                   "name": "Tumkur Spices Pvt Ltd", "doc": str(doc_id)})
    assert res.status_code == 302 and res.headers["Location"].endswith(f"/scan/teach/bill/{doc_id}")
    assert ScanLesson.query.filter_by(kind=LESSON_HEADING).one().source_doc_id == doc_id

    # Read again: before and after, and the posted bill itself untouched.
    monkeypatch.setattr(reader, "call_model", FakeModel(MISFILED))
    page = client.post(f"/scan/teach/bill/{doc_id}/read-again").get_data(as_text=True)
    assert "Before and after" in page and "The reading changed" in page and "(taught)" in page
    compare = page.split("<th>Before</th><th>After</th>", 1)[1]
    row = compare.split("Your order (PO) number</td>", 1)[1].split("</tr>", 1)[0]
    assert row == "<td>-</td><td>4500012345</td>"
    assert 'class="is-changed"><td>Supplier&#39;s own order number</td><td>4500012345</td><td>-</td>' in compare
    doc = db.session.get(ScanDocument, doc_id)
    assert doc.status == "posted" and doc.extracted["order_number"] == "4500012345" and not doc.extracted["po_number"]
    assert AuditLog.query.filter_by(action="scan_trial_read").count() == 1
    assert AuditLog.query.filter_by(action="scan_heading_read").count() == 1


def test_training_reads_have_a_daily_limit(client, db, scanning, client_admin, monkeypatch):
    _login(client)
    doc_id = _posted_bill(client, monkeypatch)
    monkeypatch.setattr(services, "TRIAL_READS_A_DAY", 1)
    page = client.post(f"/scan/teach/bill/{doc_id}/read-again").get_data(as_text=True)
    assert "Nothing changed" in page
    page = client.post(f"/scan/teach/bill/{doc_id}/read-again", follow_redirects=True).get_data(as_text=True)
    assert "training reads are used up" in page
    res = client.post(f"/scan/teach/bill/{doc_id}/label", json={"n": 0, "x": 0.5, "y": 0.5})
    assert res.status_code == 422 and "used up" in res.get_json()["error"]


def test_training_stays_with_its_business(client, db, scanning, client_admin, staff, super_admin, monkeypatch):
    _login(client)
    doc_id = _posted_bill(client, monkeypatch)
    client.get("/auth/logout")
    _login(client, staff.email, "CashierSecret123")
    assert client.get(f"/scan/teach/bill/{doc_id}").status_code == 403
    assert client.post("/scan/teach/heading", data={"heading": "PO", "field": "po_number"}).status_code == 403
    client.get("/auth/logout")
    # The Ultra Admin teaches a heading for every business.
    _login(client, super_admin.email, "SuperSecret123")
    client.post("/scan/teach/heading", data={"heading": "Buyer's Order No.", "field": "po_number"})
    platform = ScanLesson.query.one()
    assert platform.tenant_id is None and platform.kind == LESSON_HEADING
    assert lessons.heading_rules(scanning) == {"": [("Buyer's Order No.", "po_number")]}
    assert "Buyer&#39;s Order No." in client.get("/scan/teach/all").get_data(as_text=True)
