"""The ARFA Scan phone app's API (app/scan/api.py): signing in with an
email and password, the bills feed, sending a bill with what the phone
read, reading, the details, approving - by the owner, and by a branch only
when the owner lets branches approve - and retaking a bill."""

import io
import json
from decimal import Decimal

import pytest

from app.models.purchase import PurchaseBill
from app.models.scan import DISCARDED, POSTED, TO_CHECK, ScanAppToken, ScanDocument
from app.scan.engine import reader
from tests.test_scan import OWN, SELLER, FakeModel, _bill, _jpeg, _jwt

API = "/api/scan/v1"


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
    res = client.post(f"{API}/auth/login", json={"email": email, "password": password, "device": "Pixel 8"})
    assert res.status_code == 200, res.get_json()
    return {"Authorization": "Bearer " + res.get_json()["token"]}


def _send(client, auth, photo=None, **form):
    data = {"file": (io.BytesIO(photo or _jpeg()), "bill.jpg"), **form}
    return client.post(f"{API}/bills", data=data, headers=auth, content_type="multipart/form-data")


def _ready(client, auth, monkeypatch, bill=None, photo=None, **form):
    model = FakeModel(bill or _bill())
    monkeypatch.setattr(reader, "call_model", model)
    res = _send(client, auth, photo, **form)
    assert res.status_code == 201, res.get_json()
    doc_id = res.get_json()["bill"]["id"]
    body = client.post(f"{API}/bills/{doc_id}/read", headers=auth).get_json()
    assert body["bill"]["status"] == TO_CHECK, body
    return doc_id, model


# --- signing in -------------------------------------------------------------------------------------

def test_the_owner_signs_in_with_email_and_password(client, db, scanning, client_admin):
    res = client.post(f"{API}/auth/login", json={"email": "Admin@Acme.example.com", "password": "ClientSecret123"})
    body = res.get_json()
    assert res.status_code == 200 and body["token"] and body["user"]["owner"] and body["can_approve"]
    token = ScanAppToken.query.one()
    assert token.token_hash != body["token"]          # only its hash is kept
    me = client.get(f"{API}/me", headers={"Authorization": "Bearer " + body["token"]}).get_json()
    assert me["business"]["name"] and me["counts"]["to_check"] == 0


def test_a_wrong_password_and_no_token_are_refused(client, db, scanning, client_admin):
    assert client.post(f"{API}/auth/login", json={"email": "admin@acme.example.com", "password": "nope"}).status_code == 401
    assert client.get(f"{API}/me").status_code == 401
    assert client.get(f"{API}/me", headers={"Authorization": "Bearer made-up"}).status_code == 401


def test_without_scan_bills_switched_on_the_app_says_so(client, db, tenant, client_admin):
    res = client.post(f"{API}/auth/login", json={"email": "admin@acme.example.com", "password": "ClientSecret123"})
    assert res.status_code == 403 and "Scan bills" in res.get_json()["error"]


def test_signing_out_and_a_new_password_end_the_phones_session(client, db, scanning, client_admin):
    auth = _login(client)
    assert client.post(f"{API}/auth/logout", headers=auth).status_code == 200
    assert client.get(f"{API}/me", headers=auth).status_code == 401
    auth = _login(client)
    client_admin.set_password("AnotherSecret456")
    db.session.commit()
    assert client.get(f"{API}/me", headers=auth).get_json()["signed_out"]


def test_two_step_sign_in_asks_for_the_code(client, db, scanning, client_admin, monkeypatch):
    from app.auth import security
    from app.models.mixins import utcnow

    client_admin.totp_enabled_at = utcnow()
    db.session.commit()
    res = client.post(f"{API}/auth/login", json={"email": "admin@acme.example.com", "password": "ClientSecret123"})
    body = res.get_json()
    assert body["two_factor"] and "token" not in body
    monkeypatch.setattr(security, "verify_user_code", lambda user, code: code == "123456")
    assert client.post(f"{API}/auth/two-factor", json={"pending": body["pending"], "code": "000000"}).status_code == 401
    ok = client.post(f"{API}/auth/two-factor", json={"pending": body["pending"], "code": "123456"}).get_json()
    assert ok["token"]


# --- sending and reading a bill -------------------------------------------------------------------------

def test_a_bill_sent_with_what_the_phone_read(client, db, scanning, client_admin, monkeypatch):
    auth = _login(client)
    qr = _jwt({"SellerGstin": SELLER, "BuyerGstin": OWN, "DocNo": "TS/26-27/0042", "DocDt": "20/10/2026",
               "TotInvVal": 5380})
    doc_id, model = _ready(client, auth, monkeypatch, ocr_text="TUMKUR SPICES PVT LTD  GSTIN 29AABCT1332L1ZA", qr=qr)
    doc = db.session.get(ScanDocument, doc_id)
    assert doc.phone_read["text"].startswith("TUMKUR") and doc.phone_read["qr"]["invoice_number"] == "TS/26-27/0042"
    feed = client.get(f"{API}/bills", headers=auth).get_json()
    bubble = feed["bills"][-1]
    assert bubble["id"] == doc_id and bubble["state"] in ("passed", "look") and bubble["qr"]
    assert bubble["supplier"] == "Tumkur Spices Pvt Ltd" and bubble["thumb"].startswith(f"{API}/bills/{doc_id}/thumb.jpg")
    assert client.get(bubble["thumb"], headers=auth).data[:3] == b"\xff\xd8\xff"


def test_the_phones_text_goes_to_the_reader(client, db, scanning, client_admin, monkeypatch):
    seen = []

    class Spy(FakeModel):
        def __call__(self, parts, *a, **k):
            seen.append(" ".join(p.get("text", "") for p in parts))
            return super().__call__(parts, *a, **k)

    auth = _login(client)
    monkeypatch.setattr(reader, "call_model", Spy(_bill()))
    doc_id = _send(client, auth, ocr_text="INVOICE NO TS/26-27/0042").get_json()["bill"]["id"]
    client.post(f"{API}/bills/{doc_id}/read", headers=auth)
    assert any("PHONE'S OWN OCR" in s and "TS/26-27/0042" in s for s in seen)


def test_the_bills_details_say_what_is_wrong(client, db, scanning, client_admin, monkeypatch):
    auth = _login(client)
    doc_id, _ = _ready(client, auth, monkeypatch, bill=_bill(invoice_value=6000))
    body = client.get(f"{API}/bills/{doc_id}", headers=auth).get_json()
    total = next(f for f in body["fields"] if f["key"] == "invoice_value")
    assert total["flagged"] and body["issues_list"] and body["totals"]["total"] == "5380.00"
    assert len(body["lines"]) == 2 and body["kind"] == "purchase" and body["can_approve"]
    assert client.get(body["page_url"].replace("{n}", "0"), headers=auth).status_code == 200


def test_polling_gives_only_what_changed(client, db, scanning, client_admin, monkeypatch):
    auth = _login(client)
    doc_id, _ = _ready(client, auth, monkeypatch)
    first = client.get(f"{API}/bills", headers=auth).get_json()
    again = client.get(f"{API}/bills", query_string={"since": first["server_time"]}, headers=auth).get_json()
    assert again["bills"] == []
    client.post(f"{API}/bills/{doc_id}/discard", headers=auth)
    changed = client.get(f"{API}/bills", query_string={"since": first["server_time"]}, headers=auth).get_json()
    assert [b["state"] for b in changed["bills"]] == ["discarded"]


# --- approving: the owner, and the branches when allowed -------------------------------------------------

def test_the_owner_approves_on_the_phone(client, db, scanning, client_admin, monkeypatch):
    auth = _login(client)
    doc_id, _ = _ready(client, auth, monkeypatch)
    res = client.post(f"{API}/bills/{doc_id}/approve", json={}, headers=auth)
    assert res.status_code == 200, res.get_json()
    bill = PurchaseBill.query.one()
    assert bill.grand_total == Decimal("5380.00") and bill.supplier_invoice_no == "TS/26-27/0042"
    assert db.session.get(ScanDocument, doc_id).status == POSTED


def test_a_total_that_doesnt_match_asks_before_posting(client, db, scanning, client_admin, monkeypatch):
    auth = _login(client)
    doc_id, _ = _ready(client, auth, monkeypatch, bill=_bill(invoice_value=6000))
    res = client.post(f"{API}/bills/{doc_id}/approve", json={}, headers=auth)
    assert res.status_code == 409 and res.get_json()["code"] == "difference"
    assert client.post(f"{API}/bills/{doc_id}/approve", json={"accept_difference": True},
                       headers=auth).status_code == 200
    # what was corrected on the phone is what posts
    doc2, _ = _ready(client, auth, monkeypatch, bill=_bill(invoice_number="TS/9", invoice_value=6000),
                     photo=_jpeg("second"))
    client.post(f"{API}/bills/{doc2}/approve", json={"bill_total": "5380", "invoice_number": "TS/10"}, headers=auth)
    assert PurchaseBill.query.filter_by(supplier_invoice_no="TS/10").one().grand_total == Decimal("5380.00")


def test_branches_scan_and_the_owner_approves(client, db, scanning, client_admin, staff, monkeypatch):
    branch = _login(client, "cashier@acme.example.com", "CashierSecret123")
    me = client.get(f"{API}/me", headers=branch).get_json()
    assert not me["user"]["owner"] and not me["can_approve"]
    doc_id, _ = _ready(client, branch, monkeypatch)
    bubble = client.get(f"{API}/bills", headers=branch).get_json()["bills"][-1]
    assert not bubble["can_approve"]
    assert client.post(f"{API}/bills/{doc_id}/approve", json={}, headers=branch).status_code == 403
    owner = _login(client)
    owned = client.get(f"{API}/bills", headers=owner).get_json()["bills"][-1]
    assert owned["id"] == doc_id and owned["from"] == "Cashier" and owned["can_approve"]


def test_when_branches_may_approve_they_post_their_own(client, db, scanning, client_admin, staff, monkeypatch):
    from app.scan import services

    services.set_approvers(scanning, client_admin, "branches")
    db.session.commit()
    branch = _login(client, "cashier@acme.example.com", "CashierSecret123")
    doc_id, _ = _ready(client, branch, monkeypatch)
    assert client.post(f"{API}/bills/{doc_id}/approve", json={}, headers=branch).status_code == 200
    bill = PurchaseBill.query.one()
    assert bill.created_by_id == staff.id and bill.location.branch_user_id == staff.id   # into the branch's stock


def test_a_branch_sees_only_the_bills_it_sent(client, db, scanning, client_admin, staff, monkeypatch):
    owner = _login(client)
    mine, _ = _ready(client, owner, monkeypatch)
    branch = _login(client, "cashier@acme.example.com", "CashierSecret123")
    assert client.get(f"{API}/bills", headers=branch).get_json()["bills"] == []
    assert client.get(f"{API}/bills/{mine}", headers=branch).status_code == 404


def test_the_owner_chooses_who_approves_on_the_website(client, db, scanning, client_admin):
    client.post("/auth/login", data={"email": "admin@acme.example.com", "password": "ClientSecret123"})
    html = client.get("/scan/").get_data(as_text=True)
    assert "Who approves the bills scanned on phones?" in html
    client.post("/scan/approvers", data={"approvers": "branches"})
    from app.scan import services
    assert services.approvers(scanning) == "branches"


# --- retaking a bill that couldn't be read ----------------------------------------------------------------

def test_a_retake_replaces_the_bill(client, db, scanning, client_admin, monkeypatch):
    auth = _login(client)
    blank = {"supplier_name": "", "supplier_gstin": "", "receiver_gstin": "", "invoice_number": "",
             "invoice_value": 0, "confidence": 0.1, "line_items": []}
    monkeypatch.setattr(reader, "call_model", FakeModel(blank))
    old = _send(client, auth).get_json()["bill"]["id"]
    assert client.post(f"{API}/bills/{old}/read", headers=auth).get_json()["bill"]["state"] == "retake"
    new, _ = _ready(client, auth, monkeypatch, photo=_jpeg("sharper"), replaces=str(old))
    assert db.session.get(ScanDocument, old).status == DISCARDED
    assert db.session.get(ScanDocument, new).phone_read["replaces"] == old
    ids = [b["id"] for b in client.get(f"{API}/bills", headers=auth).get_json()["bills"]]
    assert ids == [new]
