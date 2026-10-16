"""Phase 8: e-Invoice (IRN) and e-Way Bill - applicability from
gst_config thresholds, the INV-01 builder / validator, the encrypted
login vault, the NIC adapter against a simulated NIC server (real RSA +
AES round trip), the GSP adapter, the logged call path, IRN generate /
record / cancel, e-way bills from invoices, challans and transfers, and
flags / roles / tenant isolation for every new table."""

import base64
import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
from cryptography.hazmat.primitives.asymmetric import rsa

from app.einvoice import eway, rules, services
from app.einvoice.errors import plain
from app.einvoice.providers import PROVIDERS, ProviderError
from app.einvoice.providers.common import IST
from app.einvoice.providers.nic import aes_decrypt, aes_encrypt
from app.einvoice.schema import EInvoiceDataError, build, payload_for, validate
from app.einvoice.vault import VaultError, seal, unseal
from app.invoicing.services import InvoiceInput, InvoiceValidationError, LineInput, create_invoice, void_invoice
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.einvoice import EInvoiceCredential, EWayBill, EWayBillStatus, GovApiCall
from app.models.inventory import StockLocation, StockTransfer, StockTransferLine
from app.models.invoice import InvoiceStatus
from app.models.invoice_series import DocumentType
from app.models.product import Product
from app.models.sales import ChallanPurpose, SalesDocumentKind
from app.models.tenant import BillingCycle, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.notes.services import NoteInput, create_note
from app.sales.services import SalesDocInput, create_sales_document
from app.utils import gst_config

D = Decimal
TODAY = date.today()
IRN = "a" * 64


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _setup(db, tenant, *flags, aato="60000000"):
    for key in flags:
        tenant.set_feature(key, True)
    tenant.aato = D(aato)
    g = tenant.gstins.first()
    g.registered_address, g.city, g.pincode = "12 Market Road", "Pune", "411001"
    db.session.commit()
    return g


@pytest.fixture()
def buyer(db, tenant):
    c = Customer(tenant_id=tenant.id, name="Ravi Stores", gstin="27AAAPR1234A1Z5", state_code="27",
                 state_name="Maharashtra", address_line1="4 Station Road", city="Mumbai", pincode="400001")
    db.session.add(c)
    db.session.commit()
    return c


@pytest.fixture()
def far_buyer(db, tenant):
    c = Customer(tenant_id=tenant.id, name="Chennai Traders", gstin="33AABCK1234M1Z5", state_code="33",
                 state_name="Tamil Nadu", address_line1="9 Anna Salai", city="Chennai", pincode="600002")
    db.session.add(c)
    db.session.commit()
    return c


@pytest.fixture()
def walk_in(db, tenant):
    c = Customer(tenant_id=tenant.id, name="Walk-in Customer", state_code="27", state_name="Maharashtra")
    db.session.add(c)
    db.session.commit()
    return c


def _invoice(db, tenant, user, customer, lines=None, doc_type=DocumentType.TAX_INVOICE, pos=None, on=None):
    inv = create_invoice(tenant, user, InvoiceInput(
        gstin_id=tenant.gstins.first().id, document_type=doc_type, customer_id=customer.id,
        place_of_supply_state_code=pos or customer.state_code, invoice_date=on or TODAY, notes=None,
        lines=lines or [LineInput(description="Steel almirah", hsn_or_sac_code="94031000", qty=D("2"),
                                  rate=D("30000"), discount_percent=D("0"), gst_rate=D("18"), unit="NOS")]))
    db.session.commit()
    return inv


# --- applicability ----------------------------------------------------------------------

def test_scope_follows_flag_registration_and_aato_threshold(db, tenant):
    assert not rules.tenant_in_scope(tenant)  # flag off
    tenant.set_feature("einvoice", True)
    tenant.aato = gst_config.EINVOICE_AATO_THRESHOLD
    assert not rules.tenant_in_scope(tenant)  # exactly Rs. 5 crore is not *above* it
    tenant.aato = gst_config.EINVOICE_AATO_THRESHOLD + 1
    assert rules.tenant_in_scope(tenant)
    tenant.registration_type = RegistrationType.COMPOSITION
    assert not rules.tenant_in_scope(tenant)


def test_only_b2b_export_and_their_notes_need_irn(db, tenant, client_admin, buyer, walk_in):
    _setup(db, tenant, "einvoice")
    b2b = _invoice(db, tenant, client_admin, buyer)
    b2c = _invoice(db, tenant, client_admin, walk_in)
    export = _invoice(db, tenant, client_admin, walk_in, doc_type=DocumentType.EXPORT_INVOICE_LUT, pos="97")
    assert rules.needs_irn(tenant, b2b) and rules.supply_type(b2b) == "B2B"
    assert not rules.needs_irn(tenant, b2c)
    assert rules.supply_type(export) == "EXPWOP" and rules.needs_irn(tenant, export)
    note = create_note(tenant, client_admin, NoteInput(
        original_invoice_id=b2b.id, document_type=DocumentType.CREDIT_NOTE, note_date=TODAY, reason="Damaged",
        lines=[LineInput(description="Steel almirah", hsn_or_sac_code="94031000", qty=D("1"), rate=D("30000"),
                         discount_percent=D("0"), gst_rate=D("18"), unit="NOS")]))
    db.session.commit()
    assert rules.needs_irn(tenant, note)
    b2b.irn = IRN
    assert not rules.needs_irn(tenant, b2b)  # already has one


def test_reporting_deadline_warning_only_at_10_crore(db, tenant, client_admin, buyer):
    _setup(db, tenant, "einvoice")
    old = _invoice(db, tenant, client_admin, buyer, on=TODAY - timedelta(days=gst_config.EINVOICE_REPORTING_DAYS - 2))
    assert rules.deadline_warning(tenant, old) is None  # AATO 6 crore: no time limit
    tenant.aato = gst_config.EINVOICE_REPORTING_LIMIT_AATO
    assert "Report this to the IRP by" in rules.deadline_warning(tenant, old)
    late = _invoice(db, tenant, client_admin, buyer, on=TODAY - timedelta(days=gst_config.EINVOICE_REPORTING_DAYS + 1))
    assert "will be refused" in rules.deadline_warning(tenant, late)


def test_irn_cancel_window(db, tenant, client_admin, buyer):
    inv = _invoice(db, tenant, client_admin, buyer)
    now = datetime.now(timezone.utc)
    inv.irn, inv.irn_ack_date = IRN, now - timedelta(hours=gst_config.IRN_CANCEL_HOURS - 1)
    assert rules.can_cancel_irn(inv, now)
    inv.irn_ack_date = now - timedelta(hours=gst_config.IRN_CANCEL_HOURS, minutes=1)
    assert not rules.can_cancel_irn(inv, now)


def test_eway_threshold_and_validity():
    limit = gst_config.EWAY_BILL_VALUE_THRESHOLD
    assert not rules.eway_bill_needed(limit) and rules.eway_bill_needed(limit + D("0.01"))
    assert rules.eway_validity_days(1) == 1 and rules.eway_validity_days(200) == 1
    assert rules.eway_validity_days(201) == 2 and rules.eway_validity_days(450) == 3
    assert rules.eway_validity_days(21, odc=True) == 2
    gen = datetime(2026, 10, 16, 22, 30, tzinfo=IST)
    upto = rules.eway_valid_upto(gen, 150)
    assert upto.astimezone(IST).date() == date(2026, 10, 17) and upto.astimezone(IST).hour == 23


# --- INV-01 builder ----------------------------------------------------------------------

def _check_amounts(payload):
    assert validate(payload, TODAY) == []
    v = payload["ValDtls"]
    assert round(sum(i["AssAmt"] for i in payload["ItemList"]), 2) == v["AssVal"]


def test_intra_state_b2b_splits_cgst_sgst(db, tenant, client_admin, buyer):
    _setup(db, tenant, "einvoice")
    inv = _invoice(db, tenant, client_admin, buyer)
    p = build(tenant, inv)
    assert p["TranDtls"]["SupTyp"] == "B2B" and p["DocDtls"]["Typ"] == "INV"
    item = p["ItemList"][0]
    assert item["CgstAmt"] == item["SgstAmt"] == 5400.0 and item.get("IgstAmt", 0) == 0
    assert p["ValDtls"]["TotInvVal"] == float(inv.grand_total)
    assert p["SellerDtls"]["Pin"] == 411001 and p["BuyerDtls"]["Pin"] == 400001
    _check_amounts(p)


def test_inter_state_igst_inclusive_price_and_discount(db, tenant, client_admin, far_buyer):
    _setup(db, tenant, "einvoice")
    inv = _invoice(db, tenant, client_admin, far_buyer, lines=[
        LineInput(description="Mixer", hsn_or_sac_code="85094010", qty=D("3"), rate=D("1180"),
                  discount_percent=D("10"), gst_rate=D("18"), unit="NOS", price_includes_tax=True),
        LineInput(description="Rice (exempt)", hsn_or_sac_code="1006", qty=D("1.5"), rate=D("80"),
                  discount_percent=D("0"), gst_rate=D("0"), unit="KGS")])
    p = build(tenant, inv)
    mixer, rice = p["ItemList"]
    assert mixer["IgstAmt"] > 0 and mixer["CgstAmt"] == 0
    assert mixer["Discount"] > 0 and mixer["TotAmt"] - mixer["Discount"] == pytest.approx(mixer["AssAmt"], abs=0.01)
    assert rice["GstRt"] == 0 and rice["IgstAmt"] == 0 and rice["Unit"] == "KGS"
    _check_amounts(p)


def test_credit_note_refers_to_invoice_and_rounding_reconciles(db, tenant, client_admin, buyer):
    _setup(db, tenant, "einvoice")
    inv = _invoice(db, tenant, client_admin, buyer, lines=[
        LineInput(description="Bolt", hsn_or_sac_code="73181500", qty=D("7"), rate=D("13.33"),
                  discount_percent=D("0"), gst_rate=D("18"), unit="NOS")])
    note = create_note(tenant, client_admin, NoteInput(
        original_invoice_id=inv.id, document_type=DocumentType.CREDIT_NOTE, note_date=TODAY, reason="Return",
        lines=[LineInput(description="Bolt", hsn_or_sac_code="73181500", qty=D("3"), rate=D("13.33"),
                         discount_percent=D("0"), gst_rate=D("18"), unit="NOS")]))
    db.session.commit()
    p = build(tenant, note)
    assert p["DocDtls"]["Typ"] == "CRN"
    assert p["RefDtls"]["PrecDocDtls"][0]["InvNo"] == inv.invoice_number
    assert p["ValDtls"]["RndOffAmt"] == float(note.round_off)
    _check_amounts(p)


def test_export_uses_urp_and_other_country(db, tenant, client_admin, walk_in):
    _setup(db, tenant, "einvoice")
    walk_in.name, walk_in.address_line1, walk_in.city = "Dubai Imports LLC", "Deira", "Dubai"
    inv = _invoice(db, tenant, client_admin, walk_in, doc_type=DocumentType.EXPORT_INVOICE_LUT, pos="97")
    assert inv.invoice_number.startswith("EXL/")  # 16 characters - the IRP's limit
    p = build(tenant, inv)
    assert p["TranDtls"]["SupTyp"] == "EXPWOP"
    assert p["BuyerDtls"]["Gstin"] == "URP" and p["BuyerDtls"]["Stcd"] == "96" and p["BuyerDtls"]["Pin"] == 999999
    _check_amounts(p)


def test_lut_export_keeps_its_prefix_without_einvoice(db, tenant, client_admin, walk_in):
    inv = _invoice(db, tenant, client_admin, walk_in, doc_type=DocumentType.EXPORT_INVOICE_LUT, pos="97")
    assert inv.invoice_number.startswith("EXP-LUT/")  # flag off: no change
    tenant.set_feature("einvoice", True)
    tenant.aato = D("60000000")
    db.session.commit()
    assert "16 characters" in " ".join(validate(build(tenant, inv), TODAY))


def test_validator_names_every_problem_in_plain_words(db, tenant, client_admin, buyer):
    tenant.set_feature("einvoice", True)
    tenant.aato = D("60000000")
    buyer.pincode = None
    db.session.commit()
    inv = _invoice(db, tenant, client_admin, buyer, lines=[
        LineInput(description="Thing", hsn_or_sac_code="12", qty=D("1"), rate=D("100"), discount_percent=D("0"),
                  gst_rate=D("18"))])
    with pytest.raises(EInvoiceDataError) as exc:
        payload_for(tenant, inv)
    text = " ".join(exc.value.problems)
    assert "e-Invoice settings" in text  # seller city / PIN missing
    assert "customer's 6-digit PIN" in text
    assert "HSN" in text


def test_b2c_invoice_cannot_be_built(db, tenant, client_admin, walk_in):
    _setup(db, tenant, "einvoice")
    inv = _invoice(db, tenant, client_admin, walk_in)
    with pytest.raises(EInvoiceDataError):
        build(tenant, inv)


# --- vault -------------------------------------------------------------------------------

def test_vault_round_trip_and_wrong_key(app):
    token = seal({"password": "s3cret!"})
    assert "s3cret" not in token
    assert unseal(token) == {"password": "s3cret!"}
    app.config["EINVOICE_SECRET_KEY"] = "a-different-key"
    with pytest.raises(VaultError):
        unseal(token)


# --- a simulated NIC server ------------------------------------------------------------

class Reply:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


class FakeNic:
    """Speaks the NIC protocol: decrypts what the adapter sends with its
    private key / the session key, answers encrypted the same way."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_pem = self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        self.sek = os.urandom(32)
        self.logins, self.seen, self.fail_next, self.expire_next = 0, [], None, False
        self.headers = {}

    def _rsa(self, data_b64):
        return self.key.decrypt(base64.b64decode(data_b64), rsa_padding.PKCS1v15())

    def post(self, url, json=None, headers=None, timeout=None):
        self.headers = headers
        if url.endswith("/eivital/v1.04/auth"):
            self.logins += 1
            creds = __import__("json").loads(self._rsa(json["Data"]))
            if creds["Password"] != "NicTestPassw0rd!":
                return Reply({"Status": 0, "ErrorDetails": [{"ErrorCode": "1001", "ErrorMessage": "Invalid Password"}]})
            app_key = base64.b64decode(creds["AppKey"])
            return Reply({"Status": 1, "Data": {"AuthToken": f"tok{self.logins}", "Sek": aes_encrypt(app_key, self.sek),
                                                "TokenExpiry": (datetime.now(IST) + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")}})
        body = __import__("json").loads(aes_decrypt(self.sek, json["Data"]))
        self.seen.append((url.rsplit("/", 1)[-1], body))
        if self.expire_next:
            self.expire_next = False
            return Reply({"Status": 0, "ErrorDetails": [{"ErrorCode": "1005", "ErrorMessage": "Invalid Token"}]})
        if self.fail_next:
            reply, self.fail_next = self.fail_next, None
            return Reply(reply)
        now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        if url.endswith("/Invoice"):
            data = {"Irn": IRN, "AckNo": 112610000000123, "AckDt": now, "SignedQRCode": "eyJhbGciOi.signed.qr",
                    "SignedInvoice": "eyJ.signed.inv"}
        elif url.endswith("/Cancel"):
            data = {"Irn": body["Irn"], "CancelDate": now}
        elif url.endswith("/ewaybill"):
            data = {"EwbNo": 331001234567, "EwbDt": now,
                    "EwbValidTill": (datetime.now(IST) + timedelta(days=1)).strftime("%Y-%m-%d 23:59:00")}
        else:
            return Reply({}, 404)
        return Reply({"Status": 1, "Data": aes_encrypt(self.sek, __import__("json").dumps(data).encode())})

    def get(self, url, headers=None, timeout=None):
        now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        data = {"Irn": url.rsplit("/", 1)[-1], "AckNo": 112610000000999, "AckDt": now, "SignedQRCode": "eyJ.dup.qr"}
        return Reply({"Status": 1, "Data": aes_encrypt(self.sek, json.dumps(data).encode())})


def _nic_login(db, tenant, user, server):
    g = tenant.gstins.first()
    cred = services.save_credential(tenant.id, g, "nic_sandbox", {
        "username": "API_ACME", "password": "NicTestPassw0rd!", "client_id": "cid", "client_secret": "csec",
        "public_key": server.public_pem}, user)
    db.session.commit()
    return cred


def test_nic_adapter_full_round_trip(db, tenant, client_admin, buyer):
    _setup(db, tenant, "einvoice")
    server = FakeNic()
    cred = _nic_login(db, tenant, client_admin, server)
    inv = _invoice(db, tenant, client_admin, buyer)
    services.generate_irn(tenant, client_admin, inv, http=server)
    db.session.commit()
    assert inv.irn == IRN and inv.irn_ack_number == "112610000000123" and inv.qr_code_data.startswith("eyJ")
    sent = server.seen[0][1]
    assert sent["DocDtls"]["No"] == inv.invoice_number and sent["SellerDtls"]["Gstin"] == "27AAAAA0000A1Z5"
    assert server.headers["AuthToken"] == "tok1" and server.headers["user_name"] == "API_ACME"
    call = GovApiCall.query.filter_by(tenant_id=tenant.id, action="generate_irn").one()
    assert call.success and call.document_id == inv.id and len(call.request_id) == 32
    assert AuditLog.query.filter_by(action="irn_generated", entity_id=inv.id).count() == 1
    # The login is stored encrypted and reused - no second login.
    assert "NicTestPassw0rd" not in cred.secrets and "tok1" not in (cred.session or "")
    services.cancel_irn(tenant, client_admin, inv, "2", "Wrong rate", http=server)
    db.session.commit()
    assert server.logins == 1
    assert inv.status == InvoiceStatus.VOID and inv.irn_cancelled_at is not None
    assert "Data entry mistake" in inv.void_reason


def test_nic_expired_token_logs_in_again_once(db, tenant, client_admin, buyer):
    _setup(db, tenant, "einvoice")
    server = FakeNic()
    _nic_login(db, tenant, client_admin, server)
    inv = _invoice(db, tenant, client_admin, buyer)
    services.generate_irn(tenant, client_admin, _invoice(db, tenant, client_admin, buyer), http=server)
    db.session.commit()
    server.expire_next = True
    services.generate_irn(tenant, client_admin, inv, http=server)
    db.session.commit()
    assert server.logins == 2 and inv.irn == IRN


def test_duplicate_irn_is_fetched_not_failed(db, tenant, client_admin, buyer):
    _setup(db, tenant, "einvoice")
    server = FakeNic()
    _nic_login(db, tenant, client_admin, server)
    inv = _invoice(db, tenant, client_admin, buyer)
    existing = "b" * 64
    server.fail_next = {"Status": 0, "ErrorDetails": [{"ErrorCode": "2150", "ErrorMessage": "Duplicate IRN"}],
                        "InfoDtls": [{"InfCd": "DUPIRN", "Desc": {"AckNo": 1, "Irn": existing}}]}
    services.generate_irn(tenant, client_admin, inv, http=server)
    db.session.commit()
    assert inv.irn == existing and inv.irn_ack_number == "112610000000999"
    actions = [c.action for c in GovApiCall.query.filter_by(tenant_id=tenant.id).order_by(GovApiCall.id)]
    assert actions == ["generate_irn", "get_irn"]


def test_irp_refusal_is_logged_with_plain_message(db, tenant, client_admin, buyer):
    _setup(db, tenant, "einvoice")
    server = FakeNic()
    _nic_login(db, tenant, client_admin, server)
    inv = _invoice(db, tenant, client_admin, buyer)
    server.fail_next = {"Status": 0, "ErrorDetails": [{"ErrorCode": "3028", "ErrorMessage": "Buyer GSTIN is invalid"}]}
    with pytest.raises(services.GovApiError) as exc:
        services.generate_irn(tenant, client_admin, inv, http=server)
    assert "customer's GSTIN" in exc.value.message and exc.value.code == "3028"
    db.session.rollback()
    call = GovApiCall.query.filter_by(tenant_id=tenant.id).one()  # committed despite the failure
    assert not call.success and call.error_code == "3028" and "customer's GSTIN" in call.plain_message
    assert db.session.get(type(inv), inv.id).irn is None


def test_wrong_password_and_unreachable_server(db, tenant, client_admin, buyer):
    _setup(db, tenant, "einvoice")
    server = FakeNic()
    g = tenant.gstins.first()
    services.save_credential(tenant.id, g, "nic_sandbox", {"username": "API_ACME", "password": "nope",
                                                          "client_id": "c", "client_secret": "s",
                                                          "public_key": server.public_pem}, client_admin)
    db.session.commit()
    cred = services.credential_for(tenant.id, g.id)
    with pytest.raises(services.GovApiError) as exc:
        services.call(cred, client_admin, "authenticate", None, http=server)
    assert "password" in exc.value.message

    import requests

    class Down:
        def post(self, *a, **k):
            raise requests.ConnectionError("no route")

    with pytest.raises(services.GovApiError) as exc:
        services.call(cred, client_admin, "authenticate", None, http=Down())
    assert "Couldn't reach" in exc.value.message and exc.value.code == "NETWORK"


def test_gsp_adapter_plain_json_with_configured_headers_and_paths():
    class FakeGsp:
        def __init__(self):
            self.calls = []

        def post(self, url, json=None, headers=None, timeout=None):
            self.calls.append((url, json, headers))
            if url.endswith("/login"):
                return Reply({"access_token": "gsp-token"})
            return Reply({"Status": 1, "Data": {"Irn": IRN, "AckNo": 5, "AckDt": "2026-10-16 10:00:00",
                                                "SignedQRCode": "qr"}})

    http = FakeGsp()
    p = PROVIDERS["gsp"]("27AAAAA0000A1Z5", {"base_url": "https://gsp.example/api/", "username": "u",
                                             "password": "p", "auth_path": "/login",
                                             "extra_headers": '{"aspid": "42"}'}, http=http)
    p.authenticate()
    result = p.generate_irn({"Version": "1.1"})
    assert result.irn == IRN and result.ack_date.hour == 10
    url, body, headers = http.calls[1]
    assert url == "https://gsp.example/api/eicore/v1.03/Invoice" and body == {"Version": "1.1"}
    assert headers["aspid"] == "42" and headers["AuthToken"] == "gsp-token"


def test_plain_messages_never_guess_codes():
    assert "customer's GSTIN" in plain(ProviderError("Recipient GSTIN not active", code="9999"))
    assert "PIN code" in plain(ProviderError("Invalid pincode for state", code="1"))
    assert plain(ProviderError("Something odd")).startswith("The government system refused")
    # "date" must start a word - "validated" isn't about dates
    assert "document date" not in plain(ProviderError("Request could not be validated"))


# --- IRN by hand, cancel rules ------------------------------------------------------------

def test_record_irn_from_offline_tool_and_qr_renders(app, db, tenant, client_admin, buyer, client):
    _setup(db, tenant, "einvoice")
    inv = _invoice(db, tenant, client_admin, buyer)
    with pytest.raises(services.EInvoiceError):
        services.record_irn(tenant, client_admin, inv, "short", "1", "2026-10-16T10:00", None)
    services.record_irn(tenant, client_admin, inv, IRN.upper(), "112610000000777", "2026-10-16T10:00", "eyJ.qr")
    db.session.commit()
    assert inv.irn == IRN and inv.irn_ack_date.astimezone(IST).hour == 10
    _login(client, client_admin.email, "ClientSecret123")
    page = client.get(f"/invoices/{inv.id}").data
    assert IRN.encode() in page and b"data:image/svg+xml" in page and b"Generate IRN" not in page
    pdf = client.get(f"/invoices/{inv.id}/pdf")
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF")


def test_active_irn_blocks_plain_void_and_after_24h_points_to_credit_note(db, tenant, client_admin, buyer, client):
    _setup(db, tenant, "einvoice")
    inv = _invoice(db, tenant, client_admin, buyer)
    inv.irn, inv.irn_ack_number = IRN, "1"
    inv.irn_ack_date = datetime.now(timezone.utc) - timedelta(hours=gst_config.IRN_CANCEL_HOURS + 1)
    db.session.commit()
    with pytest.raises(InvoiceValidationError):
        void_invoice(inv, client_admin, "mistake")
    with pytest.raises(services.EInvoiceError) as exc:
        services.cancel_irn(tenant, client_admin, inv, "2", "late", on_portal=True)
    assert "credit note" in exc.value.message
    _login(client, client_admin.email, "ClientSecret123")
    page = client.get(f"/invoices/{inv.id}").data
    assert b"make a credit note" in page and b"Void this invoice" not in page


def test_cancel_recorded_as_done_on_portal_voids(db, tenant, client_admin, buyer, client):
    _setup(db, tenant, "einvoice")
    inv = _invoice(db, tenant, client_admin, buyer)
    inv.irn, inv.irn_ack_number, inv.irn_ack_date = IRN, "1", datetime.now(timezone.utc)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    client.post(f"/einvoice/invoice/{inv.id}/irn/cancel", data={"reason_code": "1", "remark": "Raised twice",
                                                                "on_portal": "1"})
    db.session.refresh(inv)
    assert inv.status == InvoiceStatus.VOID and inv.irn_cancel_reason == "Duplicate: Raised twice"
    assert AuditLog.query.filter_by(action="irn_cancelled", entity_id=inv.id).count() == 1


# --- e-way bills ----------------------------------------------------------------------------

def test_eway_from_invoice_by_irn_and_standalone(db, tenant, client_admin, far_buyer):
    _setup(db, tenant, "eway_bill", "einvoice")
    server = FakeNic()
    _nic_login(db, tenant, client_admin, server)
    inv = _invoice(db, tenant, client_admin, far_buyer)
    assert eway.offer(tenant, inv)  # Rs. 70,800 > Rs. 50,000
    ewb = eway.draft_from(tenant, inv, client_admin)
    eway.apply_form(ewb, {"trans_mode": "1", "distance_km": "1200", "vehicle_no": "mh 12 ab 1234"})
    db.session.add(ewb)
    db.session.commit()
    assert ewb.vehicle_no == "MH12AB1234" and ewb.igst_value == D("10800.00") and eway.problems(ewb) == []
    bulk = eway.bulk_json([ewb])
    entry = bulk["billLists"][0]
    assert entry["docType"] == "INV" and entry["toStateCode"] == 33 and entry["transDistance"] == 1200
    assert entry["itemList"][0]["igstRate"] == 18.0 and entry["totInvValue"] == float(inv.grand_total)
    # With an IRN the IRP's EWB-by-IRN is used.
    inv.irn = IRN
    db.session.commit()
    eway.generate(tenant, client_admin, ewb, http=server)
    db.session.commit()
    assert ewb.status == EWayBillStatus.GENERATED and ewb.ewb_no == "331001234567" and ewb.generated_via == "irn"
    assert server.seen[-1][1]["Irn"] == IRN and server.seen[-1][1]["Distance"] == 1200
    # The IRP won't cancel an IRN with a live e-way bill on it - nor do we.
    inv.irn_ack_date = datetime.now(timezone.utc)
    with pytest.raises(services.EInvoiceError) as exc:
        services.cancel_irn(tenant, client_admin, inv, "2", "Wrong", on_portal=True)
    assert "e-way bill first" in exc.value.message


def test_eway_from_delivery_challan_and_portal_record(db, tenant, client_admin, buyer):
    _setup(db, tenant, "eway_bill", "sales_documents")
    doc = create_sales_document(tenant, client_admin, SalesDocInput(
        kind=SalesDocumentKind.DELIVERY_CHALLAN, gstin_id=tenant.gstins.first().id, customer_id=buyer.id,
        place_of_supply_state_code="27", doc_date=TODAY, invoice_document_type=DocumentType.TAX_INVOICE,
        challan_purpose=ChallanPurpose.JOB_WORK,
        lines=[LineInput(description="Fabric", hsn_or_sac_code="5208", qty=D("100"), rate=D("600"),
                         discount_percent=D("0"), gst_rate=D("5"), unit="MTR")]))
    db.session.commit()
    ewb = eway.draft_from(tenant, doc, client_admin)
    assert ewb.doc_type == "CHL" and ewb.sub_supply_type == "4"
    eway.apply_form(ewb, {"distance_km": "40", "transporter_id": "27AAACT2727Q1ZW"})
    db.session.add(ewb)
    db.session.commit()
    assert eway.problems(ewb) == []  # transporter ID alone is enough - Part A only
    eway.record(client_admin, ewb, "3310 0123 4567", "2026-10-16T09:15")
    db.session.commit()
    assert ewb.valid_upto is None  # no vehicle yet: not valid for movement
    now = datetime(2026, 10, 16, 10, 0, tzinfo=IST)
    eway.update_part_b(tenant, client_admin, ewb, {"reason_code": "4", "trans_mode": "1",
                                                   "vehicle_no": "MH14XY9876"}, on_portal=True, now=now)
    db.session.commit()
    assert ewb.vehicle_no == "MH14XY9876" and ewb.valid_upto.astimezone(IST).date() == date(2026, 10, 17)


def test_eway_from_stock_transfer_uses_cost_and_own_gstin(db, tenant, client_admin):
    _setup(db, tenant, "eway_bill", "inventory")
    g = tenant.gstins.first()
    a = StockLocation(tenant_id=tenant.id, name="Main store", address="12 Market Road, Pune 411001")
    b = StockLocation(tenant_id=tenant.id, name="Godown", address="Plot 7 MIDC, Pune 411019")
    p = Product(tenant_id=tenant.id, name="Cement bag", hsn_or_sac_code="2523", gst_rate=D("28"), unit="BAG",
                sale_price=D("400"))
    db.session.add_all([a, b, p])
    db.session.flush()
    from app.utils.numbering import get_or_create_series

    series = get_or_create_series(tenant.id, g.id, DocumentType.STOCK_TRANSFER, "2026-27")
    t = StockTransfer(tenant_id=tenant.id, gstin_id=g.id, invoice_series_id=series.id, number="ST/26-27/1",
                      from_location_id=a.id, to_location_id=b.id, transfer_date=TODAY, created_by_id=client_admin.id)
    t.lines.append(StockTransferLine(product_id=p.id, qty=D("200"), unit_cost=D("320")))
    db.session.add(t)
    db.session.commit()
    assert eway.source_value(t) == D("64000.00") and eway.offer(tenant, t)
    ewb = eway.draft_from(tenant, t, client_admin)
    assert ewb.sub_supply_type == "5" and ewb.to_party["gstin"] == g.gstin
    assert ewb.from_party["pincode"] == "411001" and ewb.to_party["pincode"] == "411019"


def test_eway_cancel_window_and_validation(db, tenant, client_admin, far_buyer):
    _setup(db, tenant, "eway_bill")
    inv = _invoice(db, tenant, client_admin, far_buyer)
    ewb = eway.draft_from(tenant, inv, client_admin)
    eway.apply_form(ewb, {"distance_km": "0", "vehicle_no": "12"})
    found = " ".join(eway.problems(ewb))
    assert "Distance" in found and "vehicle number" in found
    eway.apply_form(ewb, {"distance_km": "300", "vehicle_no": "TN01AB1234"})
    db.session.add(ewb)
    db.session.commit()
    eway.record(client_admin, ewb, "331001234568", "2026-10-16T09:00")
    later = ewb.ewb_date + timedelta(hours=gst_config.EWAY_BILL_CANCEL_HOURS + 1)
    with pytest.raises(services.EInvoiceError):
        eway.cancel(tenant, client_admin, ewb, "2", "Order cancelled", on_portal=True, now=later)
    eway.cancel(tenant, client_admin, ewb, "2", "Order cancelled", on_portal=True, now=ewb.ewb_date)
    assert ewb.status == EWayBillStatus.CANCELLED and ewb.cancel_reason == "Order cancelled: Order cancelled"


def test_below_threshold_no_eway_offer(db, tenant, client_admin, buyer, client):
    _setup(db, tenant, "eway_bill")
    small = _invoice(db, tenant, client_admin, buyer, lines=[
        LineInput(description="Chair", hsn_or_sac_code="94016100", qty=D("1"), rate=D("2000"),
                  discount_percent=D("0"), gst_rate=D("18"))])
    assert not eway.offer(tenant, small)
    _login(client, client_admin.email, "ClientSecret123")
    assert b"Make e-way bill" not in client.get(f"/invoices/{small.id}").data


# --- screens, flags, roles, isolation -------------------------------------------------------

ROUTES = ["/einvoice/", "/einvoice/settings", "/einvoice/log", "/einvoice/bulk.json", "/einvoice/eway-bills",
          "/einvoice/eway-bills/drafts.json"]


def test_flags_off_everything_404_and_no_trace_on_invoice(db, tenant, client_admin, buyer, client):
    tenant.aato = D("600000000")
    db.session.commit()
    inv = _invoice(db, tenant, client_admin, buyer)
    _login(client, client_admin.email, "ClientSecret123")
    for url in ROUTES + [f"/einvoice/invoice/{inv.id}.json", f"/einvoice/eway-bills/new/invoice/{inv.id}"]:
        assert client.get(url).status_code == 404, url
    assert client.post(f"/einvoice/invoice/{inv.id}/irn").status_code == 404
    page = client.get(f"/invoices/{inv.id}").data
    assert b"IRN" not in page and b"e-way" not in page.lower()


def test_staff_forbidden(db, tenant, staff, client):
    _setup(db, tenant, "einvoice", "eway_bill")
    _login(client, staff.email, "CashierSecret123")
    for url in ROUTES:
        assert client.get(url).status_code == 403, url


def test_generate_button_only_for_applicable_documents(db, tenant, client_admin, buyer, walk_in, client):
    _setup(db, tenant, "einvoice")
    b2b = _invoice(db, tenant, client_admin, buyer)
    b2c = _invoice(db, tenant, client_admin, walk_in)
    _login(client, client_admin.email, "ClientSecret123")
    page = client.get(f"/invoices/{b2b.id}").data
    assert b"IRN needed" in page and b"Download e-Invoice JSON" in page
    assert b"Generate IRN" not in page  # no login saved yet - JSON route only
    assert b"IRN needed" not in client.get(f"/invoices/{b2c.id}").data
    resp = client.get(f"/einvoice/invoice/{b2b.id}.json")
    assert resp.status_code == 200 and json.loads(resp.data)[0]["DocDtls"]["No"] == b2b.invoice_number
    bulk = client.get(f"/einvoice/bulk.json?period={TODAY.strftime('%Y-%m')}")
    assert [d["DocDtls"]["No"] for d in json.loads(bulk.data)] == [b2b.invoice_number]
    services.save_credential(tenant.id, tenant.gstins.first(), "gsp", {
        "username": "u", "password": "p", "base_url": "https://gsp.example"}, client_admin)
    db.session.commit()
    assert b"Generate IRN" in client.get(f"/invoices/{b2b.id}").data


def test_settings_saves_login_encrypted(db, tenant, client_admin, client):
    g = _setup(db, tenant, "einvoice")
    _login(client, client_admin.email, "ClientSecret123")
    client.post("/einvoice/settings", data={"gstin_id": g.id, "action": "login", "provider": "gsp",
                                            "username": "API_U", "password": "Hunter2!", "base_url": "https://g.example"})
    cred = EInvoiceCredential.query.filter_by(tenant_id=tenant.id).one()
    assert cred.username == "API_U" and "Hunter2" not in cred.secrets and unseal(cred.secrets)["password"] == "Hunter2!"
    # Leaving a secret blank keeps the saved one.
    client.post("/einvoice/settings", data={"gstin_id": g.id, "action": "login", "provider": "gsp",
                                            "username": "API_U", "password": "", "base_url": "https://g.example"})
    assert unseal(EInvoiceCredential.query.filter_by(tenant_id=tenant.id).one().secrets)["password"] == "Hunter2!"
    page = client.get("/einvoice/settings").data
    assert b"Hunter2" not in page and b"API_U" in page


@pytest.fixture()
def other(db, super_admin):
    t = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
               billing_cycle=BillingCycle.MONTHLY)
    db.session.add(t)
    db.session.flush()
    db.session.add(Gstin(tenant_id=t.id, gstin="29BBBBB1111B1Z5", state_code="29", state_name="Karnataka",
                         is_primary=True, city="Bengaluru", pincode="560001", registered_address="1 MG Road"))
    u = User(tenant_id=t.id, name="Other Admin", email="admin@other.example.com", role=UserRole.CLIENT_ADMIN,
             must_change_password=False)
    u.set_password("OtherSecret123")
    db.session.add(u)
    t.set_feature("einvoice", True)
    t.set_feature("eway_bill", True)
    t.aato = D("60000000")
    db.session.commit()
    return t, u


def test_tenant_isolation_for_every_new_table(db, tenant, client_admin, far_buyer, other, client):
    _setup(db, tenant, "einvoice", "eway_bill")
    server = FakeNic()
    cred = _nic_login(db, tenant, client_admin, server)
    inv = _invoice(db, tenant, client_admin, far_buyer)
    services.generate_irn(tenant, client_admin, inv, http=server)
    ewb = eway.draft_from(tenant, inv, client_admin)
    eway.apply_form(ewb, {"distance_km": "900", "vehicle_no": "MH12AB1234"})
    db.session.add(ewb)
    db.session.commit()
    other_tenant, other_admin = other
    _login(client, other_admin.email, "OtherSecret123")
    # Guessing ids: invoices, e-way bills, JSON - all 404.
    for url in (f"/einvoice/invoice/{inv.id}.json", f"/einvoice/eway-bills/{ewb.id}", f"/einvoice/eway-bills/{ewb.id}.json",
                f"/einvoice/eway-bills/new/invoice/{inv.id}"):
        assert client.get(url).status_code == 404, url
    for url in (f"/einvoice/invoice/{inv.id}/irn", f"/einvoice/invoice/{inv.id}/irn/cancel",
                f"/einvoice/eway-bills/{ewb.id}/generate", f"/einvoice/eway-bills/{ewb.id}/cancel",
                f"/einvoice/eway-bills/{ewb.id}/part-b", f"/einvoice/eway-bills/{ewb.id}/record"):
        assert client.post(url, data={"reason_code": "1", "remark": "x"}).status_code == 404, url
    # Their settings can't touch our GSTIN, nor see our login; the log and lists show none of ours.
    resp = client.post("/einvoice/settings", data={"gstin_id": tenant.gstins.first().id, "action": "remove"})
    assert resp.status_code == 404
    assert services.credential_for(other_tenant.id, tenant.gstins.first().id) is None
    assert services.credential_for(tenant.id, cred.gstin_id).is_active
    assert b"generate irn" not in client.get("/einvoice/log").data.lower()
    assert inv.invoice_number.encode() not in client.get("/einvoice/eway-bills").data
    assert b"API_ACME" not in client.get("/einvoice/settings").data
    db.session.refresh(ewb)
    assert ewb.status == EWayBillStatus.DRAFT and inv.status == InvoiceStatus.ISSUED
