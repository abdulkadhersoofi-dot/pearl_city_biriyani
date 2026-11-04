"""Teach ARFA (app/scan/lessons.py): the reader reads again when its rows
come short of the bill's taxable value, learns from rows added before
posting, and keeps the owner's (and the Ultra Admin's) notes in mind."""

import pytest

from app.models.scan import LESSON_CORRECTION, ScanLesson
from app.scan import lessons
from app.scan.engine import reader
from tests.test_scan import (  # noqa: F401
    OWN, SELLER, FakeModel, _bill, _jpeg, _login, _purchase_form, _read, _settings, _upload, scanning,
)

SHORT = _bill(line_items=[_bill()["line_items"][0]])          # the Steel container row missed: 4,000 of 5,000


class Recorder(FakeModel):
    def __init__(self, *answers):
        super().__init__(*answers)
        self.prompts = []

    def __call__(self, parts, settings, models, deadline, max_tokens=8192, http=None):
        self.prompts.append(" ".join(p["text"] for p in parts if isinstance(p, dict) and "text" in p))
        return super().__call__(parts, settings, models, deadline, max_tokens, http)


def _ctx(**kw):
    return reader.Context(own_gstins=[OWN], company_name="Acme Traders", **kw)


# --- 1. the sharper second read ---------------------------------------------------------------

def test_rows_short_of_the_taxable_value_are_read_again_and_fixed(monkeypatch):
    model = Recorder(SHORT, _bill())
    monkeypatch.setattr(reader, "call_model", model)
    out = reader.read(_jpeg(), False, _ctx(), _settings())
    assert len(model.calls) == 2
    assert model.calls[1][0] == _settings().strong_model                  # the strong model, on a sharper picture
    assert "Rs 4,000.00" in model.prompts[1] and "Rs 1,000.00 are missing" in model.prompts[1]
    assert [li["description"] for li in out["extracted"]["line_items"]] == ["Cumin seeds 1kg", "Steel container"]
    assert out["engine"]["rows_pass"] == "fixed"
    assert any("Rows re-read" in r for r in out["validation"].get("repairs", []))


def test_a_second_read_no_better_is_not_taken(monkeypatch):
    model = Recorder(SHORT, SHORT)
    monkeypatch.setattr(reader, "call_model", model)
    out = reader.read(_jpeg(), False, _ctx(), _settings())
    assert len(model.calls) == 2 and out["engine"]["rows_pass"] is True
    assert len(out["extracted"]["line_items"]) == 1


def test_a_bill_whose_rows_add_up_is_read_once(monkeypatch):
    model = Recorder(_bill())
    monkeypatch.setattr(reader, "call_model", model)
    out = reader.read(_jpeg(), False, _ctx(), _settings())
    assert len(model.calls) == 1 and not out["engine"].get("rows_pass")


def test_the_gap_and_its_tolerance():
    assert reader.rows_gap(SHORT) == (4000.0, 5000.0, 1000.0) and reader.rows_short(SHORT)
    assert not reader.rows_short(_bill(taxable_value=5010))               # within 0.5%: rounding, not a missed row
    assert not reader.rows_short(_bill(taxable_value=None))               # no figure to check against


# --- 2. lessons: learnt and handed to the reader -----------------------------------------------

def _post_with_the_missed_row(client, monkeypatch):
    monkeypatch.setattr(reader, "call_model", FakeModel(SHORT))
    doc_id = _upload(client, _jpeg()).get_json()["id"]
    _read(client, doc_id)
    res = client.post(f"/scan/{doc_id}/post", data=_purchase_form())     # both rows, as on the bill
    assert res.status_code == 302, res.data[:400]
    return doc_id


def test_rows_added_before_posting_become_a_lesson(client, db, scanning, client_admin, monkeypatch):
    _login(client)
    doc_id = _post_with_the_missed_row(client, monkeypatch)
    lesson = ScanLesson.query.one()
    assert lesson.kind == LESSON_CORRECTION and lesson.tenant_id == scanning.id and lesson.supplier_gstin == SELLER
    assert lesson.source_doc_id == doc_id and lesson.supplier_name == "Tumkur Spices Pvt Ltd"
    ex = lesson.example
    assert [r["description"] for r in ex["rows"]] == ["Steel container"] and ex["rows"][0]["value"] == 1000
    assert ex["read_rows"] == 1 and ex["posted_rows"] == 2 and ex["invoice_number"] == "TS/26-27/0042"


def test_a_bill_posted_as_read_teaches_nothing(client, db, scanning, client_admin, monkeypatch):
    _login(client)
    monkeypatch.setattr(reader, "call_model", FakeModel(_bill()))
    doc_id = _upload(client, _jpeg()).get_json()["id"]
    _read(client, doc_id)
    assert client.post(f"/scan/{doc_id}/post", data=_purchase_form()).status_code == 302
    assert ScanLesson.query.count() == 0


def test_the_next_bill_from_that_supplier_is_read_with_the_lesson(client, db, scanning, client_admin, monkeypatch):
    _login(client)
    _post_with_the_missed_row(client, monkeypatch)
    texts, ids = lessons.for_reading(scanning)
    assert SELLER in texts and '"Steel container" HSN 7323 2 x 500.00 value 1,000.00' in texts[SELLER]
    model = Recorder(_bill(invoice_number="TS/26-27/0050", line_items=[_bill()["line_items"][0]]),
                     _bill(invoice_number="TS/26-27/0050"))
    monkeypatch.setattr(reader, "call_model", model)
    doc_id = _upload(client, _jpeg("ANOTHER BILL")).get_json()["id"]
    _read(client, doc_id)
    assert "WHAT THIS BUSINESS HAS TAUGHT YOU" in model.prompts[1] and "Steel container" in model.prompts[1]
    assert ScanLesson.query.one().uses == 1


def test_notes_for_every_bill_go_with_the_first_read(client, db, scanning, client_admin, monkeypatch):
    lessons.add_note(scanning.id, client_admin, "Freight and packing are separate rows - always include them.")
    db.session.commit()
    model = Recorder(_bill())
    monkeypatch.setattr(reader, "call_model", model)
    from app.scan.services import _context

    reader.read(_jpeg(), False, _context(scanning), _settings())
    assert "About every bill: Freight and packing" in model.prompts[0]


# --- 3. the Teach ARFA page --------------------------------------------------------------------------

def test_the_page_lists_suppliers_and_takes_notes(client, db, scanning, client_admin, monkeypatch):
    _login(client)
    _post_with_the_missed_row(client, monkeypatch)
    page = client.get("/scan/teach").get_data(as_text=True)
    assert "Tumkur Spices Pvt Ltd" in page and SELLER in page and "Every supplier" in page
    page = client.get(f"/scan/teach?supplier={SELLER}").get_data(as_text=True)
    assert "Learnt from a bill" in page and "Steel container" in page
    res = client.post("/scan/teach/note", data={"text": "Rows continue on page 2 without a heading.",
                                                "gstin": SELLER, "name": "Tumkur Spices Pvt Ltd", "back": SELLER})
    assert res.status_code == 302 and SELLER in res.headers["Location"]
    texts, _ = lessons.for_reading(scanning)
    assert "About bills from this supplier: Rows continue on page 2" in texts[SELLER]
    assert client.post("/scan/teach/note", data={"text": "", "gstin": ""}, follow_redirects=True).status_code == 200
    assert "Write what ARFA should know" in client.post("/scan/teach/note", data={"text": " "},
                                                        follow_redirects=True).get_data(as_text=True)
    assert "GSTIN format" in client.post("/scan/teach/note", data={"text": "x", "gstin": "NOTAGSTIN"},
                                         follow_redirects=True).get_data(as_text=True)


def test_switching_off_and_deleting(client, db, scanning, client_admin):
    _login(client)
    note = lessons.add_note(scanning.id, client_admin, "Read the second table too.", SELLER)
    db.session.commit()
    client.post(f"/scan/teach/{note.id}/switch")
    assert SELLER not in lessons.for_reading(scanning)[0]
    client.post(f"/scan/teach/{note.id}/switch")
    assert SELLER in lessons.for_reading(scanning)[0]
    client.post(f"/scan/teach/{note.id}/delete")
    assert ScanLesson.query.count() == 0


def test_lessons_stay_with_their_business(client, db, scanning, client_admin, staff, super_admin):
    from app.models.tenant import BillingCycle, RegistrationType, Tenant

    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
                   billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.flush()
    theirs = lessons.add_note(other.id, super_admin, "Their note.")
    db.session.commit()
    assert lessons.for_reading(scanning)[0] == {}
    _login(client)
    assert client.post(f"/scan/teach/{theirs.id}/switch").status_code == 404
    assert client.post(f"/scan/teach/{theirs.id}/delete").status_code == 404
    client.get("/auth/logout")
    _login(client, staff.email, "CashierSecret123")
    assert client.get("/scan/teach").status_code == 403
    assert client.post("/scan/teach/note", data={"text": "x"}).status_code == 403


def test_the_ultra_admin_teaches_every_business(client, db, scanning, client_admin, super_admin):
    _login(client, super_admin.email, "SuperSecret123")
    page = client.get("/scan/teach/all")
    assert page.status_code == 200 and "Notes for every business" in page.get_data(as_text=True)
    client.post("/scan/teach/note", data={"text": "A 'Less: Scheme discount' line is a negative row."})
    platform = ScanLesson.query.one()
    assert platform.tenant_id is None
    assert "Scheme discount" in lessons.for_reading(scanning)[0][""]
    client.get("/auth/logout")
    _login(client)
    assert "From ARFA" in client.get("/scan/teach").get_data(as_text=True)
    assert client.post(f"/scan/teach/{platform.id}/delete").status_code == 404   # the business can't remove it
    assert client.get("/scan/teach/all").status_code == 403


@pytest.mark.parametrize("read,posted,missed", [
    ([{"description": "Cumin seeds 1kg", "value": 4000}], [{"description": "CUMIN SEEDS 1 KG", "value": 4000}], 0),
    ([{"description": "Cumin", "value": 4000}], [{"description": "Jeera", "value": 4000}], 0),     # same value
    ([{"description": "Cumin", "value": 4000}], [{"description": "Cumin", "value": 4000},
                                                 {"description": "Freight", "value": 350}], 1),
    ([], [{"description": "Freight", "value": 350}], 1),
])
def test_matching_rows(read, posted, missed):
    assert len(lessons.missed_rows(read, posted)) == missed
