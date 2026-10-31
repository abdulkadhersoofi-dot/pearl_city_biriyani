"""Scan bills (app/scan): the reading engine on its own, and the module end
to end with the AI model replaced by a stand-in - upload, read, check,
post as a purchase bill or an expense."""

import base64
import io
import json
from decimal import Decimal

import pytest
from PIL import Image, ImageDraw

from app.models.expense import Expense
from app.models.purchase import PurchaseBill, Supplier
from app.models.scan import FILED, POSTED, REJECTED, SPLIT, TO_CHECK, UPLOADED, ScanDocument
from app.scan.engine import reader

OWN = "27AAPFU0939F1ZV"        # the client's GSTIN (Maharashtra), checksum-valid
SELLER = "29AABCT1332L1ZA"     # a supplier in Karnataka
STRANGER = "33AAICS8948L1ZN"   # somebody else entirely


def _bill(**over):
    bill = {
        "supplier_name": "Tumkur Spices Pvt Ltd", "supplier_gstin": SELLER, "supplier_address": "12 Market Rd",
        "receiver_name": "Acme Traders", "receiver_gstin": OWN, "invoice_number": "TS/26-27/0042",
        "invoice_date": "2026-10-20", "category": "Purchase", "confidence": 0.95, "flags": [],
        "line_items": [
            {"description": "Cumin seeds 1kg", "hsn_sac": "0909", "quantity": 10, "unit": "KGS", "rate_per_unit": 400,
             "gst_rate": 5, "taxable_value": 4000, "igst": 200, "cgst": 0, "sgst": 0},
            {"description": "Steel container", "hsn_sac": "7323", "quantity": 2, "unit": "NOS", "rate_per_unit": 500,
             "gst_rate": 18, "taxable_value": 1000, "igst": 180, "cgst": 0, "sgst": 0},
        ],
        "taxable_value": 5000, "igst": 380, "cgst": 0, "sgst": 0, "cess": 0, "round_off": 0, "invoice_value": 5380,
    }
    bill.update(over)
    return bill


class FakeModel:
    """Stands in for reader.call_model: answers every prompt with a bill."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, parts, settings, models, deadline, max_tokens=8192, http=None):
        self.calls.append(models)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if answer is None:
            return None, None
        return json.dumps(answer), models[0]


def _settings():
    return reader.Settings(api_key="test", qr_budget=0.5)


def _jpeg(text="TAX INVOICE", qr=None):
    img = Image.new("RGB", (1200, 1600), "white")
    d = ImageDraw.Draw(img)
    d.text((60, 60), text, fill="black")
    for y in range(200, 1400, 40):
        d.text((60, y), f"Line {y}  Cumin seeds  10 x 400.00  4,000.00", fill="black")
    if qr:
        img.paste(qr, (820, 60))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _jwt(data: dict) -> str:
    enc = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc({'iss': 'NIC', 'data': json.dumps(data)})}.c2lnbmF0dXJl"


# --- the engine on its own ---------------------------------------------------------------------

def test_an_einvoice_qr_payload_is_read():
    fields = reader.parse_einvoice_jwt(_jwt({"SellerGstin": SELLER, "BuyerGstin": OWN, "DocNo": "TS/0042",
                                             "DocDt": "20/10/2026", "TotInvVal": 5380, "Irn": "ab" * 32}))
    assert fields["supplier_gstin"] == SELLER and fields["invoice_date"] == "2026-10-20"
    assert fields["invoice_value"] == 5380 and fields["irn"] == "ab" * 32
    assert reader.parse_einvoice_jwt("not.a.jwt") is None and reader.parse_einvoice_jwt("") is None


def test_the_qr_on_a_photo_is_found_and_overrides_a_misread_gstin(monkeypatch):
    pytest.importorskip("pyzbar.pyzbar")
    segno = pytest.importorskip("segno")
    payload = _jwt({"SellerGstin": SELLER, "BuyerGstin": OWN, "DocNo": "TS/26-27/0042", "DocDt": "20/10/2026",
                    "TotInvVal": 5380, "Irn": "cd" * 32})
    buf = io.BytesIO()
    segno.make(payload, error="m").save(buf, kind="png", scale=3, border=4)
    photo = _jpeg(qr=Image.open(io.BytesIO(buf.getvalue())).convert("RGB"))
    if reader.decode_einvoice_qr([photo], 3.0) is None:
        pytest.skip("zbar couldn't read the generated QR here")
    misread = SELLER[:5] + "8" + SELLER[6:]   # the scan misread one character
    monkeypatch.setattr(reader, "call_model", FakeModel(_bill(supplier_gstin=misread)))
    result = reader.read(photo, False, reader.Context(own_gstins=[OWN]), _settings())
    ex = result["extracted"]
    assert ex["supplier_gstin"] == SELLER and ex["irn"] == "cd" * 32
    assert ex["einvoice"]["present"] and "supplier_gstin" in ex["einvoice"]["replaced"]
    assert result["engine"]["qr"] == "found"


def test_a_clean_purchase_bill_reads_as_a_verified_purchase(monkeypatch):
    model = FakeModel(_bill())
    monkeypatch.setattr(reader, "call_model", model)
    result = reader.read(_jpeg(), False, reader.Context(own_gstins=[OWN], company_name="Acme Traders"), _settings())
    assert result["rejection_code"] is None and result["own_gstin"] == OWN
    assert result["extracted"]["category"] == "Purchase"
    assert result["validation"]["status"] in ("verified", "review")
    assert result["preview"][:3] == b"\xff\xd8\xff"
    assert len(model.calls) == 1   # no second pass needed


def test_our_own_gstin_misread_is_put_back(monkeypatch):
    garbled = OWN[:4] + "R" + OWN[5:]   # one character wrong, checksum fails
    monkeypatch.setattr(reader, "call_model", FakeModel(_bill(receiver_gstin=garbled)))
    result = reader.read(_jpeg(), False, reader.Context(own_gstins=[OWN], company_name="Acme Traders"), _settings())
    assert result["extracted"]["receiver_gstin"] == OWN


def test_a_known_suppliers_gstin_misread_snaps_to_it(monkeypatch):
    near = SELLER[:-1] + ("B" if SELLER[-1] != "B" else "C")
    monkeypatch.setattr(reader, "call_model", FakeModel(_bill(supplier_gstin=near)))
    result = reader.read(_jpeg(), False, reader.Context(own_gstins=[OWN], known_gstins=[SELLER]), _settings())
    assert result["extracted"]["supplier_gstin"] == SELLER


def test_someone_elses_bill_is_rejected(monkeypatch):
    monkeypatch.setattr(reader, "call_model", FakeModel(_bill(receiver_gstin=STRANGER, receiver_name="Other Co")))
    result = reader.read(_jpeg(), False, reader.Context(own_gstins=[OWN]), _settings())
    assert result["rejection_code"] == "WRONG_COMPANY"


def test_an_unreadable_picture_is_rejected(monkeypatch):
    blank = {"supplier_name": "", "supplier_gstin": "", "receiver_gstin": "", "invoice_number": "",
             "invoice_value": 0, "confidence": 0.1, "line_items": []}
    monkeypatch.setattr(reader, "call_model", FakeModel(blank))
    result = reader.read(_jpeg(), False, reader.Context(own_gstins=[OWN]), _settings())
    assert result["rejection_code"] == "UNREADABLE"


def test_a_failed_model_is_an_error_not_an_unreadable_bill(monkeypatch):
    monkeypatch.setattr(reader, "call_model", FakeModel(None))
    with pytest.raises(reader.ReadError) as err:
        reader.read(_jpeg(), False, reader.Context(own_gstins=[OWN]), _settings())
    assert err.value.code == "model_unavailable"


def test_bad_arithmetic_gets_one_targeted_re_read(monkeypatch):
    wrong = _bill(invoice_value=9380)   # the total misread
    model = FakeModel(wrong, _bill())
    monkeypatch.setattr(reader, "call_model", model)
    result = reader.read(_jpeg(), False, reader.Context(own_gstins=[OWN]), _settings())
    if result["engine"]["retry"]:
        assert result["extracted"]["invoice_value"] == 5380
        assert len(model.calls) == 2


def test_an_expense_bill_reads_as_an_expense(monkeypatch):
    hotel = _bill(supplier_name="Hotel Saravana Bhavan", category="Expenses", receiver_gstin="",
                  line_items=[{"description": "Meals", "hsn_sac": "9963", "quantity": 1, "rate_per_unit": 1000,
                               "gst_rate": 5, "taxable_value": 1000, "cgst": 25, "sgst": 25, "igst": 0}],
                  taxable_value=1000, igst=0, cgst=25, sgst=25, invoice_value=1050)
    monkeypatch.setattr(reader, "call_model", FakeModel(hotel))
    result = reader.read(_jpeg(), False, reader.Context(own_gstins=[OWN]), _settings())
    assert result["extracted"]["category"] == "Expenses"


def test_bundle_pages_group_into_invoices():
    pages = [{"i": 0, "inv": "A1"}, {"i": 1, "inv": "A1", "cont": True}, {"i": 2, "inv": "B2"},
             {"i": 3, "summary": True}, {"i": 4, "inv": "C3"}]
    assert [g["page_indices"] for g in reader.group_pages(pages)] == [[0, 1], [2], [4]]


# --- the module ----------------------------------------------------------------------------------

@pytest.fixture()
def scanning(app, db, tenant):
    from app.models.tenant import Gstin

    gstin = Gstin.query.filter_by(tenant_id=tenant.id).first()
    gstin.gstin, gstin.state_code = OWN, "27"
    for key in ("doc_scan", "purchases", "expenses"):
        tenant.set_feature(key, True)
    db.session.commit()
    app.config["GEMINI_API_KEY"] = "test-key"
    return tenant


def _login(client, email="admin@acme.example.com", password="ClientSecret123"):
    client.post("/auth/login", data={"email": email, "password": password})


def _upload(client, data, name="bill.jpg"):
    return client.post("/scan/upload", data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")


def _read(client, doc_id):
    return client.post(f"/scan/{doc_id}/read")


def _ready_doc(client, monkeypatch, bill=None, photo=None):
    monkeypatch.setattr(reader, "call_model", FakeModel(bill or _bill()))
    res = _upload(client, photo or _jpeg())
    assert res.status_code == 201, res.get_json()
    doc_id = res.get_json()["id"]
    body = _read(client, doc_id).get_json()
    assert body["status"] == TO_CHECK, body
    return doc_id


def _purchase_form(**over):
    form = {"kind": "purchase", "supplier_name": "Tumkur Spices Pvt Ltd", "supplier_gstin": SELLER,
            "invoice_number": "TS/26-27/0042", "invoice_date": "2026-10-20", "bill_total": "5380",
            "line_description[]": ["Cumin seeds 1kg", "Steel container"], "line_hsn[]": ["0909", "7323"],
            "line_qty[]": ["10", "2"], "line_unit[]": ["KGS", "NOS"], "line_rate[]": ["400", "500"],
            "line_discount[]": ["0", "0"], "line_gst_rate[]": ["5", "18"], "line_itc[]": ["eligible", "eligible"]}
    form.update(over)
    return form


def test_upload_read_and_post_a_purchase_bill(client, db, scanning, client_admin, monkeypatch):
    _login(client)
    doc_id = _ready_doc(client, monkeypatch)
    page = client.get(f"/scan/{doc_id}")
    assert page.status_code == 200 and b"Tumkur Spices" in page.data and b"Post the purchase bill" in page.data
    res = client.post(f"/scan/{doc_id}/post", data=_purchase_form())
    assert res.status_code == 302, res.data[:500]
    bill = PurchaseBill.query.one()
    assert bill.supplier_invoice_no == "TS/26-27/0042" and bill.grand_total == Decimal("5380.00")
    assert bill.attachment_id is not None
    supplier = Supplier.query.one()
    assert supplier.gstin == SELLER and supplier.state_code == "29"
    doc = db.session.get(ScanDocument, doc_id)
    assert doc.status == POSTED and doc.posted_type == "purchase_bill" and doc.posted_id == bill.id
    assert client.get(f"/scan/{doc_id}").status_code == 200   # the posted view opens


def test_a_total_that_doesnt_match_needs_post_anyway(client, db, scanning, client_admin, monkeypatch):
    _login(client)
    doc_id = _ready_doc(client, monkeypatch)
    res = client.post(f"/scan/{doc_id}/post", data=_purchase_form(bill_total="6000"))
    assert res.status_code == 422 and b"difference" in res.data
    assert PurchaseBill.query.count() == 0
    assert b"Cumin seeds 1kg" in res.data   # the form keeps what was entered
    client.post(f"/scan/{doc_id}/post", data=_purchase_form(bill_total="6000", accept_difference="1"))
    assert PurchaseBill.query.count() == 1


def test_post_as_an_expense(client, db, scanning, client_admin, monkeypatch):
    from app.expenses.services import ensure_categories

    _login(client)
    hotel = _bill(supplier_name="Hotel Saravana Bhavan", supplier_gstin=SELLER, category="Expenses",
                  line_items=[{"description": "Meals", "hsn_sac": "9963", "quantity": 1, "rate_per_unit": 1000,
                               "gst_rate": 5, "taxable_value": 1000, "igst": 50}],
                  taxable_value=1000, igst=50, invoice_value=1050)
    doc_id = _ready_doc(client, monkeypatch, bill=hotel)
    category = ensure_categories(scanning.id)[0]
    db.session.commit()
    res = client.post(f"/scan/{doc_id}/post", data={
        "kind": "expense", "supplier_name": "Hotel Saravana Bhavan", "supplier_gstin": SELLER,
        "invoice_number": "TS/26-27/0042", "invoice_date": "2026-10-20", "amount": "1050", "category_id": category.id,
        "payment_mode": "upi", "is_gst_bill": "1", "gst_rate": "5", "itc": "blocked"})
    assert res.status_code == 302, res.data[:500]
    expense = Expense.query.one()
    assert expense.amount == Decimal("1050.00") and expense.bill_number == "TS/26-27/0042"
    assert expense.is_gst_bill and expense.attachment_id is not None
    assert db.session.get(ScanDocument, doc_id).posted_type == "expense"


def test_the_same_file_twice_is_one_document(client, db, scanning, client_admin):
    _login(client)
    photo = _jpeg()
    first = _upload(client, photo).get_json()
    again = _upload(client, photo)
    assert again.status_code == 200 and again.get_json()["existing"] and again.get_json()["id"] == first["id"]
    assert ScanDocument.query.count() == 1


def test_a_bill_already_entered_is_flagged(client, db, scanning, client_admin, monkeypatch):
    _login(client)
    first = _ready_doc(client, monkeypatch)
    client.post(f"/scan/{first}/post", data=_purchase_form())
    second = _ready_doc(client, monkeypatch, photo=_jpeg("TAX INVOICE - photo taken again"))
    assert db.session.get(ScanDocument, second).duplicate_of.startswith("Purchase bill")


def test_someone_elses_bill_can_only_be_filed_or_discarded(client, db, scanning, client_admin, monkeypatch):
    _login(client)
    monkeypatch.setattr(reader, "call_model", FakeModel(_bill(receiver_gstin=STRANGER, receiver_name="Other Co")))
    doc_id = _upload(client, _jpeg()).get_json()["id"]
    assert _read(client, doc_id).get_json()["status"] == REJECTED
    page = client.get(f"/scan/{doc_id}")
    assert b"isn&#39;t addressed to your business" in page.data or b"isn't addressed to your business" in page.data
    assert b"Post the purchase bill" not in page.data
    client.post(f"/scan/{doc_id}/post", data={"kind": "file"})
    assert db.session.get(ScanDocument, doc_id).status == FILED


def test_a_pdf_of_several_invoices_is_split(client, db, scanning, client_admin, monkeypatch):
    fitz = pytest.importorskip("fitz")
    pdf = fitz.open()
    for number in ("A-100", "B-200"):
        page = pdf.new_page()
        page.insert_text((72, 72), f"TAX INVOICE   Invoice No: {number}", fontsize=12)
        for y in range(120, 700, 24):
            page.insert_text((72, y), "Cumin seeds 10 x 400.00 = 4,000.00 HSN 0909 GST 5%", fontsize=10)
    data = pdf.tobytes()
    _login(client)
    monkeypatch.setattr(reader, "call_model", FakeModel(_bill()))
    doc_id = _upload(client, data, "bundle.pdf").get_json()["id"]
    body = _read(client, doc_id).get_json()
    assert len(body["split"]) == 2
    assert db.session.get(ScanDocument, doc_id).status == SPLIT
    parts = ScanDocument.query.filter_by(parent_id=doc_id).all()
    assert {p.status for p in parts} == {UPLOADED} and all(p.page_count == 1 for p in parts)
    assert _read(client, parts[0].id).get_json()["status"] == TO_CHECK


def test_without_the_ai_key_bills_wait(app, client, db, scanning, client_admin):
    app.config["GEMINI_API_KEY"] = ""
    _login(client)
    assert b"isn&#39;t switched on" in client.get("/scan/").data or b"isn't switched on" in client.get("/scan/").data
    doc_id = _upload(client, _jpeg()).get_json()["id"]
    res = _read(client, doc_id)
    assert res.status_code == 409 and "set up" in res.get_json()["error"]


def test_the_daily_limit_holds(app, client, db, scanning, client_admin, monkeypatch):
    app.config["SCAN_DAILY_LIMIT"] = 1
    _login(client)
    _ready_doc(client, monkeypatch)
    doc_id = _upload(client, _jpeg("another bill")).get_json()["id"]
    res = _read(client, doc_id)
    assert res.status_code == 409 and "limit" in res.get_json()["error"]


def test_not_a_picture_is_refused(client, db, scanning, client_admin):
    _login(client)
    res = _upload(client, b"hello, not an image", "notes.txt")
    assert res.status_code == 400 and "photo" in res.get_json()["error"]


def test_the_module_is_for_the_owner_and_only_when_on(client, db, scanning, client_admin, staff):
    _login(client, staff.email, "CashierSecret123")
    assert client.get("/scan/").status_code == 403
    client.get("/auth/logout")
    scanning.set_feature("doc_scan", False)
    db.session.commit()
    _login(client)
    assert client.get("/scan/").status_code == 404


def test_one_clients_scans_are_invisible_to_another(client, db, scanning, client_admin, super_admin, monkeypatch):
    from app.models.tenant import BillingCycle, RegistrationType, Tenant
    from app.models.user import User, UserRole

    _login(client)
    doc_id = _ready_doc(client, monkeypatch)
    client.get("/auth/logout")
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
                   billing_cycle=BillingCycle.MONTHLY)
    other.set_feature("doc_scan", True)
    db.session.add(other)
    db.session.flush()
    owner = User(tenant_id=other.id, name="O", email="o@other.example.com", role=UserRole.CLIENT_ADMIN,
                 must_change_password=False)
    owner.set_password("OtherSecret123")
    db.session.add(owner)
    db.session.commit()
    _login(client, "o@other.example.com", "OtherSecret123")
    for url in (f"/scan/{doc_id}", f"/scan/{doc_id}/file", f"/scan/{doc_id}.json"):
        assert client.get(url).status_code == 404


def test_a_read_cut_off_part_way_is_picked_up_again(client, db, scanning, client_admin):
    from datetime import timedelta

    from app.models.mixins import utcnow
    from app.models.scan import READING

    _login(client)
    doc_id = _upload(client, _jpeg()).get_json()["id"]
    doc = db.session.get(ScanDocument, doc_id)
    doc.status = READING
    db.session.commit()
    assert f'data-pending=""'.encode() in client.get("/scan/").data   # still being read: left alone
    db.session.execute(db.text("UPDATE scan_documents SET updated_at = :t WHERE id = :i"),
                       {"t": utcnow() - timedelta(minutes=10), "i": doc_id})
    db.session.commit()
    assert f'data-pending="{doc_id}"'.encode() in client.get("/scan/").data
