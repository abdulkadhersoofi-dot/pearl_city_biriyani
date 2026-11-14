"""The invoice number on the new-invoice page: the series' next number
shown live, typing a number of your own for one invoice, and setting a
series' prefix and next number (as Zoho Books does)."""

from app.models.invoice import Invoice
from app.models.invoice_series import DocumentType, InvoiceSeries
from app.models.audit_log import AuditLog
from tests.test_invoice_form import _login, _new_invoice_form_data

FY = "2026-27"


def _admin(client, client_admin):
    _login(client, client_admin.email, "ClientSecret123")


def _peek(client, tenant, **extra):
    gstin = tenant.gstins.first()
    query = {"gstin_id": gstin.id, "document_type": "tax_invoice", "date": "2026-10-05", **extra}
    return client.get("/invoices/next-number", query_string=query)


def _save(client, tenant, prefix, next_number, **extra):
    gstin = tenant.gstins.first()
    return client.post("/invoices/numbering", json={"gstin_id": str(gstin.id), "document_type": "tax_invoice",
                                                     "date": "2026-10-05", "prefix": prefix,
                                                     "next": str(next_number), **extra})


def _make(client, tenant, **overrides):
    return client.post("/invoices/new", data=_new_invoice_form_data(tenant, **overrides), follow_redirects=True)


def test_the_page_shows_the_next_number_without_taking_it(client, db, tenant, client_admin):
    _admin(client, client_admin)
    page = client.get("/invoices/new").get_data(as_text=True)
    assert 'name="invoice_number"' in page and "/invoices/next-number" in page and "Invoice numbering" in page
    body = _peek(client, tenant).get_json()
    assert body["number"] == f"INV/{FY}/0001" and body["prefix"] == "INV" and body["next"] == 1
    assert body["label"] == "Tax Invoice" and body["fy"] == FY
    assert InvoiceSeries.query.count() == 0                      # looking changes nothing
    assert _peek(client, tenant, date="2027-04-02").get_json()["number"] == "INV/2027-28/0001"
    assert _peek(client, tenant, document_type="delivery_challan").get_json()["number"] == f"DC/{FY}/0001"


def test_left_as_suggested_the_series_gives_the_number(client, db, tenant, client_admin):
    _admin(client, client_admin)
    _make(client, tenant, invoice_number=f"INV/{FY}/0001", auto_number=f"INV/{FY}/0001")
    _make(client, tenant, invoice_number="", auto_number="")
    assert [i.invoice_number for i in Invoice.query.order_by(Invoice.id)] == [f"INV/{FY}/0001", f"INV/{FY}/0002"]
    assert _peek(client, tenant).get_json()["number"] == f"INV/{FY}/0003"


def test_a_number_of_your_own_for_one_invoice(client, db, tenant, client_admin):
    _admin(client, client_admin)
    page = _make(client, tenant, invoice_number="SPL-7", auto_number=f"INV/{FY}/0001").get_data(as_text=True)
    assert "Invoice SPL-7 created" in page
    invoice = Invoice.query.one()
    assert invoice.invoice_number == "SPL-7"
    assert AuditLog.query.filter_by(action="invoice_number_typed").count() == 1
    assert _peek(client, tenant).get_json()["number"] == f"INV/{FY}/0001"   # the series is untouched


def test_a_number_already_used_or_not_gst_shaped_is_refused(client, db, tenant, client_admin):
    _admin(client, client_admin)
    _make(client, tenant, invoice_number="SPL-7", auto_number="x")
    page = _make(client, tenant, invoice_number="SPL-7", auto_number="x").get_data(as_text=True)
    assert "already in your books" in page and 'value="SPL-7"' in page    # kept in the box to fix
    for bad in ("A NUMBER WITH SPACES", "12345678901234567", "INV#1"):
        page = _make(client, tenant, invoice_number=bad, auto_number="x").get_data(as_text=True)
        assert "at most 16 characters" in page
    assert Invoice.query.count() == 1


def test_a_typed_number_ahead_in_the_series_moves_the_series_on(client, db, tenant, client_admin):
    _admin(client, client_admin)
    _make(client, tenant, invoice_number=f"INV/{FY}/0050", auto_number=f"INV/{FY}/0001")
    _make(client, tenant)
    assert [i.invoice_number for i in Invoice.query.order_by(Invoice.id)] == [f"INV/{FY}/0050", f"INV/{FY}/0051"]


def test_the_owner_sets_the_prefix_and_where_the_series_carries_on(client, db, tenant, client_admin):
    _admin(client, client_admin)
    res = _save(client, tenant, "PCB", 120)
    assert res.status_code == 200 and res.get_json()["number"] == f"PCB/{FY}/0120"
    _make(client, tenant)
    _make(client, tenant)
    assert [i.invoice_number for i in Invoice.query.order_by(Invoice.id)] == [f"PCB/{FY}/0120", f"PCB/{FY}/0121"]
    log = AuditLog.query.filter_by(action="invoice_numbering_changed").one()
    assert log.details["before"] == {"prefix": "INV", "next": 1} and log.details["after"] == {"prefix": "PCB", "next": 120}


def test_the_series_never_goes_back_over_a_number_given_out(client, db, tenant, client_admin):
    _admin(client, client_admin)
    _save(client, tenant, "INV", 10)
    _make(client, tenant)                                          # INV/2026-27/0010
    res = _save(client, tenant, "INV", 5)
    assert res.status_code == 400 and "at least 11" in res.get_json()["error"]
    assert _save(client, tenant, "NEW", 1).status_code == 200      # a fresh prefix may start at 1
    assert _save(client, tenant, "TOOLONGPRE", 1).status_code == 400   # TOOLONGPRE/2026-27/0001 is over 16
    assert "16 characters" in _save(client, tenant, "TOOLONGPRE", 1).get_json()["error"]
    assert _save(client, tenant, "A/B", 1).status_code == 400
    assert _save(client, tenant, "INV", 0).status_code == 400


def test_only_the_owner_and_only_their_own_gstin(client, db, tenant, client_admin, staff):
    _login(client, staff.email, "CashierSecret123")
    assert _peek(client, tenant).status_code in (302, 403)
    assert _save(client, tenant, "X", 1).status_code in (302, 403)
    client.get("/auth/logout")
    _admin(client, client_admin)
    assert client.get("/invoices/next-number", query_string={"gstin_id": 999999, "document_type": "tax_invoice"}
                      ).status_code == 404
    assert _peek(client, tenant, document_type="purchase_bill").status_code == 404
    assert InvoiceSeries.query.filter_by(document_type=DocumentType.TAX_INVOICE).count() == 0
