from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.models.customer import Customer
from app.models.invoice_series import DocumentType
from app.models.mixins import utcnow
from app.models.pos_bill import PaymentMode, POSBill, POSBillLine, POSBillStatus
from app.models.tenant import RegistrationType, Tenant
from app.utils.financial_year import financial_year_for
from app.utils.gst import compute_invoice_totals, compute_line
from app.utils.numbering import allocate_number, get_or_create_series

_MAX_DESCRIPTION_LENGTH = 255
_MAX_HSN_LENGTH = 8
_MAX_UNIT_LENGTH = 20


class POSValidationError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class CartLineInput:
    description: str
    hsn_or_sac_code: str
    qty: Decimal
    rate: Decimal
    gst_rate: Decimal
    product_id: int | None = None
    unit: str = "pcs"
    discount_percent: Decimal = Decimal("0")


@dataclass
class CartInput:
    lines: list[CartLineInput] = field(default_factory=list)
    customer_id: int | None = None


def _to_decimal(value: str, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise POSValidationError(f"'{field_name}' must be a number.")


def _bounded(value: str, field_name: str, max_length: int) -> str:
    if len(value) > max_length:
        raise POSValidationError(f"'{field_name}' can be at most {max_length} characters.")
    return value


def parse_cart_lines(descriptions, hsns, qtys, rates, gst_rates, product_ids, units) -> list[CartLineInput]:
    lines = []
    for i, description in enumerate(descriptions):
        description = (description or "").strip()
        if not description:
            continue
        hsn = (hsns[i] if i < len(hsns) else "").strip()
        unit = (units[i] if i < len(units) and units[i] else "pcs").strip()
        lines.append(
            CartLineInput(
                description=_bounded(description, "Item name", _MAX_DESCRIPTION_LENGTH),
                hsn_or_sac_code=_bounded(hsn, "HSN/SAC code", _MAX_HSN_LENGTH),
                qty=_to_decimal(qtys[i] if i < len(qtys) else "0", "quantity"),
                rate=_to_decimal(rates[i] if i < len(rates) else "0", "rate"),
                gst_rate=_to_decimal(gst_rates[i] if i < len(gst_rates) else "0", "GST rate"),
                product_id=int(product_ids[i]) if i < len(product_ids) and product_ids[i] else None,
                unit=_bounded(unit, "Unit", _MAX_UNIT_LENGTH),
            )
        )
    if not lines:
        raise POSValidationError("Cart is empty.")
    return lines


def _charge_gst(tenant: Tenant) -> bool:
    # Retail POS never asks the cashier about place of supply or GST
    # jargon - a store sale is always intra-state (CGST+SGST), and a
    # composition/unregistered client never charges GST at all, same as
    # a Bill of Supply in the Invoicing module.
    return tenant.registration_type == RegistrationType.REGULAR


def _compute_lines(tenant: Tenant, lines: list[CartLineInput]) -> list[dict]:
    charge_gst = _charge_gst(tenant)
    computed = []
    for line in lines:
        result = compute_line(
            qty=line.qty,
            rate=line.rate,
            discount_percent=line.discount_percent,
            gst_rate=line.gst_rate,
            is_intra_state=True,
            charge_gst=charge_gst,
        )
        computed.append({**result, "input": line})
    return computed


def _persist_lines(bill: POSBill, computed_lines: list[dict], charge_gst: bool) -> None:
    for order, item in enumerate(computed_lines):
        line_in = item["input"]
        db.session.add(
            POSBillLine(
                bill_id=bill.id,
                product_id=line_in.product_id,
                description=line_in.description,
                hsn_or_sac_code=line_in.hsn_or_sac_code,
                qty=line_in.qty,
                unit=line_in.unit,
                rate=line_in.rate,
                discount_percent=line_in.discount_percent,
                discount_amount=item["discount_amount"],
                taxable_value=item["taxable_value"],
                gst_rate=line_in.gst_rate if charge_gst else Decimal("0"),
                cgst_amount=item["cgst_amount"],
                sgst_amount=item["sgst_amount"],
                igst_amount=item["igst_amount"],
                line_total=item["line_total"],
                sort_order=order,
            )
        )


def hold_cart(tenant: Tenant, created_by, cart: CartInput, label: str | None) -> POSBill:
    gstin = tenant.gstins.filter_by(is_active=True).first()
    if not gstin:
        raise POSValidationError(
            "This client has no business location on file. Ask the firm to add one from Clients -> this client."
        )

    computed_lines = _compute_lines(tenant, cart.lines)
    totals = compute_invoice_totals(computed_lines)

    bill = POSBill(
        tenant_id=tenant.id,
        gstin_id=gstin.id,
        customer_id=cart.customer_id,
        status=POSBillStatus.HELD,
        hold_label=(label or "").strip()[:100] or None,
        created_by_id=created_by.id,
        **totals,
    )
    db.session.add(bill)
    db.session.flush()
    _persist_lines(bill, computed_lines, _charge_gst(tenant))
    return bill


def discard_held_bill(bill: POSBill) -> None:
    if bill.status != POSBillStatus.HELD:
        raise POSValidationError("Only a held bill can be discarded.")
    db.session.delete(bill)


def checkout(tenant: Tenant, created_by, cart: CartInput, payment_mode: PaymentMode) -> POSBill:
    gstin = tenant.gstins.filter_by(is_active=True).first()
    if not gstin:
        raise POSValidationError(
            "This client has no business location on file. Ask the firm to add one from Clients -> this client."
        )

    customer = None
    customer_snapshot = None
    if cart.customer_id:
        customer = Customer.query.filter_by(id=cart.customer_id, tenant_id=tenant.id).first()
        if not customer:
            raise POSValidationError("Select a valid customer.")
        customer_snapshot = {
            "name": customer.name,
            "gstin": customer.gstin,
            "phone": customer.phone,
        }

    charge_gst = _charge_gst(tenant)
    computed_lines = _compute_lines(tenant, cart.lines)
    totals = compute_invoice_totals(computed_lines)

    series = get_or_create_series(tenant.id, gstin.id, DocumentType.POS_BILL, financial_year_for(date.today()))
    bill_number = allocate_number(series)

    bill = POSBill(
        tenant_id=tenant.id,
        gstin_id=gstin.id,
        invoice_series_id=series.id,
        bill_number=bill_number,
        customer_id=customer.id if customer else None,
        customer_snapshot=customer_snapshot,
        status=POSBillStatus.COMPLETED,
        payment_mode=payment_mode,
        created_by_id=created_by.id,
        completed_at=utcnow(),
        **totals,
    )
    db.session.add(bill)
    db.session.flush()
    _persist_lines(bill, computed_lines, charge_gst)
    return bill


def refund_bill(bill: POSBill, refunded_by, amount: Decimal, reason: str) -> None:
    if bill.status != POSBillStatus.COMPLETED:
        raise POSValidationError("Only a completed bill can be refunded.")
    if not reason or not reason.strip():
        raise POSValidationError("A reason is required to refund a bill.")
    if amount <= 0 or amount > bill.grand_total:
        raise POSValidationError(f"Refund amount must be between 0 and Rs. {bill.grand_total}.")

    bill.status = POSBillStatus.REFUNDED
    bill.refunded_amount = amount
    bill.refund_reason = reason.strip()
    bill.refunded_at = utcnow()
    bill.refunded_by_id = refunded_by.id


def _day_bounds(on_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(on_date, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def z_report(tenant_id: int, on_date: date) -> dict:
    start, end = _day_bounds(on_date)
    bills = (
        POSBill.query.filter(
            POSBill.tenant_id == tenant_id,
            POSBill.status.in_([POSBillStatus.COMPLETED, POSBillStatus.REFUNDED]),
            POSBill.completed_at >= start,
            POSBill.completed_at < end,
        )
        .order_by(POSBill.completed_at)
        .all()
    )

    by_mode = {mode: {"count": 0, "total": Decimal("0")} for mode in PaymentMode}
    total_sales = Decimal("0")
    total_tax = Decimal("0")
    total_refunds = Decimal("0")
    bill_count = 0

    for bill in bills:
        if bill.status == POSBillStatus.REFUNDED:
            total_refunds += bill.refunded_amount or Decimal("0")
            continue
        bill_count += 1
        total_sales += bill.grand_total
        total_tax += bill.total_cgst + bill.total_sgst + bill.total_igst
        if bill.payment_mode:
            by_mode[bill.payment_mode]["count"] += 1
            by_mode[bill.payment_mode]["total"] += bill.grand_total

    return {
        "date": on_date,
        "bill_count": bill_count,
        "total_sales": total_sales,
        "total_tax": total_tax,
        "total_refunds": total_refunds,
        "net_sales": total_sales - total_refunds,
        "by_mode": by_mode,
        "bills": bills,
    }
