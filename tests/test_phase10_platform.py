"""Phase 10 platform: the firm's subscription invoices (numbering, GST via
the one service, UPI, paid -> cycle through the idempotent
mark_period_paid, void, each subscriber sees only its own), admin
drill-downs for POS bills and notes, the owner's day summary (dashboard,
WhatsApp, SMTP email), the first-run checklist, /healthz and Sentry."""

from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

import pytest

from app.invoicing.services import InvoiceInput, LineInput, create_invoice
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.invoice_series import DocumentType
from app.models.platform import PlatformInvoice, PlatformInvoiceStatus, PlatformProfile, PlatformSeries
from app.models.pos_bill import POSBill
from app.models.product import Product
from app.models.tenant import BillingCycle, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.notes.services import NoteInput, create_note
from app.pos.services import CartInput, CartLineInput, checkout
from app.models.pos_bill import PaymentMode
from app.subscriptions import services as subs
from app.utils.billing import mark_period_paid

D = Decimal
TODAY = date.today()


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


@pytest.fixture()
def firm(db):
    prof = PlatformProfile(legal_name="ARFA Consultants LLP", gstin="27ABCDE1234F1Z5", state_code="27",
                           address="Pune", upi_vpa="arfa@okbank", upi_payee_name="ARFA", sac_code="997331",
                           gst_rate=D("18"), days_before_due=7, invoice_prefix="SUB")
    db.session.add(prof)
    db.session.commit()
    return prof


def _billable(db, tenant, fee="1000", due=None):
    tenant.subscription_fee = D(fee)
    tenant.next_billing_due = due or TODAY + timedelta(days=3)
    tenant.cycle_anchor_date = tenant.next_billing_due - timedelta(days=30)
    db.session.commit()


# --- the GST on a subscription invoice (through app.utils.gst) ---------------------------------

def test_subscription_tax_intra_inter_unregistered_and_rounding():
    intra = subs.compute(D("1000"), D("18"), True, True)
    assert (intra["total_cgst"], intra["total_sgst"], intra["total_igst"], intra["grand_total"]) == (
        D("90.00"), D("90.00"), D("0.00"), D("1180.00"))
    inter = subs.compute(D("1000"), D("18"), True, False)
    assert inter["total_igst"] == D("180.00") and inter["total_cgst"] == D("0.00")
    unregistered = subs.compute(D("1000"), D("18"), False, True)
    assert unregistered["grand_total"] == D("1000.00") and unregistered["total_cgst"] == D("0.00")
    zero = subs.compute(D("999.99"), D("0"), True, True)
    assert zero["grand_total"] == D("1000.00") and zero["round_off"] == D("0.01")
    odd = subs.compute(D("849.50"), D("18"), True, True)  # 849.50 + 76.455 + 76.455 -> half-up rupee
    assert odd["total_cgst"] == D("76.46") and odd["grand_total"] == D("1002.00")


def test_invoice_is_numbered_taxed_and_audited(app, db, tenant, firm, super_admin):
    _billable(db, tenant)
    invoice = subs.create_invoice(tenant, super_admin)
    db.session.commit()
    fy = invoice.number.split("/")[1]
    assert invoice.number == f"SUB/{fy}/0001" and len(invoice.number) <= 16
    assert invoice.place_of_supply == "27" and invoice.cgst == D("90.00") and invoice.grand_total == D("1180.00")
    assert invoice.period_start == tenant.next_billing_due
    assert invoice.bill_to["gstin"] == "27AAAAA0000A1Z5" and invoice.seller["gstin"] == "27ABCDE1234F1Z5"
    assert AuditLog.query.filter_by(action="platform_invoice_issued", entity_id=invoice.id).count() == 1
    with pytest.raises(subs.SubscriptionError):
        subs.create_invoice(tenant, super_admin)  # one live invoice per period


def test_out_of_state_client_gets_igst(db, tenant, firm, super_admin):
    gstin = tenant.gstins.first()
    gstin.gstin, gstin.state_code = "29AAAAA0000A1Z5", "29"
    _billable(db, tenant)
    invoice = subs.create_invoice(tenant, super_admin)
    assert invoice.igst == D("180.00") and invoice.cgst == D("0.00") and invoice.place_of_supply == "29"


def test_raise_due_only_picks_billable_subscribers_inside_the_window(db, tenant, firm, super_admin):
    far = Tenant(legal_name="Far Off", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
                 billing_cycle=BillingCycle.YEARLY, subscription_fee=D("5000"), next_billing_due=TODAY + timedelta(days=40))
    nofee = Tenant(legal_name="No Fee", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
                   billing_cycle=BillingCycle.MONTHLY, next_billing_due=TODAY)
    auditor = User(name="Office", email="office@example.com", role=UserRole.AUDITOR, billing_cycle=BillingCycle.MONTHLY,
                   subscription_fee=D("2500"), next_billing_due=TODAY + timedelta(days=1), must_change_password=False)
    auditor.set_password("AuditorSecret123")
    db.session.add_all([far, nofee, auditor])
    _billable(db, tenant)
    made = subs.raise_due(None)
    db.session.commit()
    assert {i.subscriber_name for i in made} == {"Acme Traders", "Office"}
    assert subs.raise_due(None) == []  # already invoiced
    office = next(i for i in made if i.auditor_id)
    assert office.grand_total == D("2950.00") and office.tenant_id is None


def test_paid_moves_the_cycle_once_through_mark_period_paid(app, client, db, tenant, firm, super_admin):
    _billable(db, tenant)
    due = tenant.next_billing_due
    invoice = subs.create_invoice(tenant, super_admin)
    db.session.commit()
    _login(client, "firm@example.com", "SuperSecret123")
    client.post(f"/admin/subscriptions/{invoice.id}/paid", data={"reference": "UPI 4411"})
    db.session.refresh(tenant)
    db.session.refresh(invoice)
    assert invoice.status == PlatformInvoiceStatus.PAID and invoice.payment_reference == "UPI 4411"
    from app.utils.billing import add_one_month

    assert tenant.next_billing_due == add_one_month(due)  # paid ahead of the due date still counts
    client.post(f"/admin/subscriptions/{invoice.id}/paid")  # twice
    client.post(f"/admin/clients/{tenant.id}/billing/mark-paid")  # and the old button
    db.session.refresh(tenant)
    assert tenant.next_billing_due == add_one_month(due)
    assert AuditLog.query.filter_by(action="platform_invoice_paid").count() == 1


def test_for_period_never_double_advances():
    t = Tenant(legal_name="X", billing_cycle=BillingCycle.MONTHLY, next_billing_due=date(2026, 11, 1))
    assert mark_period_paid(t, date(2026, 10, 25), for_period=date(2026, 11, 1)) == "advanced"
    assert t.next_billing_due == date(2026, 12, 1)
    assert mark_period_paid(t, date(2026, 10, 25), for_period=date(2026, 11, 1)) == "paid_ahead"
    assert t.next_billing_due == date(2026, 12, 1)
    # The plain button keeps its guards.
    assert mark_period_paid(t, date(2026, 10, 25)) == "paid_today"


def test_void_needs_a_reason_and_frees_the_period(client, db, tenant, firm, super_admin):
    _billable(db, tenant)
    invoice = subs.create_invoice(tenant, super_admin)
    db.session.commit()
    _login(client, "firm@example.com", "SuperSecret123")
    client.post(f"/admin/subscriptions/{invoice.id}/void", data={"reason": ""})
    db.session.refresh(invoice)
    assert invoice.status == PlatformInvoiceStatus.ISSUED
    client.post(f"/admin/subscriptions/{invoice.id}/void", data={"reason": "Wrong fee"})
    db.session.refresh(invoice)
    assert invoice.status == PlatformInvoiceStatus.VOID and invoice.void_reason == "Wrong fee"
    again = subs.create_invoice(tenant, super_admin)  # the period can be invoiced again
    assert again.number.endswith("/0002")  # never reuses the voided number
    assert PlatformInvoice.query.count() == 2


def test_client_sees_only_its_own_invoices_and_can_pay_by_upi(client, db, tenant, client_admin, firm, super_admin):
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
                   billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.flush()
    db.session.add(Gstin(tenant_id=other.id, gstin="29BBBBB0000B1Z5", state_code="29", state_name="Karnataka",
                         is_primary=True))
    _billable(db, tenant)
    _billable(db, other, fee="3000")
    mine = subs.create_invoice(tenant, super_admin)
    theirs = subs.create_invoice(other, super_admin)
    db.session.commit()
    _login(client, "admin@acme.example.com", "ClientSecret123")
    page = client.get("/admin/subscriptions/mine")
    assert mine.number.encode() in page.data and b"Other Co" not in page.data
    view = client.get(f"/admin/subscriptions/{mine.id}")
    assert view.status_code == 200 and b"upi://pay?pa=arfa%40okbank" in view.data and b"am=1180.00" in view.data
    assert client.get(f"/admin/subscriptions/{mine.id}.pdf").content_type == "application/pdf"
    # Guessing the other client's id: nothing.
    assert client.get(f"/admin/subscriptions/{theirs.id}").status_code == 404
    assert client.get(f"/admin/subscriptions/{theirs.id}.pdf").status_code == 404
    assert client.post(f"/admin/subscriptions/{theirs.id}/paid").status_code == 403
    assert client.get("/admin/subscriptions/").status_code == 403


def test_fee_and_profile_are_ultra_admin_only(client, db, tenant, client_admin, super_admin):
    _login(client, "firm@example.com", "SuperSecret123")
    client.post(f"/admin/subscriptions/fee/tenant/{tenant.id}", data={"fee": "1499"})
    db.session.refresh(tenant)
    assert tenant.subscription_fee == D("1499.00")
    res = client.post("/admin/subscriptions/profile", data={"legal_name": "ARFA", "state_code": "27",
                                                            "gstin": "29ABCDE1234F1Z5", "gst_rate": "18",
                                                            "invoice_prefix": "SUB", "days_before_due": "7"})
    assert b"first two digits" in res.data and PlatformProfile.query.count() == 0
    client.post("/admin/subscriptions/profile", data={"legal_name": "ARFA", "state_code": "27",
                                                      "gstin": "27ABCDE1234F1Z5", "gst_rate": "18",
                                                      "invoice_prefix": "SUB", "days_before_due": "7"})
    assert PlatformProfile.query.one().gstin == "27ABCDE1234F1Z5"
    client.get("/auth/logout")
    _login(client, "admin@acme.example.com", "ClientSecret123")
    assert client.post(f"/admin/subscriptions/fee/tenant/{tenant.id}", data={"fee": "1"}).status_code == 403


# --- admin drill-downs ---------------------------------------------------------------------------

def _invoice(db, tenant, client_admin):
    customer = Customer(tenant_id=tenant.id, name="Buyer", state_code="27", state_name="Maharashtra")
    db.session.add(customer)
    db.session.flush()
    invoice = create_invoice(tenant, client_admin, InvoiceInput(
        gstin_id=tenant.gstins.first().id, document_type=DocumentType.TAX_INVOICE, customer_id=customer.id,
        place_of_supply_state_code="27", invoice_date=TODAY, notes=None,
        lines=[LineInput(description="Biriyani", hsn_or_sac_code="996331", qty=D("2"), rate=D("150"),
                         discount_percent=D("0"), gst_rate=D("5"))]))
    db.session.commit()
    return invoice


def _bill(db, tenant, user):
    p = Product(tenant_id=tenant.id, name="Tea", hsn_or_sac_code="0902", gst_rate=D("5"), unit="NOS", sale_price=D("20"))
    db.session.add(p)
    db.session.commit()
    bill = checkout(tenant, user, CartInput(lines=[CartLineInput(description="Tea", hsn_or_sac_code="0902", qty=D("3"),
                                                                 rate=D("20"), gst_rate=D("5"), product_id=p.id)]),
                    PaymentMode.CASH)
    db.session.commit()
    return bill


def test_admin_drills_into_a_pos_bill_and_a_note(client, db, super_admin, tenant, client_admin):
    bill = _bill(db, tenant, client_admin)
    invoice = _invoice(db, tenant, client_admin)
    note = create_note(tenant, client_admin, NoteInput(original_invoice_id=invoice.id,
                                                       document_type=DocumentType.CREDIT_NOTE, note_date=TODAY,
                                                       reason="Returned", lines=[LineInput(
                                                           description="Biriyani", hsn_or_sac_code="996331", qty=D("1"),
                                                           rate=D("150"), discount_percent=D("0"), gst_rate=D("5"))]))
    db.session.commit()
    _login(client, "firm@example.com", "SuperSecret123")
    detail = client.get(f"/admin/clients/{tenant.id}")
    assert f"/admin/clients/{tenant.id}/pos-bills/{bill.id}".encode() in detail.data
    assert f"/admin/clients/{tenant.id}/notes/{note.id}".encode() in detail.data
    page = client.get(f"/admin/clients/{tenant.id}/pos-bills/{bill.id}")
    assert page.status_code == 200 and bill.bill_number.encode() in page.data and b"Tea" in page.data
    assert b"Void" not in page.data  # read-only
    assert client.get(f"/admin/clients/{tenant.id}/pos-bills/{bill.id}/pdf").content_type == "application/pdf"
    page = client.get(f"/admin/clients/{tenant.id}/notes/{note.id}")
    assert page.status_code == 200 and note.note_number.encode() in page.data and b"Returned" in page.data
    assert client.get(f"/admin/clients/{tenant.id}/notes/{note.id}/pdf").content_type == "application/pdf"
    assert AuditLog.query.filter_by(action="view_client_pos_bill").count() == 1
    assert AuditLog.query.filter_by(action="view_client_note").count() == 1


def test_drill_downs_are_scoped(client, db, super_admin, tenant, client_admin):
    bill = _bill(db, tenant, client_admin)
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
                   billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    auditor = User(name="Auditor", email="aud@example.com", role=UserRole.AUDITOR, billing_cycle=BillingCycle.MONTHLY,
                   must_change_password=False)
    auditor.set_password("AuditorSecret123")
    db.session.add(auditor)
    db.session.commit()
    _login(client, "firm@example.com", "SuperSecret123")
    # Another client's id with this bill's id: not found.
    assert client.get(f"/admin/clients/{other.id}/pos-bills/{bill.id}").status_code == 404
    client.get("/auth/logout")
    _login(client, "aud@example.com", "AuditorSecret123")  # an Auditor not allocated this client
    assert client.get(f"/admin/clients/{tenant.id}/pos-bills/{bill.id}").status_code in (403, 404)
    client.get("/auth/logout")
    _login(client, "admin@acme.example.com", "ClientSecret123")
    assert client.get(f"/admin/clients/{tenant.id}/pos-bills/{bill.id}").status_code == 403


# --- owner's day summary -------------------------------------------------------------------------

def test_owner_summary_page_whatsapp_and_dashboard(client, db, tenant, client_admin, staff):
    from app.inventory import ledger
    from app.inventory import services as inv
    from app.models.inventory import AdjustmentReason

    tenant.set_feature("daily_summary", True)
    tenant.set_feature("business_reports", True)
    tenant.set_feature("inventory", True)
    db.session.commit()
    bill = _bill(db, tenant, staff)  # 3 teas, stock never received -> below zero
    tea = bill.lines[0].product
    _login(client, "admin@acme.example.com", "ClientSecret123")
    page = client.get("/reports/owner-summary")
    assert page.status_code == 200
    text = page.data.decode()
    assert "Rs. 63.00" in text and "Cashier" in text and "Below zero" in text and "Tea" in text
    assert "https://wa.me/?text=" in text
    from app.finance.owner_summary import owner_summary, summary_text

    data = owner_summary(tenant)
    assert data["bill_count"] == 1 and data["net_sales"] == D("63.00")
    assert data["oversold"][0]["name"] == "Tea" and data["oversold"][0]["qty"] == D("-3")
    message = summary_text(tenant, data)
    assert "Sales: Rs. 63.00" in message and "Below zero: Tea (-3 NOS)" in message
    dash = client.get("/reports/dashboard")
    assert b"End of the day" in dash.data


def test_owner_summary_email_needs_smtp(app, client, db, tenant, client_admin):
    tenant.set_feature("daily_summary", True)
    db.session.commit()
    _login(client, "admin@acme.example.com", "ClientSecret123")
    assert b"Email it" not in client.get("/reports/owner-summary").data
    assert client.post("/reports/owner-summary/email").status_code == 404
    app.config.update(SMTP_HOST="smtp.example.com", SMTP_FROM="reports@example.com", SMTP_PORT=587,
                      SMTP_USERNAME="u", SMTP_PASSWORD="p", SMTP_USE_TLS=True)
    with mock.patch("smtplib.SMTP") as smtp:
        client.post("/reports/owner-summary/email")
    session = smtp.return_value.__enter__.return_value
    assert session.starttls.called and session.login.called
    sent = session.send_message.call_args[0][0]
    assert sent["To"] == "admin@acme.example.com" and "day summary" in sent["Subject"]
    assert AuditLog.query.filter_by(action="owner_summary_emailed").count() == 1


def test_owner_summary_is_hidden_with_the_flag_off(client, db, tenant, client_admin):
    tenant.set_feature("business_reports", True)
    db.session.commit()
    _login(client, "admin@acme.example.com", "ClientSecret123")
    assert client.get("/reports/owner-summary").status_code == 404
    assert b"End of the day" not in client.get("/reports/dashboard").data


# --- first-run checklist -------------------------------------------------------------------------

def test_new_client_admin_sees_the_checklist_and_it_ticks_itself(client, db, tenant, client_admin):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    page = client.get("/invoices/").data.decode()
    assert "Getting started - 1 of 6 done" in page  # the GSTIN is already there
    db.session.add(Product(tenant_id=tenant.id, name="Tea", hsn_or_sac_code="0902", gst_rate=D("5"), unit="NOS",
                           sale_price=D("20")))
    db.session.commit()
    assert "Getting started - 2 of 6 done" in client.get("/invoices/").data.decode()
    client.post("/settings/printer-test", data={"format": "3in"})
    assert client.get("/settings/printer-test.pdf?format=2in").content_type == "application/pdf"
    assert "Getting started - 3 of 6 done" in client.get("/invoices/").data.decode()
    client.post("/settings/first-run/dismiss")
    assert "Getting started" not in client.get("/invoices/").data.decode()
    db.session.refresh(tenant)
    assert tenant.first_run["dismissed"] is True


def test_existing_clients_never_see_it(client, db, tenant, client_admin):
    tenant.first_run = {"dismissed": True}  # what the migration set for every client that existed
    db.session.commit()
    _login(client, "admin@acme.example.com", "ClientSecret123")
    assert "Getting started" not in client.get("/invoices/").data.decode()


# --- monitoring ----------------------------------------------------------------------------------

def test_healthz_reports_its_checks(client):
    res = client.get("/healthz")
    assert res.status_code == 200 and res.get_json()["status"] == "ok"
    assert res.get_json()["checks"]["database"] == "ok"


def test_health_answers_on_both_paths(client):
    # /health is what the Cloud Run deploy checks (Cloud Run keeps paths
    # ending in "z" for itself); /healthz stays for the phones and Render.
    a, b = client.get("/healthz"), client.get("/health")
    assert a.status_code == b.status_code
    assert a.get_json()["status"] == b.get_json()["status"]


def test_healthz_is_503_when_the_database_is_down(app, client):
    with mock.patch("app.main.routes.db.session.execute", side_effect=RuntimeError("down")):
        res = client.get("/healthz")
    assert res.status_code == 503 and res.get_json()["checks"]["database"] == "error"


def test_sentry_only_with_a_dsn():
    from app import create_app
    from app.config import TestingConfig

    with mock.patch("sentry_sdk.init") as init:
        create_app(TestingConfig)
        assert not init.called

    class WithDsn(TestingConfig):
        SENTRY_DSN = "https://public@o0.ingest.sentry.io/0"

    with mock.patch("sentry_sdk.init") as init:
        create_app(WithDsn)
        assert init.called
        kwargs = init.call_args.kwargs
        assert kwargs["dsn"] == WithDsn.SENTRY_DSN and kwargs["send_default_pii"] is False
        event = kwargs["before_send"]({"user": {"id": 5, "email": "x@y.z"}}, {})
        assert event["user"] == {"id": 5}
