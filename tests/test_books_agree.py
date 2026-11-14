"""The test business with known answers.

One month of a small business - invoices, a credit and a debit note, counter
bills, a full refund, a goods return, a part refund of last month's bill and
stock sent to an owned branch - whose totals are worked out by hand below.
The dashboard, the Sales report, Profit & loss and the GST working papers
must each give exactly these figures, and the Books check
(app/finance/checks.py) must find that they agree. Then records are spoiled
on purpose and the Books check must notice.

Worked by hand (business in Maharashtra, 27):

                                        taxable     CGST     SGST      IGST      total
  A  invoice, Karnataka buyer, 18%    10,000.00                     1,800.00  11,800.00
  B  invoice, local buyer, 12%         1,000.00    60.00    60.00              1,120.00
     credit note on A, 18%            -1,000.00                      -180.00  -1,180.00
     debit note on B, 12%                100.00     6.00     6.00                112.00
  C  counter bill 3 x 150, 5%            450.00    11.25    11.25                473.00  (a)
  D  counter bill 1 x 200, 5%            200.00     5.00     5.00                210.00
     D refunded in full                 -200.00    -5.00    -5.00               -210.00
  E  counter bill 4 x 100, 5%            400.00    10.00    10.00                420.00
     1 of E's 4 returned                -100.00    -2.50    -2.50               -105.00
     half of F refunded (F: last month) -300.00    -7.50    -7.50               -315.00
     challan to own branch 25 x 100         not a sale - counted nowhere
                                      ---------  -------  -------  --------  ---------
  this month                          10,550.00    77.25    77.25  1,620.00  12,325.00

  (a) a counter bill is rounded to the rupee: 472.50 becomes 473.00, with 0.50 kept as its
      round-off. So the month's total is 0.50 more than taxable value plus tax.

  last month: F, counter bill 2 x 300, 5%  600.00  15.00  15.00           630.00
              (its refund belongs to this month, so last month stays 630.00)
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal as D

import pytest

from app.finance import checks
from app.finance import sales_figures as sf
from app.finance.dashboard import daily_sales, dashboard
from app.finance.pnl import places, revenue
from app.invoicing.services import InvoiceInput, LineInput, create_invoice
from app.models.customer import Customer
from app.models.invoice_series import DocumentType
from app.models.pos_bill import PaymentMode
from app.models.user import BranchType, User, UserRole
from app.notes.services import NoteInput, create_note
from app.pos.returns import create_return
from app.pos.services import CartInput, CartLineInput, checkout, refund_bill
from app.reports.gstr import gstr1_data, gstr3b_data
from app.reports.sales import sales_report_data

TODAY = date.today()
MONTH_START = TODAY.replace(day=1)
LAST_MONTH_END = MONTH_START - timedelta(days=1)
LAST_MONTH_START = LAST_MONTH_END.replace(day=1)
PERIOD = f"{TODAY.year:04d}-{TODAY.month:02d}"
LAST_PERIOD = f"{LAST_MONTH_END.year:04d}-{LAST_MONTH_END.month:02d}"

THIS_MONTH = {"taxable_value": D("10550.00"), "cgst": D("77.25"), "sgst": D("77.25"), "igst": D("1620.00"),
              "total": D("12325.00")}
LAST_MONTH_TOTAL = D("630.00")


def _line(description, qty, rate, gst):
    return LineInput(description=description, hsn_or_sac_code="996331", qty=D(qty), rate=D(rate),
                     discount_percent=D("0"), gst_rate=D(gst))


def _cart(description, qty, rate):
    return CartInput(lines=[CartLineInput(description=description, hsn_or_sac_code="996331", qty=D(qty), rate=D(rate),
                                          gst_rate=D("5"), unit="plate")])


@pytest.fixture()
def business(db, tenant, client_admin):
    """The month above, entered through the same services the screens use."""
    for flag in ("business_reports", "receivables", "payables", "cash_bank_book"):
        tenant.set_feature(flag, True)
    gstin = tenant.gstins.first()
    far = Customer(tenant_id=tenant.id, name="Bengaluru Caterers", gstin="29BBBBB0000B1Z5", state_code="29",
                   state_name="Karnataka")
    near = Customer(tenant_id=tenant.id, name="Pune Walk-in", state_code="27", state_name="Maharashtra")
    branch = User(tenant_id=tenant.id, name="Own branch", email="own@acme.example.com", role=UserRole.STAFF,
                  branch_type=BranchType.OWNED, must_change_password=False)
    branch.set_password("BranchSecret123")
    db.session.add_all([far, near, branch])
    db.session.commit()

    def invoice(customer, lines, pos, **kw):
        inv = create_invoice(tenant, client_admin, InvoiceInput(
            gstin_id=gstin.id, document_type=DocumentType.TAX_INVOICE, customer_id=customer.id if customer else None,
            place_of_supply_state_code=pos, invoice_date=TODAY, notes=None, lines=lines, **kw))
        db.session.commit()
        return inv

    a = invoice(far, [_line("Catering", "1", "10000", "18")], "29")
    b = invoice(near, [_line("Lunch boxes", "2", "500", "12")], "27")
    create_note(tenant, client_admin, NoteInput(
        original_invoice_id=a.id, document_type=DocumentType.CREDIT_NOTE, note_date=TODAY, reason="Shortfall",
        lines=[_line("Catering", "1", "1000", "18")]))
    create_note(tenant, client_admin, NoteInput(
        original_invoice_id=b.id, document_type=DocumentType.DEBIT_NOTE, note_date=TODAY, reason="Extra boxes",
        lines=[_line("Lunch boxes", "1", "100", "12")]))
    db.session.commit()

    c = checkout(tenant, client_admin, _cart("Biriyani", "3", "150"), PaymentMode.CASH)
    d = checkout(tenant, client_admin, _cart("Meals", "1", "200"), PaymentMode.CASH)
    e = checkout(tenant, client_admin, _cart("Tea", "4", "100"), PaymentMode.CASH)
    f = checkout(tenant, client_admin, _cart("Party pack", "2", "300"), PaymentMode.CASH)
    db.session.commit()
    # F was sold last month.
    f.completed_at = datetime.combine(LAST_MONTH_END.replace(day=15), time(13, 0)).astimezone()
    db.session.commit()

    refund_bill(d, client_admin, D("210.00"), "Order cancelled", PaymentMode.CASH)
    create_return(e, client_admin, {e.lines[0].id: D("1")}, "Spilled", PaymentMode.CASH)
    refund_bill(f, client_admin, D("315.00"), "Half the pack was short", PaymentMode.CASH)
    db.session.commit()

    # Stock sent to a branch the business owns: a delivery challan, not a sale.
    challan = create_invoice(tenant, client_admin, InvoiceInput(
        gstin_id=gstin.id, document_type=DocumentType.TAX_INVOICE, customer_id=None, branch_user_id=branch.id,
        place_of_supply_state_code="27", invoice_date=TODAY, notes=None, lines=[_line("Biriyani", "25", "100", "5")]))
    db.session.commit()
    assert challan.document_type == DocumentType.DELIVERY_CHALLAN and challan.grand_total == D("2500.00")
    return {"tenant": tenant, "gstin": gstin, "a": a, "b": b, "c": c, "d": d, "e": e, "f": f, "challan": challan,
            "admin": client_admin}


# --- every screen gives the hand-worked figures ------------------------------------------------------

def test_the_one_sales_figure(business):
    tenant = business["tenant"]
    assert sf.total(tenant, MONTH_START, TODAY) == THIS_MONTH["total"]
    assert sf.totals(sf.entries(tenant, MONTH_START, TODAY)) == THIS_MONTH
    assert sf.total(tenant, LAST_MONTH_START, LAST_MONTH_END) == LAST_MONTH_TOTAL


def test_the_dashboard(business):
    tenant = business["tenant"]
    data = dashboard(tenant, TODAY)
    assert data["sales_month"] == THIS_MONTH["total"]
    days = daily_sales(tenant, MONTH_START, TODAY)
    assert sum(v for _, v in days) == THIS_MONTH["total"]
    assert dict(days)[TODAY] == THIS_MONTH["total"]          # everything this month happened today


def test_the_sales_report(business):
    tenant = business["tenant"]
    report = sales_report_data(tenant, MONTH_START, TODAY)
    assert report["totals"] == THIS_MONTH
    assert report["invoice_count"] == 2 and report["note_count"] == 2 and report["pos_bill_count"] == 3
    kinds = sorted(r.doc_type for r in report["rows"])
    assert "Delivery Challan" not in kinds                      # stock to an own branch is not a sale
    assert kinds.count("POS Bill (refund)") == 2                # D in full, and half of last month's F
    assert all(MONTH_START <= r.doc_date <= TODAY for r in report["rows"])
    # Last month's report is not changed by this month's refund of F.
    last = sales_report_data(tenant, LAST_MONTH_START, LAST_MONTH_END)
    assert last["totals"]["total"] == LAST_MONTH_TOTAL and len(last["rows"]) == 1


def test_profit_and_loss_revenue(business):
    tenant = business["tenant"]
    assert revenue(tenant, MONTH_START, TODAY, None, places(tenant)).total == THIS_MONTH["taxable_value"]
    assert revenue(tenant, LAST_MONTH_START, LAST_MONTH_END, None, places(tenant)).total == D("600.00")


def test_gstr1_and_gstr3b(business):
    tenant, gstin = business["tenant"], business["gstin"]
    one = gstr1_data(tenant, gstin, PERIOD)["totals"]
    assert {k: one[k] for k in ("taxable_value", "cgst", "sgst", "igst")} == \
        {k: THIS_MONTH[k] for k in ("taxable_value", "cgst", "sgst", "igst")}
    three = gstr3b_data(tenant, gstin, PERIOD)
    assert three["outward_taxable"]["taxable_value"] == THIS_MONTH["taxable_value"]
    assert three["outward_taxable"]["igst"] == THIS_MONTH["igst"]
    assert three["outward_taxable"]["cgst"] == THIS_MONTH["cgst"]
    # The challan is still a numbered document of the month.
    summary = gstr1_data(tenant, gstin, PERIOD)["document_summary"]
    assert any(s.label == "Delivery Challan" and s.total_count == 1 for s in summary)
    # Last month, already filed perhaps: F in full, untouched by this month's refund.
    last = gstr1_data(tenant, gstin, LAST_PERIOD)["totals"]
    assert last["taxable_value"] == D("600.00") and last["cgst"] == D("15.00")


# --- and the Books check agrees ------------------------------------------------------------------------

def _statuses(report):
    return {c.key: c.status for c in report.checks}


def test_the_books_check_passes_on_good_books(business):
    tenant = business["tenant"]
    for period in (PERIOD, LAST_PERIOD):
        report = checks.run(tenant, period)
        bad = [(c.key, c.note, c.rows, c.expected, c.actual) for c in report.failed]
        assert report.ok, bad
    statuses = _statuses(checks.run(tenant, PERIOD))
    for key in ("sales_one_figure", "documents_add_up", "sales_vs_pnl", "sales_vs_gstr1", "gstr1_vs_gstr3b", "journal",
                "customers", "cash_and_bank"):
        assert statuses[key] == checks.OK, key
    assert statuses["stock_not_negative"] == checks.SKIP          # stock is not switched on here


def test_the_books_check_notices_a_spoiled_invoice(db, business):
    tenant, a = business["tenant"], business["a"]
    a.total_taxable_value = D("9000.00")             # the top of the invoice no longer matches its lines
    db.session.commit()
    report = checks.run(tenant, PERIOD)
    statuses = _statuses(report)
    assert statuses["documents_add_up"] == checks.FAIL
    assert statuses["sales_vs_gstr1"] == checks.FAIL             # GSTR-1 adds the lines, the report the top
    assert {c.key for c in checks.blocking_failures(report)} >= {"documents_add_up", "sales_vs_gstr1"}
    add_up = next(c for c in report.checks if c.key == "documents_add_up")
    assert a.invoice_number in add_up.rows[0]["label"] and "9000.00" in add_up.rows[0]["label"]


def test_the_books_check_notices_cash_below_zero(db, business):
    tenant = business["tenant"]
    tenant.books_opening_date = MONTH_START
    tenant.cash_opening_balance = D("-50000")
    db.session.commit()
    cash = next(c for c in checks.run(tenant, PERIOD).checks if c.key == "cash_and_bank")
    assert cash.status == checks.FAIL and "below zero" in cash.rows[0]["label"]
    assert not cash.blocks_filing


def test_a_check_that_cannot_run_is_reported_not_hidden(business, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no")

    monkeypatch.setattr(checks, "check_journal", boom)
    report = checks.run(business["tenant"], PERIOD)
    assert not report.ok
    broken = [c for c in report.checks if c.status == checks.ERROR]
    assert len(broken) == 1 and len(report.checks) == 12


def test_results_are_kept_and_the_latest_is_shown(db, business):
    tenant = business["tenant"]
    row = checks.save(tenant, checks.run(tenant, PERIOD))
    db.session.commit()
    assert row.ok and row.failed_count == 0 and len(row.results) == 12
    assert checks.latest(tenant, PERIOD).id == row.id
    assert checks.failing_now(tenant) == []
    business["a"].total_taxable_value = D("1.00")
    db.session.commit()
    bad = checks.save(tenant, checks.run(tenant, PERIOD))
    db.session.commit()
    assert checks.latest(tenant, PERIOD).id == bad.id and [r.id for r in checks.failing_now(tenant)] == [bad.id]


# --- the page, the bell and "ready to file" -------------------------------------------------------------

def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def test_the_books_check_page(client, db, business):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    page = client.get(f"/reports/books-check?period={PERIOD}").get_data(as_text=True)
    assert "The figures agree" in page and "Sales and GSTR-1" in page and "Rs. 10,550.00" in page
    assert "Books check" in client.get("/reports/").get_data(as_text=True)
    business["a"].total_taxable_value = D("9000.00")
    db.session.commit()
    # The kept result still shows until the owner checks again.
    assert "The figures agree" in client.get(f"/reports/books-check?period={PERIOD}").get_data(as_text=True)
    page = client.post("/reports/books-check", data={"period": PERIOD}, follow_redirects=True).get_data(as_text=True)
    assert "checks need a look" in page and "Stops filing" in page and business["a"].invoice_number in page
    assert "don&#39;t agree" in client.get("/needs-you").get_data(as_text=True) \
        or "don't agree" in client.get("/needs-you").get_data(as_text=True)


def test_a_month_that_does_not_agree_cannot_be_marked_ready_without_a_reason(client, db, business):
    from app.models.audit_log import AuditLog
    from app.models.filing import FilingStatus

    tenant, gstin = business["tenant"], business["gstin"]
    _login(client, "admin@acme.example.com", "ClientSecret123")
    form = {"gstin_id": gstin.id, "period": PERIOD, "action": "ready"}
    # Good books: straight through.
    client.post("/reports/filing", data=form)
    assert all(s.data_ready for s in FilingStatus.query.filter_by(tenant_id=tenant.id, period=PERIOD))
    client.post("/reports/filing", data={**form, "action": "not_ready"})

    business["a"].total_taxable_value = D("9000.00")
    db.session.commit()
    resp = client.post("/reports/filing", data=form)
    assert resp.status_code == 302 and "/reports/books-check" in resp.headers["Location"]
    assert not any(s.data_ready for s in FilingStatus.query.filter_by(tenant_id=tenant.id, period=PERIOD))
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Mark" in page and "ready all the same" in page and 'name="explanation"' in page
    # Too short to be a reason.
    client.post("/reports/filing", data={**form, "explanation": "ok"})
    assert not any(s.data_ready for s in FilingStatus.query.filter_by(tenant_id=tenant.id, period=PERIOD))
    client.post("/reports/filing", data={**form, "explanation": "Invoice total corrected by hand, auditor informed"})
    assert all(s.data_ready for s in FilingStatus.query.filter_by(tenant_id=tenant.id, period=PERIOD))
    log = AuditLog.query.filter_by(action="filing_data_ready").order_by(AuditLog.id.desc()).first()
    assert "auditor informed" in log.details["explanation"] and "documents_add_up" in log.details["books_check_failed"]


def test_the_nightly_command_keeps_results_and_logs_a_failure(app, db, business, caplog):
    import logging

    from app.models.filing import BooksCheckRun

    runner = app.test_cli_runner()
    out = runner.invoke(args=["books-check"])
    assert out.exit_code == 0 and "2 month(s) checked, 0 with figures that don't agree" in out.output
    assert BooksCheckRun.query.count() == 2
    business["a"].total_taxable_value = D("9000.00")
    db.session.commit()
    with caplog.at_level(logging.ERROR, logger="arfa.books"):
        out = runner.invoke(args=["books-check", "--tenant-id", str(business["tenant"].id)])
    assert "1 with figures that don't agree" in out.output
    assert any("books check failed" in r.getMessage() and PERIOD in r.getMessage() for r in caplog.records)
