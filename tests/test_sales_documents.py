"""Phase 6 sales documents (quotation, proforma, sales order, delivery
challan) - numbering, one-click conversion to an invoice, cancel / return,
the GSTR-1 document summary - and recurring invoices (draft runs, issue,
skip, idempotent generation), void & re-issue, flags and tenant isolation."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.invoicing.services import LineInput
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.expense import Frequency
from app.models.invoice import Invoice, InvoiceStatus
from app.models.invoice_series import DocumentType
from app.models.sales import (
    ChallanPurpose,
    RecurringInvoice,
    RecurringInvoiceRun,
    RecurringRunStatus,
    SalesDocument,
    SalesDocumentKind,
    SalesDocumentStatus,
)
from app.models.tenant import BillingCycle, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.reports.gstr import gstr1_data
from app.sales import services as ss

D = Decimal
TODAY = date.today()


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _on(db, tenant, *keys):
    for key in keys:
        tenant.set_feature(key, True)
    db.session.commit()


@pytest.fixture()
def buyer(db, tenant):
    c = Customer(tenant_id=tenant.id, name="Ravi Stores", state_code="27", state_name="Maharashtra")
    db.session.add(c)
    db.session.commit()
    return c


def _line(rate="1000", gst="18", qty="2"):
    return LineInput(description="Steel rack", hsn_or_sac_code="9403", qty=D(qty), rate=D(rate),
                     discount_percent=D("10"), gst_rate=D(gst), unit="NOS")


def _doc(db, tenant, user, customer, kind=SalesDocumentKind.QUOTATION, pos="27", **kw):
    doc = ss.create_sales_document(tenant, user, ss.SalesDocInput(
        kind=kind, gstin_id=tenant.gstins.first().id, customer_id=customer.id, place_of_supply_state_code=pos,
        doc_date=TODAY, invoice_document_type=kw.pop("doc_type", DocumentType.TAX_INVOICE),
        lines=kw.pop("lines", [_line()]), **kw))
    db.session.commit()
    return doc


def test_each_kind_has_its_own_series(db, tenant, client_admin, buyer):
    q1 = _doc(db, tenant, client_admin, buyer)
    q2 = _doc(db, tenant, client_admin, buyer)
    pi = _doc(db, tenant, client_admin, buyer, kind=SalesDocumentKind.PROFORMA)
    so = _doc(db, tenant, client_admin, buyer, kind=SalesDocumentKind.SALES_ORDER)
    dc = _doc(db, tenant, client_admin, buyer, kind=SalesDocumentKind.DELIVERY_CHALLAN, challan_purpose=ChallanPurpose.JOB_WORK)
    assert q1.number.startswith("QTN/") and q2.number.endswith("0002")
    assert pi.number.startswith("PI/") and so.number.startswith("SO/") and dc.number.startswith("DC/")


def test_totals_match_the_invoice_it_becomes(db, tenant, client_admin, buyer):
    q = _doc(db, tenant, client_admin, buyer)  # 2 x 1000 less 10% = 1800 + 18% = 2124
    assert (q.total_taxable_value, q.total_cgst, q.total_sgst, q.grand_total) == (D("1800.00"), D("162.00"), D("162.00"), D("2124.00"))
    inv = ss.convert_to_invoice(tenant, client_admin, q)
    db.session.commit()
    assert inv.invoice_number.startswith("INV/") and inv.grand_total == q.grand_total
    assert inv.total_cgst == q.total_cgst and len(inv.lines) == 1
    assert q.status == SalesDocumentStatus.CONVERTED and q.converted_invoice_id == inv.id
    with pytest.raises(ss.SalesError):  # only once
        ss.convert_to_invoice(tenant, client_admin, q)


def test_inter_state_inclusive_and_zero_rated_lines(db, tenant, client_admin, buyer):
    incl = LineInput(description="Fan", hsn_or_sac_code="8414", qty=D("1"), rate=D("1180"), discount_percent=D("0"),
                     gst_rate=D("18"), unit="NOS", price_includes_tax=True)
    zero = LineInput(description="Book", hsn_or_sac_code="4901", qty=D("1"), rate=D("250"), discount_percent=D("0"),
                     gst_rate=D("0"), unit="NOS")
    so = _doc(db, tenant, client_admin, buyer, kind=SalesDocumentKind.SALES_ORDER, pos="29", lines=[incl, zero])
    assert so.total_igst == D("180.00") and so.total_cgst == 0 and so.total_taxable_value == D("1250.00")
    assert so.grand_total == D("1430.00")


def test_bill_of_supply_document_has_no_gst(db, tenant, client_admin, buyer):
    tenant.registration_type = RegistrationType.COMPOSITION
    db.session.commit()
    q = _doc(db, tenant, client_admin, buyer, doc_type=DocumentType.BILL_OF_SUPPLY)
    assert q.total_tax == 0 and q.grand_total == D("1800.00")
    with pytest.raises(ss.SalesError):  # composition can't promise a tax invoice
        _doc(db, tenant, client_admin, buyer, doc_type=DocumentType.TAX_INVOICE)


def test_challan_needs_purpose_moves_no_stock_and_can_come_back(db, tenant, client_admin, buyer):
    with pytest.raises(ss.SalesError):
        _doc(db, tenant, client_admin, buyer, kind=SalesDocumentKind.DELIVERY_CHALLAN)
    db.session.rollback()
    dc = _doc(db, tenant, client_admin, buyer, kind=SalesDocumentKind.DELIVERY_CHALLAN,
              challan_purpose=ChallanPurpose.SALE_ON_APPROVAL)
    ss.mark_returned(dc, client_admin, "Not approved")
    db.session.commit()
    assert dc.status == SalesDocumentStatus.RETURNED
    q = _doc(db, tenant, client_admin, buyer)
    with pytest.raises(ss.SalesError):
        ss.mark_returned(q, client_admin, "")
    with pytest.raises(ss.SalesError):
        ss.cancel_document(q, client_admin, " ")
    ss.cancel_document(q, client_admin, "Customer went elsewhere")
    db.session.commit()
    assert q.status == SalesDocumentStatus.CANCELLED and SalesDocument.query.count() == 2


def test_challans_in_gstr1_document_summary_share_the_dc_series(db, tenant, client_admin, buyer):
    gstin = tenant.gstins.first()
    period = TODAY.strftime("%Y-%m")
    dc = _doc(db, tenant, client_admin, buyer, kind=SalesDocumentKind.DELIVERY_CHALLAN, challan_purpose=ChallanPurpose.JOB_WORK)
    assert all(s.label != "Delivery Challan" for s in gstr1_data(tenant, gstin, period)["document_summary"])  # flag off
    _on(db, tenant, "sales_documents")
    q = _doc(db, tenant, client_admin, buyer)
    ss.cancel_document(dc, client_admin, "Wrong address")
    db.session.commit()
    summary = {s.label: s for s in gstr1_data(tenant, gstin, period)["document_summary"]}
    assert summary["Delivery Challan"].total_count == 1 and summary["Delivery Challan"].cancelled_count == 1
    assert "Quotation" not in summary  # not a GST document


# --- routes ---------------------------------------------------------------------------------

def _form(customer, tenant, **extra):
    return {"gstin_id": tenant.gstins.first().id, "document_type": "tax_invoice", "customer_id": str(customer.id),
            "place_of_supply_state_code": "27", "invoice_date": TODAY.isoformat(),
            "line_description[]": "Steel rack", "line_hsn[]": "9403", "line_qty[]": "2", "line_rate[]": "1000",
            "line_discount[]": "0", "line_gst_rate[]": "18", "line_product_id[]": "", "line_unit[]": "NOS", **extra}


def test_flag_off_is_404_and_hidden(db, client, tenant, client_admin, buyer):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    for url in ("/sales/documents", "/sales/documents/new?kind=quotation", "/sales/recurring", "/sales/recurring/new"):
        assert client.get(url).status_code == 404, url
    assert b"/sales/" not in client.get("/invoices/").data


def test_quote_form_then_one_click_invoice(db, client, tenant, client_admin, staff, buyer):
    _on(db, tenant, "sales_documents")
    _login(client, "cashier@acme.example.com", "CashierSecret123")
    assert client.get("/sales/documents").status_code == 403
    client.get("/auth/logout")
    _login(client, "admin@acme.example.com", "ClientSecret123")
    for kind in ("quotation", "proforma", "sales_order", "delivery_challan"):
        assert client.get(f"/sales/documents/new?kind={kind}").status_code == 200
    page = client.post("/sales/documents/new", data=_form(buyer, tenant, kind="quotation", valid_until=(TODAY + timedelta(days=15)).isoformat(),
                                                          terms="50% advance"), follow_redirects=True)
    assert b"Quotation QTN/" in page.data
    doc = SalesDocument.query.one()
    assert doc.terms == "50% advance" and doc.grand_total == D("2360.00")
    assert client.get(f"/sales/documents/{doc.id}/pdf").data[:4] == b"%PDF"
    page = client.post(f"/sales/documents/{doc.id}/convert", data={"invoice_date": TODAY.isoformat()}, follow_redirects=True)
    assert b"issued from QTN/" in page.data
    inv = Invoice.query.one()
    assert inv.grand_total == D("2360.00") and doc.status == SalesDocumentStatus.CONVERTED
    assert AuditLog.query.filter_by(action="sales_document_converted", entity_id=doc.id).count() == 1


def test_edit_and_invoice_links_the_document(db, client, tenant, client_admin, buyer):
    _on(db, tenant, "sales_documents")
    doc = _doc(db, tenant, client_admin, buyer, kind=SalesDocumentKind.SALES_ORDER)
    _login(client, "admin@acme.example.com", "ClientSecret123")
    page = client.get(f"/invoices/new?from_document={doc.id}")
    assert page.status_code == 200 and b"Steel rack" in page.data and doc.number.encode() in page.data
    client.post(f"/invoices/new?from_document={doc.id}", data=_form(buyer, tenant, from_document=doc.id, **{"line_rate[]": "900"}),
                follow_redirects=True)
    inv = Invoice.query.one()
    assert doc.status == SalesDocumentStatus.CONVERTED and doc.converted_invoice_id == inv.id


def test_void_and_reissue(db, client, tenant, client_admin, buyer):
    _on(db, tenant, "sales_documents")
    doc = _doc(db, tenant, client_admin, buyer)
    inv = ss.convert_to_invoice(tenant, client_admin, doc)
    db.session.commit()
    _login(client, "admin@acme.example.com", "ClientSecret123")
    res = client.post(f"/invoices/{inv.id}/void", data={"reason": "Wrong rate", "reissue": "1"})
    assert res.status_code == 302 and f"duplicate_from={inv.id}" in res.location
    assert db.session.get(Invoice, inv.id).status == InvoiceStatus.VOID


# --- recurring invoices ---------------------------------------------------------------------

def _recurring(db, tenant, user, customer, start, frequency=Frequency.MONTHLY, end=None):
    rec = ss.save_recurring(tenant, user, ss.RecurringInvoiceInput(
        name="AMC - Ravi", gstin_id=tenant.gstins.first().id, customer_id=customer.id,
        document_type=DocumentType.TAX_INVOICE, place_of_supply_state_code="27", frequency=frequency,
        start_date=start, end_date=end, lines=[_line(rate="5000", qty="1")]))
    db.session.commit()
    return rec


def test_recurring_drafts_are_idempotent_and_numberless(db, tenant, client_admin, buyer):
    start = TODAY - timedelta(days=70)
    rec = _recurring(db, tenant, client_admin, buyer, start)
    made = ss.generate_due_runs(tenant)
    db.session.commit()
    assert len(made) >= 2 and ss.generate_due_runs(tenant) == []
    assert Invoice.query.count() == 0  # nothing numbered yet
    runs = RecurringInvoiceRun.query.order_by(RecurringInvoiceRun.due_date).all()
    assert runs[0].due_date == start and rec.next_due_date > TODAY
    inv = ss.issue_run(tenant, client_admin, runs[0])
    ss.skip_run(runs[1], client_admin, "Paid in cash, billed separately")
    db.session.commit()
    assert inv.invoice_date == start and inv.grand_total == D("5310.00")
    assert runs[0].status == RecurringRunStatus.ISSUED and runs[1].status == RecurringRunStatus.SKIPPED
    with pytest.raises(ss.SalesError):
        ss.issue_run(tenant, client_admin, runs[1])


def test_recurring_end_date_and_month_end_anchor(db, tenant, client_admin, buyer):
    assert ss.next_due(date(2026, 1, 31), Frequency.MONTHLY, 31) == date(2026, 2, 28)
    assert ss.next_due(date(2026, 2, 28), Frequency.MONTHLY, 31) == date(2026, 3, 31)
    assert ss.next_due(date(2026, 1, 15), Frequency.QUARTERLY, 15) == date(2026, 4, 15)
    start = TODAY - timedelta(days=100)
    _recurring(db, tenant, client_admin, buyer, start, end=start + timedelta(days=5))
    assert len(ss.generate_due_runs(tenant)) == 1


def test_recurring_routes(db, client, tenant, client_admin, buyer):
    _on(db, tenant, "recurring_invoices")
    _login(client, "admin@acme.example.com", "ClientSecret123")
    assert client.get("/sales/recurring/new").status_code == 200
    page = client.post("/sales/recurring/new", data=_form(buyer, tenant, name="Rent - shop 4", frequency="monthly"),
                       follow_redirects=True)
    assert b"Rent - shop 4" in page.data
    run = RecurringInvoiceRun.query.one()
    assert client.get(f"/invoices/new?from_run={run.id}").status_code == 200
    client.post(f"/sales/recurring/runs/{run.id}/issue", follow_redirects=True)
    assert run.status == RecurringRunStatus.ISSUED and Invoice.query.count() == 1


# --- isolation --------------------------------------------------------------------------------

@pytest.fixture()
def other(db, super_admin):
    t = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
               billing_cycle=BillingCycle.MONTHLY)
    db.session.add(t)
    db.session.flush()
    db.session.add(Gstin(tenant_id=t.id, gstin="29BBBBB0000B1Z5", state_code="29", state_name="Karnataka", is_primary=True))
    admin = User(tenant_id=t.id, name="Other", email="other@x.example.com", role=UserRole.CLIENT_ADMIN, must_change_password=False)
    admin.set_password("OtherSecret123")
    db.session.add(admin)
    for key in ("sales_documents", "recurring_invoices"):
        t.set_feature(key, True)
    db.session.commit()
    return {"tenant": t, "admin": admin}


def test_tenant_isolation_for_sales_documents_and_recurring(db, client, tenant, client_admin, buyer, other):
    _on(db, tenant, "sales_documents", "recurring_invoices")
    doc = _doc(db, tenant, client_admin, buyer)
    rec = _recurring(db, tenant, client_admin, buyer, TODAY)
    ss.generate_due_runs(tenant)
    db.session.commit()
    run = RecurringInvoiceRun.query.one()
    ot = other["tenant"]
    with pytest.raises(ss.SalesError):  # our customer is not theirs
        ss.create_sales_document(ot, other["admin"], ss.SalesDocInput(
            kind=SalesDocumentKind.QUOTATION, gstin_id=ot.gstins.first().id, customer_id=buyer.id,
            place_of_supply_state_code="29", doc_date=TODAY, invoice_document_type=DocumentType.TAX_INVOICE, lines=[_line()]))
    _login(client, "other@x.example.com", "OtherSecret123")
    for url in (f"/sales/documents/{doc.id}", f"/sales/documents/{doc.id}/pdf", f"/sales/recurring/{rec.id}/edit"):
        assert client.get(url).status_code == 404, url
    for url in (f"/sales/documents/{doc.id}/convert", f"/sales/documents/{doc.id}/cancel", f"/sales/documents/{doc.id}/returned",
                f"/sales/recurring/{rec.id}/toggle", f"/sales/recurring/runs/{run.id}/issue", f"/sales/recurring/runs/{run.id}/skip"):
        assert client.post(url, data={"reason": "x"}).status_code == 404, url
    # Linking ids in the invoice form are ignored for another client's documents.
    page = client.get(f"/invoices/new?from_document={doc.id}&from_run={run.id}")
    assert doc.number.encode() not in page.data and b"Steel rack" not in page.data
    assert doc.status == SalesDocumentStatus.OPEN and run.status == RecurringRunStatus.PENDING and rec.is_active
    assert Invoice.query.count() == 0
    assert doc.number.encode() not in client.get("/sales/documents").data
