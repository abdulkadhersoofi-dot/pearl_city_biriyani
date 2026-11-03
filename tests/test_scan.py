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
    # The direction is the parser's own (the validator depends on it); the
    # expense is a separate hint the screens use.
    assert result["extracted"]["category"] == "Purchase"
    assert result["extracted"]["model_category"] == "Expenses"
    from app.scan.services import display_category
    assert display_category(result["extracted"]) == "Expenses"


def test_the_direction_is_decided_exactly_as_the_parser_did(monkeypatch):
    # The validator's swap repair reads the direction, so ARFA must give it
    # the parser's own: our GSTIN on the seller's side means "Sales" (the
    # parser's pass 1a) even when the model called it an expense - never
    # "Expenses", which would silently skip that repair.
    mine = _bill(supplier_name="Acme Traders", supplier_gstin=OWN, receiver_name="Hotel Saravana Bhavan",
                 receiver_gstin=SELLER, category="Expenses")
    monkeypatch.setattr(reader, "call_model", FakeModel(mine))
    result = reader.read(_jpeg(), False, reader.Context(own_gstins=[OWN]), _settings())
    assert result["extracted"]["category"] == "Sales"
    bought = _bill(category="Expenses")   # our GSTIN as the buyer
    monkeypatch.setattr(reader, "call_model", FakeModel(bought))
    result = reader.read(_jpeg(), False, reader.Context(own_gstins=[OWN]), _settings())
    assert result["extracted"]["category"] == "Purchase"


def test_the_validator_is_the_parsers_unchanged():
    from pathlib import Path

    from app.scan.engine import validators

    text = Path(validators.__file__).read_text()
    assert text.startswith("# Ported unchanged from the ARFA Invoice Parser")
    assert "def validate_and_repair(rec: dict, repair: bool = True," in text


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
    assert page.status_code == 200 and b"Tumkur Spices" in page.data and b"post the purchase bill" in page.data
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
    assert b"post the purchase bill" not in page.data
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


# --- Vertex AI (Gemini through the Cloud project, no key) ------------------------------------------

class FakeHttp:
    """Records the calls; answers the token request and the model calls."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.posts, self.gets = [], []

    class Res:
        def __init__(self, status, body):
            self.status_code, self._body = status, body
            self.text = ""

        def json(self):
            return self._body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

    def get(self, url, headers=None, timeout=None):
        self.gets.append((url, headers))
        return self.Res(200, {"access_token": "server-token", "expires_in": 3600})

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url, headers, json))
        status = self.statuses.pop(0) if self.statuses else 200
        body = {"candidates": [{"content": {"parts": [{"text": '{"invoice_number": "X1"}'}]}}]}
        return self.Res(status, body if status == 200 else {})


def _vertex(location="asia-south1"):
    reader._TOKEN.update(value=None, expires=0)
    reader._GLOBAL_ONLY.clear()
    return reader.Settings(provider="vertex", project="arfa-gst-prod-1", location=location)


def test_vertex_calls_the_project_endpoint_with_the_servers_own_token():
    http = FakeHttp([200])
    text, model = reader.call_model([{"text": "hi"}], _vertex(), ["gemini-2.5-flash-lite"], reader.time.time() + 30,
                                    http=http)
    assert model == "gemini-2.5-flash-lite" and "X1" in text
    url, headers, body = http.posts[0]
    assert url == ("https://asia-south1-aiplatform.googleapis.com/v1/projects/arfa-gst-prod-1/locations/asia-south1/"
                   "publishers/google/models/gemini-2.5-flash-lite:generateContent")
    assert headers == {"Authorization": "Bearer server-token"} and "key" not in url
    assert body["contents"][0]["role"] == "user"
    assert http.gets[0][1] == {"Metadata-Flavor": "Google"}


def test_a_model_the_region_lacks_moves_to_the_global_endpoint_and_stays():
    http = FakeHttp([404, 200, 200])
    settings = _vertex()
    deadline = reader.time.time() + 30
    assert reader.call_model([{"text": "a"}], settings, ["gemini-2.5-flash"], deadline, http=http)[1]
    assert http.posts[1][0].startswith("https://aiplatform.googleapis.com/v1/projects/arfa-gst-prod-1/locations/global/")
    reader.call_model([{"text": "b"}], settings, ["gemini-2.5-flash"], deadline, http=http)
    assert "/locations/global/" in http.posts[2][0]   # remembered - no second detour
    assert len(http.gets) == 1                        # the token is reused


def test_the_pro_model_gets_a_thinking_budget_the_fast_ones_none():
    http = FakeHttp([200, 200])
    deadline = reader.time.time() + 30
    reader.call_model([{"text": "a"}], _vertex("global"), ["gemini-2.5-pro"], deadline, http=http)
    reader.call_model([{"text": "a"}], _vertex("global"), ["gemini-2.5-flash-lite"], deadline, http=http)
    assert http.posts[0][2]["generationConfig"]["thinkingConfig"]["thinkingBudget"] > 0
    assert http.posts[1][2]["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 0


def test_the_module_is_set_up_by_vertex_without_any_key(app, client, db, scanning, client_admin):
    from app.scan import services

    app.config.update(GEMINI_API_KEY="", SCAN_PROVIDER="vertex", GCP_PROJECT="arfa-gst-prod-1")
    with app.test_request_context():
        settings = services.engine_settings()
    assert settings.provider == "vertex" and settings.project == "arfa-gst-prod-1" and not settings.api_key
    _login(client)
    assert b"isn&#39;t switched on" not in client.get("/scan/").data


def test_a_model_that_refuses_the_thinking_setting_is_asked_again_without_it():
    class Http(FakeHttp):
        def post(self, url, json=None, headers=None, timeout=None):
            self.posts.append((url, headers, {**json, "generationConfig": dict(json["generationConfig"])}))
            if "thinkingConfig" in json["generationConfig"]:
                res = self.Res(400, {})
                res.text = "Thinking budget is not supported for this model. Use thinking level instead."
                return res
            res = self.Res(200, {"candidates": [{"content": {"parts": [{"text": '{"ok": 1}'}]}}]})
            res.text = ""
            return res

    http = Http([])
    text, model = reader.call_model([{"text": "a"}], _vertex("global"), ["gemini-4-argon"], reader.time.time() + 30,
                                    http=http)
    assert model == "gemini-4-argon" and len(http.posts) == 2
    assert "thinkingConfig" not in http.posts[1][2]["generationConfig"]


def test_the_models_are_a_setting(app):
    from app.config import Config

    assert reader.Settings().models == list(reader.DEFAULT_MODELS)
    assert "2.5" not in " ".join(reader.DEFAULT_MODELS)   # withdrawn on Vertex AI
    import os
    import re
    os.environ["SCAN_MODELS"] = "gemini-x gemini-y"
    try:
        assert [m for m in re.split(r"[,;\s]+", os.environ["SCAN_MODELS"]) if m] == ["gemini-x", "gemini-y"]
    finally:
        del os.environ["SCAN_MODELS"]
    assert Config.SCAN_MODELS is None or isinstance(Config.SCAN_MODELS, list)


# --- the review screen's matching (and the bug where quotes broke it) ----------------------------

def test_the_review_form_gets_its_data_safely_even_with_quotes(client, db, scanning, client_admin, monkeypatch):
    _login(client)
    quoted = _bill(line_items=[{"description": 'Ball joint "upper" arm <oem>', "hsn_sac": "87089900", "quantity": 2,
                                "rate_per_unit": 650, "gst_rate": 28, "taxable_value": 1300, "igst": 364}],
                   taxable_value=1300, igst=364, invoice_value=1664)
    doc_id = _ready_doc(client, monkeypatch, bill=quoted)
    html = client.get(f"/scan/{doc_id}").get_data(as_text=True)
    assert 'x-data="scanReview()"' in html            # no data inside the attribute any more
    block = html.split('<script type="application/json" id="scan-data">', 1)[1].split("</script>", 1)[0]
    data = json.loads(block)
    assert data["lines"][0]["description"] == 'Ball joint "upper" arm <oem>'
    assert data["lines"][0]["product_id"] == "new"   # goods not in the catalogue: a new item


def test_posting_creates_new_items_and_links_existing_ones(client, db, scanning, client_admin, monkeypatch):
    from app.models.product import Product

    existing = Product(tenant_id=scanning.id, name="Cumin seeds 1kg", hsn_or_sac_code="0909", gst_rate=5, unit="KGS",
                       sale_price=0)
    db.session.add(existing)
    db.session.commit()
    _login(client)
    doc_id = _ready_doc(client, monkeypatch)
    data = json.loads(client.get(f"/scan/{doc_id}").get_data(as_text=True)
                      .split('id="scan-data">', 1)[1].split("</script>", 1)[0])
    assert data["lines"][0]["product_id"] == str(existing.id)   # matched by name
    assert data["lines"][1]["product_id"] == "new"
    res = client.post(f"/scan/{doc_id}/post", data=_purchase_form(**{"line_product_id[]": [str(existing.id), "new"]}))
    assert res.status_code == 302, res.data[:400]
    bill = PurchaseBill.query.one()
    container = Product.query.filter_by(name="Steel container").one()
    assert container.hsn_or_sac_code == "7323" and container.purchase_rate == Decimal("500.00")
    assert [l.product_id for l in bill.lines] == [existing.id, container.id]


def test_a_new_item_needs_a_proper_hsn(client, db, scanning, client_admin, monkeypatch):
    _login(client)
    doc_id = _ready_doc(client, monkeypatch)
    res = client.post(f"/scan/{doc_id}/post", data=_purchase_form(**{"line_product_id[]": ["new", ""],
                                                                     "line_hsn[]": ["329269099", "7323"]}))
    assert res.status_code == 422 and b"has 9 digits" in res.data
    assert PurchaseBill.query.count() == 0
    res = client.post(f"/scan/{doc_id}/post", data=_purchase_form(**{"line_product_id[]": ["", "new"],
                                                                     "line_hsn[]": ["0909", "73"]}))
    assert res.status_code == 422 and b"has 2 digits" in res.data


def test_an_expense_adds_a_new_supplier_too(client, db, scanning, client_admin, monkeypatch):
    from app.expenses.services import ensure_categories

    _login(client)
    doc_id = _ready_doc(client, monkeypatch, bill=_bill(category="Expenses"))
    category = ensure_categories(scanning.id)[0]
    db.session.commit()
    res = client.post(f"/scan/{doc_id}/post", data={
        "kind": "expense", "supplier_id": "new", "supplier_name": "Balaji Autos", "supplier_gstin": "",
        "invoice_number": "B-1", "invoice_date": "2026-10-20", "amount": "5380", "category_id": category.id,
        "payment_mode": "cash"})
    assert res.status_code == 302, res.data[:400]
    supplier = Supplier.query.filter_by(name="Balaji Autos").one()
    assert Expense.query.one().supplier_id == supplier.id


def test_units_become_gst_unit_codes():
    from app.scan.services import unit_code

    assert [unit_code(u) for u in ("Kg", "pcs", "EA", "Lt", "nos.", "", "widgets")] == \
        ["KGS", "PCS", "NOS", "LTR", "NOS", "NOS", "OTH"]


def test_a_category_offered_on_the_page_can_be_posted_with(app, client, db, scanning, client_admin, monkeypatch):
    """The "Pick a category" bug: the page made the standard categories but
    never kept them, so the one chosen didn't exist when posting."""
    import re as _re

    from app.models.expense import ExpenseCategory

    assert ExpenseCategory.query.count() == 0
    _login(client)
    service = _bill(supplier_name="Balaji Autos", category="Expenses",
                    line_items=[{"description": "Wheel alignment and vehicle washing", "hsn_sac": "998714",
                                 "quantity": 1, "rate_per_unit": 1000, "gst_rate": 18, "taxable_value": 1000,
                                 "igst": 180}], taxable_value=1000, igst=180, invoice_value=1180)
    doc_id = _ready_doc(client, monkeypatch, bill=service)
    with app.app_context():   # its own session, thrown away afterwards - as in production
        html = client.get(f"/scan/{doc_id}").get_data(as_text=True)
    chosen = _re.search(r'<option value="(\d+)" selected>Repairs</option>', html)
    assert chosen, "the bill's wording should pick Repairs"
    assert db.session.get(ExpenseCategory, int(chosen.group(1))) is not None   # it was kept
    res = client.post(f"/scan/{doc_id}/post", data={
        "kind": "expense", "supplier_id": "new", "supplier_name": "Balaji Autos", "invoice_number": "B-7",
        "invoice_date": "2026-10-20", "amount": "1180", "category_id": chosen.group(1), "payment_mode": "cash"})
    assert res.status_code == 302, res.data[:300]
    assert Expense.query.one().category.name == "Repairs"


def test_the_bills_to_check_are_a_table_in_upload_order(client, db, scanning, client_admin, monkeypatch):
    _login(client)
    first = _ready_doc(client, monkeypatch)
    second = _ready_doc(client, monkeypatch, photo=_jpeg("second bill"))
    html = client.get("/scan/?tab=check").get_data(as_text=True)
    assert "scan-queue-table" in html and html.index(f"/scan/{first}") < html.index(f"/scan/{second}")
    page = client.get(f"/scan/{first}").get_data(as_text=True)
    assert "Bill 1 of 2" in page and f'/scan/{second}"' in page   # Next
    res = client.post(f"/scan/{first}/post", data=_purchase_form())
    assert res.headers["Location"].endswith(f"/scan/{second}")   # straight on to the next bill


# --- the same supplier never twice; the QR wins; TDS; the bill's other details -----------------

def test_names_written_differently_are_one_supplier():
    from app.scan.services import names_alike

    assert names_alike("M/s. A.B.C. Traders Pvt. Ltd.", "ABC TRADERS")
    assert names_alike("Sri Balaji Traders", "Balaji Traders")
    assert names_alike("Kumar Auto Spares", "Kumar Auto Spare")
    assert names_alike("S K Hardware", "SK Hardware & Co")
    assert names_alike('BALAJI AUTOS "Kavangarai"', "Balaji Autos")      # a place after the name
    assert not names_alike("Balaji", "Balaji Autos")                     # one word is too little
    assert not names_alike("Balaji Traders", "Balaji Steel Traders")
    assert not names_alike("Sri Ram Traders", "Sri Ram Enterprises")
    assert not names_alike("Vijay Electricals", "Ajay Electricals")


def _supplier(db, tenant, **kw):
    supplier = Supplier(tenant_id=tenant.id, **kw)
    db.session.add(supplier)
    db.session.commit()
    return supplier


def test_new_on_the_screen_still_posts_to_the_supplier_already_there(client, db, scanning, client_admin, monkeypatch):
    # entered earlier by hand, without a GSTIN and in another style
    earlier = _supplier(db, scanning, name="TUMKUR SPICES PRIVATE LIMITED")
    _login(client)
    doc_id = _ready_doc(client, monkeypatch)
    data = json.loads(client.get(f"/scan/{doc_id}").get_data(as_text=True)
                      .split('id="scan-data">', 1)[1].split("</script>", 1)[0])
    assert data["supplier"] == str(earlier.id)          # picked on the screen
    res = client.post(f"/scan/{doc_id}/post", data=_purchase_form(supplier_id="new"))
    assert res.status_code == 302, res.data[:400]
    assert Supplier.query.count() == 1                  # no second copy
    assert PurchaseBill.query.one().supplier_id == earlier.id
    assert earlier.gstin == SELLER and earlier.state_code == "29"   # what was missing is filled in


def test_a_misread_gstin_or_a_second_bill_reuses_the_supplier(client, db, scanning, client_admin, monkeypatch):
    _login(client)
    first = _ready_doc(client, monkeypatch)
    assert client.post(f"/scan/{first}/post", data=_purchase_form(supplier_id="new")).status_code == 302
    near = SELLER[:-1] + ("B" if SELLER[-1] != "B" else "C")   # one character off: fails its check digit
    second = _ready_doc(client, monkeypatch, bill=_bill(invoice_number="TS/26-27/0050"), photo=_jpeg("ANOTHER BILL"))
    res = client.post(f"/scan/{second}/post", data=_purchase_form(
        supplier_id="new", supplier_gstin=near, supplier_name="Tumkur Spices", invoice_number="TS/26-27/0050"))
    assert res.status_code == 302, res.data[:400]
    assert Supplier.query.count() == 1 and PurchaseBill.query.count() == 2


def test_two_registrations_are_two_suppliers(app, db, scanning):
    from app.scan.services import find_supplier

    with app.test_request_context():
        _supplier(db, scanning, name="Tumkur Spices", gstin=SELLER, state_code="29")
        assert find_supplier(scanning, "Tumkur Spices Pvt Ltd", SELLER).gstin == SELLER
        assert find_supplier(scanning, "Tumkur Spices", "27AAPFU0939F1ZV") is None   # a valid, different GSTIN
        assert find_supplier(scanning, "Tumkur Spices", "").gstin == SELLER          # GSTIN not read: by name
        assert find_supplier(scanning, "Mysore Spices", "") is None


def test_the_einvoice_qr_number_date_and_total_win_over_the_print():
    ex = _bill(invoice_number="TS/26-27/OO42", invoice_date="2026-10-02", invoice_value=5830,
               flags=["invoice_number"])
    reader._apply_qr(ex, {"supplier_gstin": SELLER, "receiver_gstin": OWN, "invoice_number": "TS/26-27/0042",
                          "invoice_date": "2026-10-20", "invoice_value": 5380, "irn": "ab" * 32})
    assert ex["invoice_number"] == "TS/26-27/0042" and ex["invoice_date"] == "2026-10-20"
    assert ex["invoice_value"] == 5380 and "invoice_number" not in ex["flags"]
    assert set(ex["einvoice"]["replaced"]) >= {"invoice_number", "invoice_date", "invoice_value"}
    assert any("print read 'TS/26-27/OO42'" in n for n in ex["einvoice"]["notes"])
    # a later repair can't undo it
    ex["invoice_number"], ex["supplier_gstin"] = "X", STRANGER
    reader._hold_qr(ex)
    assert ex["invoice_number"] == "TS/26-27/0042" and ex["supplier_gstin"] == SELLER


def test_a_bill_printing_less_tds_keeps_its_net_against_the_qr():
    ex = _bill(tds=5, invoice_value=5375)
    reader._apply_qr(ex, {"supplier_gstin": SELLER, "invoice_number": "TS/26-27/0042", "invoice_value": 5380})
    assert ex["invoice_value"] == 5375


def test_tds_on_a_bill_posts_and_the_supplier_is_owed_the_net(client, db, scanning, client_admin, monkeypatch):
    from datetime import date as _d

    from app.accounts.payables import supplier_account
    from app.finance import journal

    _login(client)
    doc_id = _ready_doc(client, monkeypatch, bill=_bill(tds=5, tds_section="u/s 194Q", invoice_value=5375))
    html = client.get(f"/scan/{doc_id}").get_data(as_text=True)
    data = json.loads(html.split('id="scan-data">', 1)[1].split("</script>", 1)[0])
    assert data["tds"] == "5.00" and data["tdsSection"] == "194Q"
    # the printed total is the net: no "difference" to accept
    res = client.post(f"/scan/{doc_id}/post", data=_purchase_form(bill_total="5375", tds_amount="5",
                                                                  tds_section="194Q"))
    assert res.status_code == 302, res.data[:600]
    bill = PurchaseBill.query.one()
    assert bill.grand_total == Decimal("5380.00") and bill.tds_amount == Decimal("5.00")
    assert bill.tds_section == "194Q" and bill.net_payable == Decimal("5375.00")
    account = supplier_account(scanning, bill.supplier)
    assert account.balance == Decimal("5375.00")
    _, vouchers = journal.build(scanning, _d(2000, 1, 1), _d(2100, 1, 1))
    v = next(v for v in vouchers if v.number == bill.number)
    assert v.debits == v.credits and any(e.ledger == "TDS Payable" and e.credit == Decimal("5.00") for e in v.entries)
    view = client.get(f"/purchases/{bill.id}").get_data(as_text=True)
    assert "Less TDS" in view and "5375.00" in view


def test_serial_numbers_and_the_bills_references_are_kept(client, db, scanning, client_admin, monkeypatch):
    _login(client)
    laptop = _bill(po_number="PO-778", challan_number="DC/91", bank_name="HDFC Bank", bank_ifsc="HDFC0001234",
                   line_items=[{"description": "Laptop i5", "item_code": "LT-5520", "hsn_sac": "84713010",
                                "quantity": 2, "unit": "NOS", "rate_per_unit": 2500, "gst_rate": 18,
                                "taxable_value": 5000, "igst": 900, "serial_numbers": ["SN-A1", "SN-A2"],
                                "expiry": "", "mfg_date": "03/2026"}],
                   taxable_value=5000, igst=900, invoice_value=5900)
    doc_id = _ready_doc(client, monkeypatch, bill=laptop)
    html = client.get(f"/scan/{doc_id}").get_data(as_text=True)
    data = json.loads(html.split('id="scan-data">', 1)[1].split("</script>", 1)[0])
    line = data["lines"][0]
    assert line["serials"] == "SN-A1\nSN-A2" and line["item_code"] == "LT-5520" and line["mfg"] == "2026-03-01"
    assert 'name="detail_po_number"' in html and 'value="PO-778"' in html and "Other details on the bill" in html
    res = client.post(f"/scan/{doc_id}/post", data=_purchase_form(**{
        "bill_total": "5900", "line_description[]": ["Laptop i5"], "line_hsn[]": ["84713010"], "line_qty[]": ["2"],
        "line_unit[]": ["NOS"], "line_rate[]": ["2500"], "line_discount[]": ["0"], "line_gst_rate[]": ["18"],
        "line_itc[]": ["eligible"], "line_serials[]": ["SN-A1\nSN-A2"], "line_item_code[]": ["LT-5520"],
        "line_batch[]": [""], "line_mfg[]": ["2026-03-01"], "line_expiry[]": [""], "line_mrp[]": [""],
        "detail_po_number": "PO-778", "detail_challan_number": "DC/91", "detail_bank_ifsc": "HDFC0001234"}))
    assert res.status_code == 302, res.data[:600]
    bill = PurchaseBill.query.one()
    assert bill.details == {"Your order (PO) no.": "PO-778", "Challan / delivery note": "DC/91",
                            "IFSC": "HDFC0001234"}
    assert bill.lines[0].details == {"serials": ["SN-A1", "SN-A2"], "item_code": "LT-5520", "mfg": "2026-03-01"}
    view = client.get(f"/purchases/{bill.id}").get_data(as_text=True)
    assert "SN-A1, SN-A2" in view and "PO-778" in view and "LT-5520" in view


def test_the_reader_keeps_serials_and_references():
    shaped = reader._shape(_bill(po_number="PO-1", line_items=[
        {"description": "Phone", "quantity": 1, "taxable_value": 100, "serial_numbers": "IMEI1, IMEI2",
         "batch": "B7", "mrp": "120"}]))
    assert shaped["po_number"] == "PO-1"
    assert shaped["line_items"][0]["serial_numbers"] == ["IMEI1", "IMEI2"]
    assert shaped["line_items"][0]["batch"] == "B7" and shaped["line_items"][0]["mrp"] == 120


def test_printed_dates_become_iso():
    from app.scan.services import iso_date

    assert iso_date("03/27", month_end=True) == "2027-03-31"
    assert iso_date("MAR 2027", month_end=True) == "2027-03-31"
    assert iso_date("05/10/2026") == "2026-10-05"
    assert iso_date("2026-10-05") == "2026-10-05"
    assert iso_date("soon") == ""


# --- discounts as printed (a percentage or rupees), exactly ------------------------------------

def test_a_discount_is_taken_as_the_bill_gives_it():
    from app.scan.services import line_discount

    assert line_discount(Decimal("1000"), Decimal("900")) == ("pct", Decimal("10.00"))
    assert line_discount(Decimal("1000"), Decimal("925")) == ("pct", Decimal("7.50"))
    # Rs. 100 off 1,299 is 7.698...% - as a rounded percentage it would be paise out: rupees it is
    assert line_discount(Decimal("1299"), Decimal("1199")) == ("amt", Decimal("100.00"))
    assert line_discount(Decimal("1299"), Decimal("1169.10"), 10) == ("pct", Decimal("10.00"))


def test_a_discount_at_the_foot_of_the_bill_goes_onto_the_lines():
    doc = ScanDocument(extracted={"taxable_value": 4500, "discount": 500, "line_items": [
        {"description": "A", "quantity": 2, "rate_per_unit": 1000, "taxable_value": 2000, "gst_rate": 18},
        {"description": "B", "quantity": 3, "rate_per_unit": 1000, "taxable_value": 3000, "gst_rate": 18}]})
    from app.scan.services import line_rows

    rows = line_rows(doc)
    assert [(r["disc_type"], r["discount"]) for r in rows] == [("amt", "200.00"), ("amt", "300.00")]


def test_a_rupee_discount_posts_to_the_paisa_and_comes_back_pro_rata(client, db, scanning, client_admin,
                                                                       monkeypatch):
    from app.purchases.services import create_purchase_return

    _login(client)
    bill_read = _bill(line_items=[{"description": "Mixer grinder", "hsn_sac": "85094010", "quantity": 2,
                                   "unit": "NOS", "rate_per_unit": 1299, "gst_rate": 18, "taxable_value": 2498,
                                   "igst": 449.64, "discount_amount": 100}],
                      taxable_value=2498, igst=449.64, invoice_value=2948)
    doc_id = _ready_doc(client, monkeypatch, bill=bill_read)
    data = json.loads(client.get(f"/scan/{doc_id}").get_data(as_text=True)
                      .split('id="scan-data">', 1)[1].split("</script>", 1)[0])
    assert (data["lines"][0]["disc_type"], data["lines"][0]["discount"]) == ("amt", "100.00")
    res = client.post(f"/scan/{doc_id}/post", data=_purchase_form(**{
        "bill_total": "2948", "line_description[]": ["Mixer grinder"], "line_hsn[]": ["85094010"],
        "line_qty[]": ["2"], "line_unit[]": ["NOS"], "line_rate[]": ["1299"], "line_discount[]": ["0"],
        "line_discount_flat[]": ["100"], "line_gst_rate[]": ["18"], "line_itc[]": ["eligible"]}))
    assert res.status_code == 302, res.data[:500]
    bill = PurchaseBill.query.one()
    line = bill.lines[0]
    assert line.discount_flat == Decimal("100.00") and line.taxable_value == Decimal("2498.00")
    assert bill.grand_total == Decimal("2948.00")
    ret = create_purchase_return(bill, client_admin, {line.id: Decimal("1")}, "One faulty")
    assert ret.total_taxable_value == Decimal("1249.00")   # half the rupee discount goes back with half the goods


def test_the_manual_bill_form_takes_a_rupee_discount(client, db, scanning, client_admin):
    from app.inventory.ledger import default_location

    supplier = _supplier(db, scanning, name="Tumkur Spices", gstin=SELLER, state_code="29")
    _login(client)
    gstin = scanning.gstins.first()
    res = client.post("/purchases/new", data={
        "supplier_id": supplier.id, "gstin_id": gstin.id, "supplier_invoice_no": "M-1",
        "supplier_invoice_date": "2026-10-20", "bill_date": "2026-10-21", "location_id": default_location(scanning.id).id,
        "line_product_id[]": [""], "line_description[]": ["Cumin"], "line_hsn[]": ["0909"], "line_qty[]": ["3"],
        "line_unit[]": ["KGS"], "line_rate[]": ["333.33"], "line_discount[]": ["0"], "line_discount_flat[]": ["49.99"],
        "line_gst_rate[]": ["5"], "line_itc[]": ["eligible"]})
    assert res.status_code == 302, res.data[:500]
    assert PurchaseBill.query.one().total_taxable_value == Decimal("950.00")


# --- pointing at a value shows where it was read ----------------------------------------------

def test_boxes_from_one_tall_image_land_on_their_own_pages():
    # two pages stacked: page 1 at y 26..1026, page 2 at y 1066..2066 of a 2080-high image
    layout = {"size": (800, 2080), "pages": [(0, 26, 800, 1000), (0, 1066, 800, 1000)]}
    ex = {"boxes": {"invoice_number": [0, 100, 500, 110, 700],      # near the top: page 1
                    "invoice_value": [0, 900, 600, 910, 800]},      # near the bottom: page 2
          "line_items": [{"box": [0, 300, 10, 310, 990]}]}
    reader.place_boxes(ex, [0, 3], layout)    # the second image was the document's 4th page
    page, y0, x0, y1, x1 = ex["boxes"]["invoice_number"]
    assert page == 0 and abs(y0 - (100 / 1000 * 2080 - 26) / 1000 * 1000) < 0.2 and x0 == 500
    assert ex["boxes"]["invoice_value"][0] == 3
    assert ex["line_items"][0]["box"][0] == 0


def test_the_reader_keeps_only_sensible_boxes():
    shaped = reader._shape(_bill(boxes={"invoice_number": [0, 50, 600, 70, 900], "supplier_gstin": [80, 100, 300, 120],
                                        "junk": [0, 1, 2, 3, 4], "invoice_date": "top right",
                                        "invoice_value": [0, 500, 500, 500, 600]}))
    assert shaped["boxes"] == {"invoice_number": [0, 50.0, 600.0, 70.0, 900.0],
                               "supplier_gstin": [0, 80.0, 100.0, 300.0, 120.0]}


def test_the_review_marks_where_each_value_is_and_serves_the_pages(client, db, scanning, client_admin, monkeypatch):
    _login(client)
    doc_id = _ready_doc(client, monkeypatch, bill=_bill(boxes={"invoice_number": [0, 50, 600, 70, 900]},
                                                       line_items=[dict(_bill()["line_items"][0],
                                                                        box=[0, 300, 20, 320, 980])],
                                                       taxable_value=4000, igst=200, invoice_value=4200))
    html = client.get(f"/scan/{doc_id}").get_data(as_text=True)
    assert "data-zoom='[0, 50.0, 600.0, 70.0, 900.0]'" in html and "scan_zoom.js" in html
    assert f'data-page-url="/scan/{doc_id}/page/{{n}}.jpg?v=' in html
    data = json.loads(html.split('id="scan-data">', 1)[1].split("</script>", 1)[0])
    assert data["lines"][0]["box"] == [0, 300.0, 20.0, 320.0, 980.0]
    page = client.get(f"/scan/{doc_id}/page/0.jpg")
    assert page.status_code == 200 and page.data[:3] == b"\xff\xd8\xff"
    assert client.get(f"/scan/{doc_id}/page/1.jpg").status_code == 404


def test_a_pdf_page_is_served_for_the_zoom(client, db, scanning, client_admin, monkeypatch):
    fitz = pytest.importorskip("fitz")
    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), "TAX INVOICE   Invoice No: TS/26-27/0042", fontsize=12)
    _login(client)
    monkeypatch.setattr(reader, "call_model", FakeModel(_bill()))
    doc_id = _upload(client, pdf.tobytes(), "bill.pdf").get_json()["id"]
    res = client.get(f"/scan/{doc_id}/page/0.jpg")
    assert res.status_code == 200 and res.data[:3] == b"\xff\xd8\xff"


# --- every bill upright, however it was photographed or scanned --------------------------------

def test_boxes_turn_with_the_page():
    # a box near the top-left of a sideways page ends near the top-right once turned a quarter clockwise
    assert reader.turn_box([0, 100, 50, 200, 150], 90) == [0, 50.0, 800.0, 150.0, 900.0]
    assert reader.turn_box([0, 100, 50, 200, 150], 180) == [0, 800.0, 850.0, 900.0, 950.0]
    assert reader.turn_box([0, 100, 50, 200, 150], 270) == [0, 850.0, 100.0, 950.0, 200.0]
    assert reader.turn_box([2, 1, 2, 3, 4], 0) == [2, 1, 2, 3, 4]


def test_a_sideways_photo_is_turned_upright_with_its_boxes(monkeypatch):
    photo = _jpeg()   # 1200 x 1600
    monkeypatch.setattr(reader, "call_model", FakeModel(_bill(rotation=[90],
                                                              boxes={"invoice_number": [0, 100, 50, 200, 150]})))
    result = reader.read(photo, False, reader.Context(own_gstins=[OWN]), _settings())
    assert result["file_data"] is not None
    assert Image.open(io.BytesIO(result["file_data"])).size == (1600, 1200)
    assert result["extracted"]["boxes"]["invoice_number"] == [0, 50.0, 800.0, 150.0, 900.0]
    assert result["extracted"]["turned"] == {"0": 90}
    assert "turns" not in result["extracted"] and "rotation" not in result["extracted"]


def test_an_upright_photo_is_left_alone(monkeypatch):
    monkeypatch.setattr(reader, "call_model", FakeModel(_bill()))
    result = reader.read(_jpeg(), False, reader.Context(own_gstins=[OWN]), _settings())
    assert result["file_data"] is None and "turned" not in result["extracted"]


def test_a_pdf_whose_text_runs_sideways_is_turned_before_it_is_read(monkeypatch):
    fitz = pytest.importorskip("fitz")
    pdf = fitz.open()
    page = pdf.new_page(width=595, height=842)
    for i in range(10):
        page.insert_text((80 + i * 40, 700), f"TAX INVOICE line {i} Cumin seeds 4,000.00", fontsize=11, rotate=90)
    data = pdf.tobytes()
    assert reader.pdf_text_turns(data) == {0: 90}
    monkeypatch.setattr(reader, "call_model", FakeModel(_bill()))
    result = reader.read(data, True, reader.Context(own_gstins=[OWN]), _settings())
    turned = fitz.open(stream=result["file_data"], filetype="pdf")
    assert turned[0].rotation == 90 and reader.pdf_text_turns(result["file_data"]) == {}
    assert result["extracted"]["turned"] == {"0": 90}


def test_the_turn_buttons_turn_the_bill_and_its_boxes(client, db, scanning, client_admin, monkeypatch):
    _login(client)
    doc_id = _ready_doc(client, monkeypatch, bill=_bill(boxes={"invoice_number": [0, 100, 50, 200, 150]}))
    html = client.get(f"/scan/{doc_id}").get_data(as_text=True)
    assert "is-wide" in html and "data-scan-viewer" in html and 'name="deg" value="90"' in html
    res = client.post(f"/scan/{doc_id}/turn", data={"deg": "90"})
    assert res.status_code == 302
    doc = db.session.get(ScanDocument, doc_id)
    assert Image.open(io.BytesIO(doc.file_data)).size == (1600, 1200)
    assert Image.open(io.BytesIO(doc.preview)).width > Image.open(io.BytesIO(doc.preview)).height
    assert doc.extracted["boxes"]["invoice_number"] == [0, 50.0, 800.0, 150.0, 900.0]
    assert doc.extracted["turned"] == {"0": 90}


def test_each_value_on_a_line_points_at_its_own_cell():
    from app.scan.services import line_cells, line_rows

    columns = {"hsn_sac": [0, 300, 400, 700, 480], "cgst": [0, 300, 700, 700, 760], "sgst": [0, 300, 780, 700, 840],
               "amount": [0, 300, 880, 700, 990]}
    cells = line_cells([0, 410, 10, 430, 990], columns)
    assert cells["hsn_sac"] == [0, 410, 400, 430, 480]
    assert cells["tax"] == [0, 410, 700, 430, 840]        # CGST and SGST together
    assert line_cells(None, columns) == {} and line_cells([0, 1, 2, 3, 4], {}) == {}
    doc = ScanDocument(extracted={**_bill(), "columns": columns,
                                  "line_items": [dict(_bill()["line_items"][0], box=[0, 410, 10, 430, 990])]})
    assert line_rows(doc)[0]["cells"]["amount"] == [0, 410, 880, 430, 990]


def test_the_reader_places_the_tables_columns_and_every_detail():
    assert "columns" in reader.PROMPT and "challan_number" in reader.PROMPT.split("WHERE IT IS PRINTED")[1]
    shaped = reader._shape(_bill(columns={"hsn_sac": [0, 300, 400, 700, 480], "nonsense": [0, 1, 2, 30, 40]},
                                 boxes={"challan_number": [0, 80, 600, 95, 700]}))
    assert shaped["columns"] == {"hsn_sac": [0, 300.0, 400.0, 700.0, 480.0]}
    assert shaped["boxes"]["challan_number"] == [0, 80.0, 600.0, 95.0, 700.0]
    reader.turn_boxes(shaped, {0: 180})
    assert shaped["columns"]["hsn_sac"] == [0, 300.0, 520.0, 700.0, 600.0]
