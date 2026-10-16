"""Job cards (flag "job_cards"): a repair or service job from receipt to
delivery.

  Received -> In progress -> Ready -> Delivered   (or Cancelled, with a reason)

Parts used come out of the main store's stock as they are added (so the
shelf is right while the job is open) and go back if removed or the job
is cancelled. Making the invoice hands the parts over to it: the job's
own stock entries are reversed and the invoice takes them out as a sale,
so they are counted once. Labour is a SAC service line. A job card is
numbered in its own series (JOB) and never deleted."""

from datetime import date
from decimal import Decimal

from app.extensions import db
from app.inventory import ledger
from app.models.business import JOB_STATUS_LABELS, JobCard, JobCardLine, JobStatus
from app.models.customer import Customer
from app.models.inventory import MovementType, StockMovement
from app.models.invoice_series import DocumentType
from app.models.mixins import utcnow
from app.models.product import Product
from app.models.tenant import Gstin, RegistrationType
from app.utils.financial_year import financial_year_for
from app.utils.numbering import allocate_number, get_or_create_series

FLOW = [JobStatus.RECEIVED, JobStatus.IN_PROGRESS, JobStatus.READY, JobStatus.DELIVERED]


class JobError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _dec(value, label) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except Exception:
        raise JobError(f"{label} must be a number.")


def create_job(tenant, user, data: dict) -> JobCard:
    customer = None
    if data.get("customer_id"):
        customer = Customer.query.filter_by(id=int(data["customer_id"]), tenant_id=tenant.id).first()
    if customer is None:
        name = (data.get("customer_name") or "").strip()
        if not name:
            raise JobError("Pick the customer, or type a new customer's name.")
        gstin = tenant.gstins.filter_by(is_active=True).first()
        customer = Customer(tenant_id=tenant.id, name=name[:255], phone=(data.get("customer_phone") or "").strip()[:20] or None,
                            state_code=gstin.state_code if gstin else None, state_name=gstin.state_name if gstin else None)
        db.session.add(customer)
        db.session.flush()
    item = (data.get("item") or "").strip()
    complaint = (data.get("complaint") or "").strip()
    if not item or not complaint:
        raise JobError("Say what was received and what the problem is.")
    estimate = _dec(data["estimate"], "Estimate") if data.get("estimate") else None
    promised = date.fromisoformat(data["promised_on"]) if data.get("promised_on") else None
    gstin = tenant.gstins.filter_by(is_active=True).first()
    if gstin is None:
        raise JobError("This client has no business location on file.")
    today = date.today()
    series = get_or_create_series(tenant.id, gstin.id, DocumentType.JOB_CARD, financial_year_for(today))
    job = JobCard(tenant_id=tenant.id, gstin_id=gstin.id, invoice_series_id=series.id, number=allocate_number(series),
                  customer_id=customer.id, received_on=today, item=item[:150],
                  serial_no=(data.get("serial_no") or "").strip()[:64] or None,
                  accessories=(data.get("accessories") or "").strip()[:255] or None, complaint=complaint,
                  estimate=estimate, promised_on=promised, technician=(data.get("technician") or "").strip()[:100] or None,
                  status=JobStatus.RECEIVED, status_changed_at=utcnow(), created_by_id=user.id)
    db.session.add(job)
    db.session.flush()
    return job


def _open(job: JobCard):
    if job.status in (JobStatus.DELIVERED, JobStatus.CANCELLED):
        raise JobError(f"This job is {JOB_STATUS_LABELS[job.status].lower()} - it can't be changed.")
    if job.invoice_id:
        raise JobError("This job is already invoiced.")


def set_status(job: JobCard, status: JobStatus) -> None:
    if job.status in (JobStatus.DELIVERED, JobStatus.CANCELLED):
        raise JobError(f"This job is already {JOB_STATUS_LABELS[job.status].lower()}.")
    if status == JobStatus.CANCELLED:
        raise JobError("Use Cancel job, with a reason.")
    if status == JobStatus.DELIVERED and job.lines and not job.invoice_id:
        raise JobError("Make the invoice before handing the item over.")
    job.status = status
    job.status_changed_at = utcnow()
    if status == JobStatus.DELIVERED:
        job.delivered_at = utcnow()


def add_part(tenant, user, job: JobCard, product_id: int, qty) -> JobCardLine:
    _open(job)
    product = Product.query.filter_by(id=product_id, tenant_id=tenant.id, is_active=True).first()
    if product is None:
        raise JobError("Pick a part from your items.")
    if product.is_service:
        raise JobError(f"{product.name} is a service - add it as labour.")
    qty = _dec(qty or "1", "Quantity")
    if qty <= 0:
        raise JobError("Quantity must be more than 0.")
    location = ledger.default_location(tenant.id)
    try:
        ledger.check_available(tenant, location.id, {product.id: qty}, {product.id: product})
    except ledger.StockError as exc:
        raise JobError(exc.message)
    line = JobCardLine(job_id=job.id, product_id=product.id, description=product.name, hsn_or_sac_code=product.hsn_or_sac_code,
                       qty=qty, unit=product.unit, rate=product.sale_price, gst_rate=product.gst_rate,
                       price_includes_tax=product.price_includes_tax)
    db.session.add(line)
    db.session.flush()
    if ledger.sells_from_stock(product):
        ledger.post(tenant.id, product.id, location.id, -qty, MovementType.SALE, "job_card", job.id, source_line_id=line.id,
                    unit_cost=ledger.issue_cost(product), user_id=user.id, note=f"Used on {job.number}")
    return line


def add_labour(job: JobCard, description: str, amount, gst_rate, sac: str) -> JobCardLine:
    _open(job)
    description = (description or "").strip() or "Labour"
    amount = _dec(amount, "Labour charge")
    if amount <= 0:
        raise JobError("The labour charge must be more than 0.")
    sac = (sac or "").strip() or "9987"
    if not sac.startswith("99"):
        raise JobError("Labour is a service - its SAC code starts with 99.")
    line = JobCardLine(job_id=job.id, description=description[:255], hsn_or_sac_code=sac[:8], qty=Decimal(1), unit="NOS",
                       rate=amount, gst_rate=_dec(gst_rate or "18", "GST rate"), price_includes_tax=False)
    db.session.add(line)
    db.session.flush()
    return line


def _return_line_stock(job: JobCard, line: JobCardLine, user_id) -> None:
    for m in StockMovement.query.filter_by(tenant_id=job.tenant_id, source_type="job_card", source_id=job.id,
                                           source_line_id=line.id, reversal_of_id=None).all():
        if StockMovement.query.filter_by(reversal_of_id=m.id).first():
            continue
        ledger.post(job.tenant_id, m.product_id, m.location_id, -m.qty, m.movement_type, "job_card", job.id,
                    source_line_id=line.id, unit_cost=m.unit_cost, user_id=user_id, note=f"Removed from {job.number}",
                    reversal_of_id=m.id)


def remove_line(job: JobCard, line: JobCardLine, user) -> None:
    _open(job)
    _return_line_stock(job, line, user.id)
    db.session.delete(line)


def cancel(job: JobCard, user, reason: str) -> None:
    if job.status in (JobStatus.DELIVERED, JobStatus.CANCELLED) or job.invoice_id:
        raise JobError("An invoiced, delivered or cancelled job can't be cancelled.")
    if not reason or not reason.strip():
        raise JobError("A reason is needed to cancel a job.")
    ledger.reverse_source(job.tenant_id, "job_card", job.id, user.id, f"{job.number} cancelled")
    job.status = JobStatus.CANCELLED
    job.cancel_reason = reason.strip()
    job.status_changed_at = utcnow()


def make_invoice(tenant, user, job: JobCard):
    """Bill the job: its parts and labour on a tax invoice (bill of supply
    for a client without regular GST registration)."""
    from app.invoicing.services import InvoiceInput, InvoiceValidationError, LineInput, create_invoice

    _open(job)
    if not job.lines:
        raise JobError("Add the parts and labour first.")
    customer = job.customer
    gstin = db.session.get(Gstin, job.gstin_id)
    doc_type = DocumentType.TAX_INVOICE if tenant.registration_type == RegistrationType.REGULAR else DocumentType.BILL_OF_SUPPLY
    # The invoice takes the parts out of stock itself - the job's own
    # entries are reversed so they're counted once.
    ledger.reverse_source(tenant.id, "job_card", job.id, user.id, f"{job.number} invoiced")
    try:
        invoice = create_invoice(tenant, user, InvoiceInput(
            gstin_id=job.gstin_id, document_type=doc_type, customer_id=customer.id,
            place_of_supply_state_code=customer.state_code or gstin.state_code, invoice_date=date.today(),
            notes=f"Job card {job.number}: {job.item}",
            lines=[LineInput(description=l.description, hsn_or_sac_code=l.hsn_or_sac_code, qty=l.qty, rate=l.rate,
                             discount_percent=Decimal("0"), gst_rate=l.gst_rate, product_id=l.product_id, unit=l.unit,
                             price_includes_tax=l.price_includes_tax) for l in job.lines]))
    except InvoiceValidationError as exc:
        raise JobError(exc.message)
    job.invoice_id = invoice.id
    if job.status in (JobStatus.RECEIVED, JobStatus.IN_PROGRESS):
        job.status = JobStatus.READY
        job.status_changed_at = utcnow()
    return invoice


def ready_message(tenant, job: JobCard) -> str:
    amount = f" Amount: Rs. {job.invoice.grand_total:.2f}." if job.invoice else (
        f" Estimate: Rs. {job.estimate:.2f}." if job.estimate else "")
    return (f"Hello {job.customer.name}, your {job.item} (job {job.number}) is ready for pickup.{amount} "
            f"- {tenant.display_name}")
