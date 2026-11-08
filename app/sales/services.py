"""Sales documents (quotation, proforma, sales order, delivery challan) and
recurring invoices. Line amounts come from the one GST service
(app.utils.gst) exactly as an invoice's do, so converting never changes a
rupee - the invoice is made by app.invoicing.services.create_invoice from
the same lines."""

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from app.extensions import db
from app.invoicing.services import InvoiceInput, InvoiceValidationError, LineInput, create_invoice
from app.models.customer import Customer
from app.models.expense import Frequency
from app.models.invoice_series import DocumentType
from app.models.mixins import utcnow
from app.models.sales import (
    SALES_DOCUMENT_SERIES,
    ChallanPurpose,
    RecurringInvoice,
    RecurringInvoiceRun,
    RecurringRunStatus,
    SalesDocument,
    SalesDocumentKind,
    SalesDocumentLine,
    SalesDocumentStatus,
)
from app.models.tenant import Gstin
from app.utils.financial_year import financial_year_for
from app.utils.gst import allowed_document_types, compute_invoice_totals, compute_line
from app.utils.numbering import allocate_number, get_or_create_series
from app.warehouse import dispatch

ZERO = Decimal("0.00")
MAX_RUNS_PER_PASS = 36
# What a quotation / order / challan can become.
CONVERTIBLE_TYPES = [DocumentType.TAX_INVOICE, DocumentType.BILL_OF_SUPPLY, DocumentType.EXPORT_INVOICE_IGST,
                     DocumentType.EXPORT_INVOICE_LUT, DocumentType.RCM_INVOICE]


class SalesError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class SalesDocInput:
    kind: SalesDocumentKind
    gstin_id: int
    customer_id: int
    place_of_supply_state_code: str
    doc_date: date
    invoice_document_type: DocumentType
    lines: list[LineInput] = field(default_factory=list)
    valid_until: date | None = None
    challan_purpose: ChallanPurpose | None = None
    notes: str | None = None
    terms: str | None = None
    # Warehouse dispatch: the godown a sales order ships from (required for
    # a sales order when the module is on).
    dispatch_location_id: int | None = None
    # An EMI (finance) sale (warehouse dispatch, stage 5B).
    is_finance_sale: bool = False
    financier_id: int | None = None
    loan_ref: str | None = None
    finance_amount: Decimal | None = None
    down_payment: Decimal | None = None
    # Where the goods go (blank: the customer's address) and, when no single
    # warehouse has it all, whether to split the order across warehouses.
    ship_to: dict | None = None
    split: bool = False
    # Set when this order is one part of a split (internal).
    split_group_id: int | None = None


def _customer_snapshot(customer: Customer) -> dict:
    return {"name": customer.name, "gstin": customer.gstin, "address_line1": customer.address_line1,
            "address_line2": customer.address_line2, "city": customer.city, "state_code": customer.state_code,
            "state_name": customer.state_name, "pincode": customer.pincode}


def _check_doc_type(tenant, doc_type: DocumentType):
    if doc_type not in CONVERTIBLE_TYPES or doc_type not in allowed_document_types(tenant.registration_type):
        raise SalesError(f"{tenant.legal_name} can't issue that kind of invoice.")


def create_sales_document(tenant, user, data: SalesDocInput) -> SalesDocument:
    customer = Customer.query.filter_by(id=data.customer_id, tenant_id=tenant.id).first() if data.customer_id else None
    if customer is None:
        raise SalesError("Select a valid customer.")
    gstin = Gstin.query.filter_by(id=data.gstin_id, tenant_id=tenant.id, is_active=True).first()
    if gstin is None:
        raise SalesError("Select a valid GSTIN to bill from.")
    _check_doc_type(tenant, data.invoice_document_type)
    if not data.lines:
        raise SalesError("Add at least one item.")
    if data.kind == SalesDocumentKind.DELIVERY_CHALLAN and data.challan_purpose is None:
        raise SalesError("Say why the goods are going out (job work, sale on approval...).")
    if data.valid_until and data.valid_until < data.doc_date:
        raise SalesError("'Valid until' can't be before the date.")
    warehouse, ship_to, reason = None, None, None
    if data.kind == SalesDocumentKind.SALES_ORDER and dispatch.is_on(tenant):
        ship_to = _ship_to(customer, data.ship_to)
        if data.split_group_id is not None:
            reason = "split"
            try:
                warehouse = dispatch.resolve_warehouse(tenant, data.dispatch_location_id)
                dispatch.check_free(tenant, warehouse, data.lines)
            except dispatch.DispatchError as exc:
                raise SalesError(exc.message)
        else:
            warehouse, reason, parts = _choose_warehouse(tenant, user, data, ship_to)
            if parts:
                return _create_split(tenant, user, data, ship_to, parts)
    charge_gst = data.invoice_document_type not in (DocumentType.BILL_OF_SUPPLY, DocumentType.EXPORT_INVOICE_LUT)
    is_intra = data.place_of_supply_state_code == gstin.state_code
    computed = []
    for line in data.lines:
        if line.qty <= 0:
            raise SalesError(f"Quantity for '{line.description}' must be more than 0.")
        c = compute_line(qty=line.qty, rate=line.rate, discount_percent=line.discount_percent, gst_rate=line.gst_rate,
                         is_intra_state=is_intra, charge_gst=charge_gst, price_includes_tax=line.price_includes_tax)
        computed.append({**c, "input": line})
    totals = compute_invoice_totals(computed)
    finance = _finance(tenant, data, totals["grand_total"]) if warehouse is not None and data.is_finance_sale else {}
    series = get_or_create_series(tenant.id, gstin.id, SALES_DOCUMENT_SERIES[data.kind], financial_year_for(data.doc_date))
    doc = SalesDocument(
        tenant_id=tenant.id, kind=data.kind, gstin_id=gstin.id, invoice_series_id=series.id,
        number=allocate_number(series), customer_id=customer.id, customer_snapshot=_customer_snapshot(customer),
        place_of_supply_state_code=data.place_of_supply_state_code, doc_date=data.doc_date,
        valid_until=data.valid_until, invoice_document_type=data.invoice_document_type,
        challan_purpose=data.challan_purpose if data.kind == SalesDocumentKind.DELIVERY_CHALLAN else None,
        status=SalesDocumentStatus.OPEN, notes=(data.notes or "").strip() or None,
        terms=(data.terms or "").strip() or None, created_by_id=user.id, **totals, **finance,
        ship_to=ship_to, dispatch_choice_reason=reason, split_group_id=data.split_group_id or None,
    )
    for order, item in enumerate(computed):
        li = item["input"]
        doc.lines.append(SalesDocumentLine(
            product_id=li.product_id, description=li.description, hsn_or_sac_code=li.hsn_or_sac_code, qty=li.qty,
            unit=li.unit, rate=li.rate, discount_percent=li.discount_percent, discount_amount=item["discount_amount"],
            taxable_value=item["taxable_value"], gst_rate=li.gst_rate if charge_gst else ZERO,
            price_includes_tax=li.price_includes_tax, cgst_amount=item["cgst_amount"], sgst_amount=item["sgst_amount"],
            igst_amount=item["igst_amount"], line_total=item["line_total"], sort_order=order,
        ))
    db.session.add(doc)
    db.session.flush()
    if warehouse is not None:
        try:
            dispatch.start(tenant, doc, warehouse)
        except dispatch.DispatchError as exc:
            raise SalesError(exc.message)
    return doc


SHIP_TO_KEYS = ("name", "address_line1", "address_line2", "city", "state_code", "pincode", "phone")


def _ship_to(customer: Customer, given: dict | None) -> dict:
    """The delivery address: what the order says, else the customer's."""
    base = {"name": customer.name, "address_line1": customer.address_line1, "address_line2": customer.address_line2,
            "city": customer.city, "state_code": customer.state_code, "pincode": customer.pincode,
            "phone": customer.phone}
    given = {k: (str(v).strip()[:255] if v is not None else "") for k, v in (given or {}).items() if k in SHIP_TO_KEYS}
    if any(given.get(k) for k in ("address_line1", "city", "pincode")):
        out = {k: given.get(k) or "" for k in SHIP_TO_KEYS}
        out["name"] = out["name"] or customer.name
    else:
        out = {k: (base.get(k) or "") for k in SHIP_TO_KEYS}
    from app.warehouse.nearest import clean_pincode

    if out.get("pincode") and not clean_pincode(out["pincode"]):
        raise SalesError("The delivery pincode should be 6 digits.")
    return out


def _choose_warehouse(tenant, user, data: SalesDocInput, ship_to: dict):
    """(warehouse, why, split plan or None): the one picked on the form (its
    reason "manual" unless it is what ARFA suggested), else ARFA's suggestion
    (bulk / API orders). With split on and the warehouse short, a plan to
    spread the order over warehouses, nearest first."""
    from app.warehouse import nearest

    home = dispatch.ledger.location_for_user(tenant.id, user)
    suggestion = nearest.suggest_warehouse(tenant, ship_to.get("pincode"), data.lines,
                                           fallback=home.default_dispatch_location)
    try:
        if data.dispatch_location_id:
            warehouse = dispatch.resolve_warehouse(tenant, data.dispatch_location_id)
            reason = suggestion.reason if suggestion.location and suggestion.location.id == warehouse.id else "manual"
        elif suggestion.location is not None:
            warehouse, reason = suggestion.location, suggestion.reason
        else:
            raise dispatch.DispatchError("Pick the warehouse this order ships from. " + suggestion.message)
        try:
            dispatch.check_free(tenant, warehouse, data.lines)
        except dispatch.DispatchError:
            if not data.split:
                raise
            if data.is_finance_sale:
                raise dispatch.DispatchError("An EMI sale is invoiced once, with every serial - it can't be split "
                                             "across warehouses. Pick one warehouse that has it all.")
            ranked = sorted(suggestion.ranked, key=lambda o: o.location.id != warehouse.id)   # the chosen one first
            plan = nearest.plan_split(tenant, ranked, data.lines)
            if plan is None:
                raise dispatch.DispatchError("Even all the warehouses together don't have this order free.")
            return warehouse, "split", plan
    except dispatch.DispatchError as exc:
        raise SalesError(exc.message)
    return warehouse, reason, None


def _create_split(tenant, user, data: SalesDocInput, ship_to: dict, plan) -> SalesDocument:
    """One order per warehouse, each with its share of the lines; every part
    points at the first (split_group_id). Returns the first; .split_parts holds all."""
    from dataclasses import replace

    parts = []
    for location, shares in plan:
        lines = [replace(data.lines[i], qty=Decimal(q)) for i, q in sorted(shares.items()) if q > 0]
        part = create_sales_document(tenant, user, replace(
            data, lines=lines, dispatch_location_id=location.id, split=False, ship_to=ship_to,
            split_group_id=parts[0].id if parts else 0))
        if not parts:
            part.split_group_id = part.id
        parts.append(part)
    db.session.flush()
    parts[0].split_parts = parts
    return parts[0]


def _finance(tenant, data: SalesDocInput, total) -> dict:
    """An EMI sale's split: what the financier pays (finance amount) and what
    the customer pays (down payment) - together the order's total."""
    from app.models.warehouse import Financier

    financier = (Financier.query.filter_by(id=data.financier_id, tenant_id=tenant.id, is_active=True).first()
                 if data.financier_id else None)
    if financier is None:
        raise SalesError("Pick the financier (Bajaj Finserv, HDB...) for this EMI sale.")
    total = Decimal(total)
    finance = Decimal(data.finance_amount or 0)
    if finance <= 0 or finance > total:
        raise SalesError(f"The finance amount must be more than 0 and at most the order total (Rs. {total:.2f}).")
    down = Decimal(data.down_payment) if data.down_payment not in (None, "") else total - finance
    if abs(down + finance - total) > Decimal("1.00"):
        raise SalesError(f"Down payment Rs. {down:.2f} + finance Rs. {finance:.2f} should make the order total "
                         f"Rs. {total:.2f}.")
    return {"is_finance_sale": True, "financier_id": financier.id,
            "loan_ref": (data.loan_ref or "").strip()[:60] or None, "finance_amount": finance,
            "down_payment": total - finance}


def lines_of(doc_lines) -> list[LineInput]:
    """Invoice line inputs from a document's (or a template's) lines."""
    out = []
    for l in doc_lines:
        get = (lambda k: l.get(k)) if isinstance(l, dict) else (lambda k: getattr(l, k))
        out.append(LineInput(
            description=get("description"), hsn_or_sac_code=get("hsn_or_sac_code") or "", qty=Decimal(str(get("qty"))),
            rate=Decimal(str(get("rate"))), discount_percent=Decimal(str(get("discount_percent") or 0)),
            gst_rate=Decimal(str(get("gst_rate") or 0)), product_id=get("product_id") or None,
            unit=get("unit") or "pcs", price_includes_tax=bool(get("price_includes_tax")),
        ))
    return out


def _require_open(doc: SalesDocument):
    if doc.status != SalesDocumentStatus.OPEN:
        raise SalesError(f"This {doc.label.lower()} is already {doc.status.value}.")


def mark_converted(doc: SalesDocument, invoice, user) -> None:
    _require_open(doc)
    doc.status = SalesDocumentStatus.CONVERTED
    doc.converted_invoice_id = invoice.id
    doc.closed_at = utcnow()
    doc.closed_by_id = user.id


def convert_to_invoice(tenant, user, doc: SalesDocument, invoice_date: date | None = None):
    """One click: issue the invoice from the document's own lines, customer
    and place of supply, and close the document."""
    _require_open(doc)
    if doc.customer_id is None:
        raise SalesError("This document has no customer.")
    if doc.dispatch_status is not None:
        return convert_picked_order(tenant, user, doc, invoice_date)
    try:
        invoice = create_invoice(tenant, user, InvoiceInput(
            gstin_id=doc.gstin_id, document_type=doc.invoice_document_type, customer_id=doc.customer_id,
            place_of_supply_state_code=doc.place_of_supply_state_code, invoice_date=invoice_date or date.today(),
            notes=doc.notes, lines=lines_of(doc.lines),
        ))
    except InvoiceValidationError as exc:
        raise SalesError(exc.message)
    mark_converted(doc, invoice, user)
    return invoice


def convert_picked_order(tenant, user, doc: SalesDocument, invoice_date: date | None = None):
    """A warehouse-dispatched order: invoiced once its warehouse has picked
    it, with each line's scanned serials, from that warehouse's stock."""
    from app.models.inventory import SerialStatus
    from app.models.warehouse import DispatchStatus

    if doc.dispatch_status != DispatchStatus.PICKED:
        raise SalesError("This order is still at the warehouse - it is invoiced there once every unit is "
                         "scanned and the picker has confirmed it.")
    progress = dispatch.line_progress(doc)
    lines = lines_of(doc.lines)
    for line_in, line in zip(lines, doc.lines):
        if not dispatch.needs_scan(line):
            continue
        info = progress[line.id]
        if info["picked"] < info["qty"]:
            raise SalesError(f"{line.description}: only {info['picked']} of {info['qty']} picked.")
        for pick in info["picks"]:
            unit = pick.serial
            if unit is None:
                continue
            if unit.status != SerialStatus.PICKED or unit.picked_sales_line_id != line.id \
                    or unit.location_id != doc.dispatch_location_id:
                raise SalesError(f"{line.description}: serial {unit.serial_no} is no longer held at "
                                 f"{doc.dispatch_location.name} for this order - undo and scan it again.")
        line_in.serials = list(info["serials"])
    try:
        invoice = create_invoice(tenant, user, InvoiceInput(
            gstin_id=doc.gstin_id, document_type=doc.invoice_document_type, customer_id=doc.customer_id,
            place_of_supply_state_code=doc.place_of_supply_state_code, invoice_date=invoice_date or date.today(),
            notes=doc.notes, lines=lines, dispatch_location_id=doc.dispatch_location_id,
            financier_id=doc.financier_id if doc.is_finance_sale else None,
            finance_amount=doc.finance_amount if doc.is_finance_sale else None, loan_ref=doc.loan_ref,
            ship_to=doc.ship_to,
        ))
    except InvoiceValidationError as exc:
        raise SalesError(exc.message)
    mark_converted(doc, invoice, user)
    dispatch.mark_invoiced(doc, invoice)
    return invoice


def cancel_document(doc: SalesDocument, user, reason: str) -> None:
    _require_open(doc)
    if not (reason or "").strip():
        raise SalesError("A reason is required.")
    dispatch.cancelled(doc)
    doc.status = SalesDocumentStatus.CANCELLED
    doc.cancel_reason = reason.strip()
    doc.closed_at = utcnow()
    doc.closed_by_id = user.id


def mark_returned(doc: SalesDocument, user, note: str) -> None:
    """A challan's goods came back (job work done, approval goods refused)."""
    if doc.kind != SalesDocumentKind.DELIVERY_CHALLAN:
        raise SalesError("Only a delivery challan's goods come back.")
    _require_open(doc)
    doc.status = SalesDocumentStatus.RETURNED
    doc.cancel_reason = (note or "").strip() or "Goods returned"
    doc.closed_at = utcnow()
    doc.closed_by_id = user.id


# --- recurring invoices -------------------------------------------------------------------

def _add_months(d: date, months: int, anchor_day: int) -> date:
    index = d.month - 1 + months
    year, month = d.year + index // 12, index % 12 + 1
    return date(year, month, min(anchor_day, calendar.monthrange(year, month)[1]))


def next_due(current: date, frequency: Frequency, anchor_day: int) -> date:
    if frequency == Frequency.WEEKLY:
        return current + timedelta(days=7)
    months = {Frequency.MONTHLY: 1, Frequency.QUARTERLY: 3, Frequency.YEARLY: 12}[frequency]
    return _add_months(current, months, anchor_day)


@dataclass
class RecurringInvoiceInput:
    name: str
    gstin_id: int
    customer_id: int
    document_type: DocumentType
    place_of_supply_state_code: str
    frequency: Frequency
    start_date: date
    lines: list[LineInput]
    end_date: date | None = None
    notes: str | None = None


def _line_json(line: LineInput) -> dict:
    return {"description": line.description, "hsn_or_sac_code": line.hsn_or_sac_code, "qty": str(line.qty),
            "unit": line.unit, "rate": str(line.rate), "discount_percent": str(line.discount_percent),
            "gst_rate": str(line.gst_rate), "product_id": line.product_id, "price_includes_tax": line.price_includes_tax}


def save_recurring(tenant, user, data: RecurringInvoiceInput, recurring: RecurringInvoice | None = None) -> RecurringInvoice:
    name = (data.name or "").strip()[:120]
    if not name:
        raise SalesError("Give the recurring invoice a name (e.g. 'AMC - Ravi Stores').")
    if Customer.query.filter_by(id=data.customer_id, tenant_id=tenant.id).first() is None:
        raise SalesError("Select a valid customer.")
    if Gstin.query.filter_by(id=data.gstin_id, tenant_id=tenant.id, is_active=True).first() is None:
        raise SalesError("Select a valid GSTIN to bill from.")
    _check_doc_type(tenant, data.document_type)
    if not data.lines:
        raise SalesError("Add at least one item.")
    if data.end_date and data.end_date < data.start_date:
        raise SalesError("The end date can't be before the start date.")
    if recurring is None:
        recurring = RecurringInvoice(tenant_id=tenant.id, created_by_id=user.id, next_due_date=data.start_date)
        db.session.add(recurring)
    elif recurring.start_date != data.start_date:
        # Moving the start re-anchors the schedule from there (runs already
        # made stay as they are; the unique due date stops duplicates).
        recurring.next_due_date = data.start_date
    recurring.name = name
    recurring.gstin_id = data.gstin_id
    recurring.customer_id = data.customer_id
    recurring.document_type = data.document_type
    recurring.place_of_supply_state_code = data.place_of_supply_state_code
    recurring.frequency = data.frequency
    recurring.start_date = data.start_date
    recurring.anchor_day = data.start_date.day
    recurring.end_date = data.end_date
    recurring.lines = [_line_json(l) for l in data.lines]
    recurring.notes = (data.notes or "").strip() or None
    db.session.flush()
    return recurring


def generate_due_runs(tenant, today: date | None = None) -> list[RecurringInvoiceRun]:
    """A draft (pending run) for every due date up to today. Safe to run any
    number of times - the unique (recurring, due date) stops doubles."""
    today = today or date.today()
    made = []
    for rec in RecurringInvoice.query.filter_by(tenant_id=tenant.id, is_active=True).all():
        steps = 0
        while rec.next_due_date <= today and steps < MAX_RUNS_PER_PASS:
            if rec.end_date and rec.next_due_date > rec.end_date:
                break
            exists = RecurringInvoiceRun.query.filter_by(recurring_id=rec.id, due_date=rec.next_due_date).first()
            if exists is None:
                run = RecurringInvoiceRun(tenant_id=tenant.id, recurring_id=rec.id, due_date=rec.next_due_date,
                                          status=RecurringRunStatus.PENDING)
                db.session.add(run)
                made.append(run)
            rec.next_due_date = next_due(rec.next_due_date, rec.frequency, rec.anchor_day)
            steps += 1
    db.session.flush()
    return made


def _require_pending(run: RecurringInvoiceRun):
    if run.status != RecurringRunStatus.PENDING:
        raise SalesError(f"This one is already {run.status.value}.")


def mark_run_issued(run: RecurringInvoiceRun, invoice, user) -> None:
    _require_pending(run)
    run.status = RecurringRunStatus.ISSUED
    run.invoice_id = invoice.id
    run.closed_at = utcnow()
    run.closed_by_id = user.id


def issue_run(tenant, user, run: RecurringInvoiceRun, invoice_date: date | None = None):
    _require_pending(run)
    rec = run.recurring
    try:
        invoice = create_invoice(tenant, user, InvoiceInput(
            gstin_id=rec.gstin_id, document_type=rec.document_type, customer_id=rec.customer_id,
            place_of_supply_state_code=rec.place_of_supply_state_code, invoice_date=invoice_date or run.due_date,
            notes=rec.notes, lines=lines_of(rec.lines),
        ))
    except InvoiceValidationError as exc:
        raise SalesError(exc.message)
    mark_run_issued(run, invoice, user)
    return invoice


def skip_run(run: RecurringInvoiceRun, user, reason: str) -> None:
    _require_pending(run)
    if not (reason or "").strip():
        raise SalesError("Say why this one is skipped.")
    run.status = RecurringRunStatus.SKIPPED
    run.skip_reason = reason.strip()
    run.closed_at = utcnow()
    run.closed_by_id = user.id
