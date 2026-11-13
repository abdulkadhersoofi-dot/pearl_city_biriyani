"""The Books check: do the figures agree with each other, and with the
records behind them?

Each check works a figure out two ways that should give the same answer -
the dashboard against the Sales report, the Sales report against the GST
working papers, a customer's ledger against their unpaid bills - and says
"agrees" or shows the difference and the records that cause it. Nothing
here changes any record.

    run(tenant, "2026-11")            every check for one month
    save(tenant, report)              keep the result (BooksCheckRun)
    latest(tenant, period)            the last kept result
    blocking_failures(report)         the failed checks that stop a month
                                      being marked ready to file

The nightly job (flask books-check) runs it for every business, for this
month and last; a failure is logged as an error (an alert, see
scripts/gcp/monitoring_setup.sh) and shows under "Needs you".

A check that cannot give a fair answer for a business says so ("not
checked: ...") instead of guessing - a margin-scheme sale's taxable value
is its margin, for one, so the before-tax comparisons skip it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from flask import url_for

from app.extensions import db
from app.finance import sales_figures as sf
from app.models.mixins import utcnow

log = logging.getLogger("arfa.books")

ZERO = Decimal("0.00")
OK, FAIL, SKIP, ERROR = "ok", "fail", "skip", "error"
MAX_ROWS = 25            # records listed under a failed check
ROUND_OFF_LIMIT = Decimal("1.00")


@dataclass
class Check:
    key: str
    title: str
    what: str                       # what must be true, in plain words
    status: str = OK
    expected: Decimal | None = None
    actual: Decimal | None = None
    note: str = ""                  # why it was skipped, or what the difference means
    rows: list[dict] = field(default_factory=list)   # {"label", "amount", "url"}
    blocks_filing: bool = False
    labels: tuple[str, str] = ("Should be", "Is")

    @property
    def difference(self) -> Decimal | None:
        if self.expected is None or self.actual is None:
            return None
        return self.actual - self.expected

    def compare(self, expected, actual, tolerance=ZERO):
        self.expected, self.actual = sf.money(expected), sf.money(actual)
        if abs(self.actual - self.expected) > tolerance:
            self.status = FAIL
        return self

    def add(self, label, amount=None, url=None):
        self.status = FAIL
        if len(self.rows) < MAX_ROWS:
            self.rows.append({"label": label, "amount": None if amount is None else str(sf.money(amount)), "url": url})
        return self

    def skip(self, why):
        self.status, self.note = SKIP, why
        return self

    def as_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "what": self.what, "status": self.status,
                "expected": None if self.expected is None else str(self.expected),
                "actual": None if self.actual is None else str(self.actual),
                "difference": None if self.difference is None else str(self.difference),
                "note": self.note, "rows": self.rows, "blocks_filing": self.blocks_filing,
                "labels": list(self.labels)}


@dataclass
class Report:
    period: str
    start: date
    end: date
    checks: list[Check]
    ran_at: object = None

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status in (FAIL, ERROR)]

    @property
    def ok(self) -> bool:
        return not self.failed


def _url(endpoint, **kwargs):
    try:
        return url_for(endpoint, **kwargs)
    except Exception:  # noqa: BLE001 - no request context (the nightly job): no link
        return None


def _margin(entries) -> bool:
    from app.models.invoice_series import DocumentType

    for e in entries:
        doc = e.doc
        if getattr(doc, "is_margin_scheme", False) or getattr(doc, "document_type", None) == DocumentType.MARGIN_SCHEME_INVOICE:
            return True
        bill = getattr(doc, "bill", None)
        if bill is not None and getattr(bill, "is_margin_scheme", False):
            return True
    return False


# --- sales ------------------------------------------------------------------------------------------

def check_sales_one_figure(tenant, start, end, entries) -> Check:
    c = Check("sales_one_figure", "Sales: dashboard, Sales report and daily figures",
              "The dashboard's sales for the month, the Sales report's total and the days added up are the same "
              "amount.", blocks_filing=True, labels=("Sales report total", "Dashboard"))
    from app.finance.dashboard import _sales, daily_sales
    from app.reports.sales import sales_report_data

    report_total = sales_report_data(tenant, start, end)["totals"]["total"]
    dashboard_total = _sales(tenant, start, end)
    c.compare(report_total, dashboard_total)
    by_day = sum((v for _, v in daily_sales(tenant, start, end)), ZERO)
    if sf.money(by_day) != sf.money(report_total):
        c.add("The days of the month added up", by_day, _url("reports.sales_view"))
    return c


def check_sales_vs_pnl(tenant, start, end, entries) -> Check:
    c = Check("sales_vs_pnl", "Sales before tax and Profit & loss revenue",
              "Revenue in Profit & loss is the Sales report's total before tax.",
              labels=("Sales report, before tax", "Profit & loss revenue"))
    if not tenant.has_feature("business_reports"):
        return c.skip("Profit & loss is not switched on.")
    if _margin(entries):
        return c.skip("This month has margin-scheme sales: their revenue is the selling price, which the Sales "
                      "report's taxable value (the margin) does not show.")
    from app.finance.pnl import places, revenue

    c.compare(sum((e.signed_taxable for e in entries), ZERO), revenue(tenant, start, end, None, places(tenant)).total)
    return c


def _regular(tenant) -> bool:
    from app.models.tenant import RegistrationType

    return tenant.registration_type == RegistrationType.REGULAR


def check_sales_vs_gstr1(tenant, period, entries) -> Check:
    from app.models.invoice_series import DocumentType
    from app.reports.gstr import ReportPeriodError, gstr1_data

    c = Check("sales_vs_gstr1", "Sales and GSTR-1",
              "For each GSTIN, the taxable value and the tax in GSTR-1 are those of the same documents as the Sales "
              "report (bills of supply aside - they are not reported there).", blocks_filing=True,
              labels=("Sales documents, value and tax", "GSTR-1, value and tax"))
    if not _regular(tenant):
        return c.skip("GSTR-1 is for regular GST registrations.")
    if _margin(entries):
        return c.skip("This month has margin-scheme sales, which GSTR-1 reports on the margin.")
    part_refunds = sum(1 for e in entries if e.kind == sf.POS_REFUND and e.ratio != 1)
    # A part refund is spread over the bill's lines in GSTR-1 and rounded once
    # here: a few paise either way.
    tolerance = Decimal("0.05") * part_refunds
    expected_total, actual_total = ZERO, ZERO
    for gstin in tenant.gstins.filter_by(is_active=True):
        mine = [e for e in entries if e.doc.gstin_id == gstin.id
                and not (e.kind == sf.INVOICE and e.doc.document_type == DocumentType.BILL_OF_SUPPLY)]
        want = sf.totals(mine)
        try:
            got = gstr1_data(tenant, gstin, period)["totals"]
        except ReportPeriodError as exc:
            return c.skip(str(exc))
        for key, label in (("taxable_value", "taxable value"), ("cgst", "CGST"), ("sgst", "SGST"), ("igst", "IGST")):
            expected_total += want[key]
            actual_total += got[key]
            if abs(sf.money(got[key]) - sf.money(want[key])) > tolerance:
                c.add(f"{gstin.gstin}: {label} in GSTR-1 is {sf.money(got[key])}, the documents add up to "
                      f"{sf.money(want[key])}", got[key] - want[key],
                      _url("reports.gstr1_view", period=period, gstin_id=gstin.id))
    c.expected, c.actual = sf.money(expected_total), sf.money(actual_total)
    return c


def check_gstr1_vs_gstr3b(tenant, period, entries) -> Check:
    from app.reports.gstr import ReportPeriodError, gstr1_data, gstr3b_data

    c = Check("gstr1_vs_gstr3b", "GSTR-1 and GSTR-3B",
              "The outward supplies in GSTR-3B (taxable, zero-rated and nil-rated together, before advances) are "
              "the taxable value in GSTR-1.", blocks_filing=True, labels=("GSTR-1", "GSTR-3B"))
    if not _regular(tenant):
        return c.skip("GSTR-3B is for regular GST registrations.")
    if _margin(entries):
        return c.skip("This month has margin-scheme sales, which GSTR-3B shows partly as exempt.")
    expected_total, actual_total = ZERO, ZERO
    for gstin in tenant.gstins.filter_by(is_active=True):
        try:
            one, three = gstr1_data(tenant, gstin, period), gstr3b_data(tenant, gstin, period)
        except ReportPeriodError as exc:
            return c.skip(str(exc))
        outward = sum((three[k]["taxable_value"] for k in ("outward_taxable", "outward_zero_rated",
                                                            "outward_nil_rated")), ZERO)
        if three.get("advances"):
            outward -= three["advances"]["net"]["taxable_value"]
        expected_total += one["totals"]["taxable_value"]
        actual_total += outward
        if sf.money(outward) != sf.money(one["totals"]["taxable_value"]):
            c.add(f"{gstin.gstin}: GSTR-3B shows {sf.money(outward)}, GSTR-1 shows "
                  f"{sf.money(one['totals']['taxable_value'])}", outward - one["totals"]["taxable_value"],
                  _url("reports.gstr3b_view", period=period, gstin_id=gstin.id))
    c.expected, c.actual = sf.money(expected_total), sf.money(actual_total)
    return c


def check_journal(tenant, start, end, entries) -> Check:
    """The accounting journal (what the Tally export is built from) is a
    third, separate reading of the same documents."""
    from app.finance.journal import ROUND_OFF, build

    c = Check("journal", "Accounting journal (Tally export)",
              "The output GST in the journal is the tax on the month's sales documents, and no voucher needs more "
              f"than Rs. {ROUND_OFF_LIMIT} of rounding to balance.", labels=("Tax on sales documents", "Journal"))
    _ledgers, vouchers = build(tenant, start, end)
    journal_tax = ZERO
    for v in vouchers:
        for e in v.entries:
            if e.ledger.startswith("Output "):
                journal_tax += e.credit - e.debit
        rounding = sum((e.debit + e.credit for e in v.entries if e.ledger == ROUND_OFF), ZERO)
        doc_round_off = ROUND_OFF_LIMIT
        if rounding > doc_round_off:
            c.add(f"{v.vch_type} {v.number} on {v.on:%d %b %Y} needs Rs. {rounding} to balance", rounding)
    documents_tax = ZERO
    for e in entries:
        doc = e.doc
        reverse_charge = ZERO
        if e.kind == sf.INVOICE:
            reverse_charge = (Decimal(doc.total_rcgst or 0) + Decimal(doc.total_rsgst or 0)
                              + Decimal(doc.total_rigst or 0))
        documents_tax += e.sign * (e.tax - reverse_charge)
    # Each refund's tax is rounded per tax head in the journal.
    refunds = sum(1 for e in entries if e.kind == sf.POS_REFUND and e.ratio != 1)
    if abs(sf.money(journal_tax) - sf.money(documents_tax)) > Decimal("0.03") * refunds:
        c.status = FAIL
    c.expected, c.actual = sf.money(documents_tax), sf.money(journal_tax)
    return c


# --- documents --------------------------------------------------------------------------------------

def _sums(lines) -> dict:
    out = {"taxable": ZERO, "cgst": ZERO, "sgst": ZERO, "igst": ZERO}
    for l in lines:
        out["taxable"] += Decimal(l.taxable_value or 0)
        out["cgst"] += Decimal(l.cgst_amount or 0) + Decimal(getattr(l, "rcgst_amount", 0) or 0)
        out["sgst"] += Decimal(l.sgst_amount or 0) + Decimal(getattr(l, "rsgst_amount", 0) or 0)
        out["igst"] += Decimal(l.igst_amount or 0) + Decimal(getattr(l, "rigst_amount", 0) or 0)
    return out


def check_documents_add_up(tenant, start, end, entries) -> Check:
    c = Check("documents_add_up", "Each sales document adds up",
              "On every invoice, counter bill, return and credit or debit note, the taxable value and the GST at the "
              "top are the lines added up.", blocks_filing=True)
    seen = set()
    for e in entries:
        if e.kind == sf.POS_REFUND:
            continue                    # the same bill as its sale
        doc = e.doc
        key = (e.kind, doc.id)
        if key in seen:
            continue
        seen.add(key)
        got = _sums(doc.lines)
        head = {"taxable": Decimal(doc.total_taxable_value), "cgst": Decimal(doc.total_cgst),
                "sgst": Decimal(doc.total_sgst), "igst": Decimal(doc.total_igst)}
        if e.kind == sf.INVOICE:
            head["cgst"] += Decimal(doc.total_rcgst or 0)
            head["sgst"] += Decimal(doc.total_rsgst or 0)
            head["igst"] += Decimal(doc.total_rigst or 0)
        wrong = [k for k in head if sf.money(head[k]) != sf.money(got[k])]
        if wrong:
            number = getattr(doc, "invoice_number", None) or getattr(doc, "bill_number", None) \
                or getattr(doc, "note_number", None) or getattr(doc, "return_number", None) or f"#{doc.id}"
            url = {sf.INVOICE: _url("invoicing.view_invoice", invoice_id=doc.id),
                   sf.CREDIT_NOTE: _url("notes.view_note", note_id=doc.id),
                   sf.DEBIT_NOTE: _url("notes.view_note", note_id=doc.id)}.get(e.kind)
            first = wrong[0]
            c.add(f"{number}: {first} at the top is {sf.money(head[first])}, the lines add up to "
                  f"{sf.money(got[first])}", head[first] - got[first], url)
    return c


# --- stock ------------------------------------------------------------------------------------------

def check_stock_not_negative(tenant, start, end, entries) -> Check:
    c = Check("stock_not_negative", "No stock below zero",
              "No item shows less than nothing at any place: more went out than ever came in.")
    if not tenant.has_feature("inventory"):
        return c.skip("Stock is not switched on.")
    from app.inventory.ledger import stock_by_location
    from app.models.inventory import StockLocation
    from app.models.product import Product

    negative = {k: q for k, q in stock_by_location(tenant.id).items() if q < 0}
    if not negative:
        return c
    products = {p.id: p for p in Product.query.filter(Product.id.in_({k[0] for k in negative}))}
    places = {l.id: l for l in StockLocation.query.filter(StockLocation.id.in_({k[1] for k in negative}))}
    for (pid, lid), qty in sorted(negative.items(), key=lambda kv: kv[1]):
        name = products[pid].name if pid in products else f"item {pid}"
        where = places[lid].name if lid in places else f"place {lid}"
        c.add(f"{name} at {where}: {qty.normalize():f}", None, _url("inventory.item_ledger", product_id=pid))
    c.note = f"{len(negative)} item(s) below zero."
    return c


def check_serials_match_stock(tenant, start, end, entries) -> Check:
    c = Check("serials_match_stock", "Serial numbers and stock quantity",
              "For every serial-numbered item, the units in stock are the serial numbers in stock (those held for "
              "an order at the warehouse included).")
    if not tenant.has_feature("inventory"):
        return c.skip("Stock is not switched on.")
    from app.inventory.ledger import stock_on_hand
    from app.models.inventory import ProductSerial, SerialStatus
    from app.models.product import Product

    tracked = Product.query.filter_by(tenant_id=tenant.id, track_serial=True).all()
    if not tracked:
        return c.skip("No serial-numbered items.")
    on_hand = stock_on_hand(tenant.id, [p.id for p in tracked])
    counts = dict(db.session.query(ProductSerial.product_id, db.func.count(ProductSerial.id)).filter(
        ProductSerial.tenant_id == tenant.id,
        ProductSerial.status.in_([SerialStatus.IN_STOCK, SerialStatus.PICKED])).group_by(ProductSerial.product_id))
    wrong = 0
    for p in tracked:
        qty, serials = Decimal(on_hand.get(p.id, 0)), Decimal(counts.get(p.id, 0))
        if qty != serials:
            wrong += 1
            c.add(f"{p.name}: {qty.normalize():f} in stock, {serials.normalize():f} serial number(s)", None,
                  _url("inventory.item_ledger", product_id=p.id))
    if wrong:
        c.note = (f"{wrong} item(s) differ. Stock taken in without its serial numbers (opening stock, a bill typed "
                  "without them) shows here until they are added.")
    return c


# --- money ------------------------------------------------------------------------------------------

def _accounts_check(c: Check, parties, account_of, url_of) -> Check:
    total_ledger, total_items = ZERO, ZERO
    for party in parties:
        account = account_of(party)
        if not account.entries and not account.items and not account.on_account:
            continue
        ledger = account.entries[-1].balance if account.entries else ZERO
        total_ledger += ledger
        total_items += account.balance
        if sf.money(ledger) != sf.money(account.balance):
            c.add(f"{party.name}: ledger closes at {sf.money(ledger)}, unpaid bills less money on account come to "
                  f"{sf.money(account.balance)}", ledger - account.balance, url_of(party))
    c.expected, c.actual = sf.money(total_items), sf.money(total_ledger)
    return c


def check_customers(tenant, start, end, entries) -> Check:
    c = Check("customers", "Customer accounts",
              "Each customer's ledger closes at the same balance as their unpaid bills less money on account.",
              labels=("Unpaid bills less on account", "Ledger balances"))
    if not tenant.has_feature("receivables"):
        return c.skip("Customer accounts are not switched on.")
    from app.accounts.receivables import customer_account
    from app.models.customer import Customer

    return _accounts_check(c, Customer.query.filter_by(tenant_id=tenant.id).order_by(Customer.name),
                           lambda cust: customer_account(tenant, cust),
                           lambda cust: _url("accounts.customer_ledger", customer_id=cust.id))


def check_suppliers(tenant, start, end, entries) -> Check:
    c = Check("suppliers", "Supplier accounts",
              "Each supplier's ledger closes at the same balance as the unpaid bills less money paid on account.",
              labels=("Unpaid bills less on account", "Ledger balances"))
    if not tenant.has_feature("payables"):
        return c.skip("Supplier accounts are not switched on.")
    from app.accounts.payables import supplier_account
    from app.models.purchase import Supplier

    return _accounts_check(c, Supplier.query.filter_by(tenant_id=tenant.id).order_by(Supplier.name),
                           lambda s: supplier_account(tenant, s),
                           lambda s: _url("accounts.supplier_ledger", supplier_id=s.id))


def check_cash_and_bank(tenant, start, end, entries) -> Check:
    c = Check("cash_and_bank", "Cash and bank books",
              "Each book closes at its opening balance plus money in less money out, and cash in hand never goes "
              "below zero.")
    if not tenant.has_feature("cash_bank_book"):
        return c.skip("The cash and bank books are not switched on.")
    from app.accounts.books import book

    for which in ("cash", "bank"):
        b = book(tenant, which, start, end)
        if sf.money(b["opening"] + b["total_in"] - b["total_out"]) != sf.money(b["closing"]):
            c.add(f"The {which} book closes at {sf.money(b['closing'])}; opening plus in less out is "
                  f"{sf.money(b['opening'] + b['total_in'] - b['total_out'])}", None,
                  _url("accounts.book", book=which))
        if which == "cash":
            below = next((r for r in b["rows"] if r.balance < 0), None)
            if b["opening"] < 0 or below is not None:
                when = f"on {below.entry_date:%d %b %Y}" if below is not None else "at the start of the month"
                amount = below.balance if below is not None else b["opening"]
                c.add(f"Cash in hand is below zero {when} ({sf.money(amount)}): money was paid out that the book "
                      "never saw come in", amount, _url("accounts.book", book="cash"))
    return c


def check_receipts_not_over_set(tenant, start, end, entries) -> Check:
    c = Check("receipts", "Receipts and payments set against bills",
              "No receipt or payment is set against bills for more than its own amount.")
    if not (tenant.has_feature("receivables") or tenant.has_feature("payables")):
        return c.skip("Accounts are not switched on.")
    from app.models.accounts import CustomerReceipt, SupplierPayment, VoucherStatus

    for model, label, endpoint, arg in ((CustomerReceipt, "Receipt", "accounts.view_receipt", "receipt_id"),
                                         (SupplierPayment, "Payment", "accounts.view_payment", "payment_id")):
        rows = model.query.filter(model.tenant_id == tenant.id, model.status == VoucherStatus.POSTED,
                                  model.voucher_date >= start, model.voucher_date <= end)
        for v in rows:
            used = sum((Decimal(a.amount) for a in v.allocations), ZERO)
            if used > Decimal(v.amount):
                c.add(f"{label} {v.number}: Rs. {sf.money(v.amount)} received, Rs. {sf.money(used)} set against "
                      "bills", used - Decimal(v.amount), _url(endpoint, **{arg: v.id}))
    return c


# --- running them -----------------------------------------------------------------------------------

def run(tenant, period: str) -> Report:
    """Every check for one month ("2026-11")."""
    from app.finance.filing import period_bounds

    start, end = period_bounds(period)
    entries = sf.entries(tenant, start, end)
    plan = [
        (check_sales_one_figure, (tenant, start, end, entries)),
        (check_documents_add_up, (tenant, start, end, entries)),
        (check_sales_vs_pnl, (tenant, start, end, entries)),
        (check_sales_vs_gstr1, (tenant, period, entries)),
        (check_gstr1_vs_gstr3b, (tenant, period, entries)),
        (check_journal, (tenant, start, end, entries)),
        (check_stock_not_negative, (tenant, start, end, entries)),
        (check_serials_match_stock, (tenant, start, end, entries)),
        (check_customers, (tenant, start, end, entries)),
        (check_suppliers, (tenant, start, end, entries)),
        (check_receipts_not_over_set, (tenant, start, end, entries)),
        (check_cash_and_bank, (tenant, start, end, entries)),
    ]
    checks = []
    for fn, args in plan:
        try:
            checks.append(fn(*args))
        except Exception as exc:  # noqa: BLE001 - one check failing to run must not hide the others
            db.session.rollback()
            log.exception("books check %s could not run for tenant %s", fn.__name__, tenant.id)
            checks.append(Check(fn.__name__.replace("check_", ""), fn.__name__.replace("check_", "").replace("_", " ")
                                .capitalize(), "This check could not be run.", status=ERROR,
                                note=f"{type(exc).__name__}: it was reported to ARFA."))
    return Report(period, start, end, checks, utcnow())


def blocking_failures(report: Report) -> list[Check]:
    return [c for c in report.failed if c.blocks_filing]


def save(tenant, report: Report):
    from app.models.filing import BooksCheckRun

    row = BooksCheckRun(tenant_id=tenant.id, period=report.period, ran_at=report.ran_at or utcnow(), ok=report.ok,
                        failed_count=len(report.failed), results=[c.as_dict() for c in report.checks])
    db.session.add(row)
    # One kept result per month is enough to show; older ones go.
    old = BooksCheckRun.query.filter(BooksCheckRun.tenant_id == tenant.id, BooksCheckRun.period == report.period) \
        .order_by(BooksCheckRun.ran_at.desc()).offset(5).all()
    for o in old:
        db.session.delete(o)
    db.session.flush()
    return row


def latest(tenant, period: str | None = None):
    from app.models.filing import BooksCheckRun

    query = BooksCheckRun.query.filter_by(tenant_id=tenant.id)
    if period:
        query = query.filter_by(period=period)
    return query.order_by(BooksCheckRun.ran_at.desc(), BooksCheckRun.id.desc()).first()


def failing_now(tenant) -> list:
    """The latest kept result of each month that has failures - for "Needs you"."""
    from app.models.filing import BooksCheckRun

    out, seen = [], set()
    for row in BooksCheckRun.query.filter_by(tenant_id=tenant.id) \
            .order_by(BooksCheckRun.ran_at.desc(), BooksCheckRun.id.desc()).limit(40):
        if row.period in seen:
            continue
        seen.add(row.period)
        if not row.ok:
            out.append(row)
    return out
