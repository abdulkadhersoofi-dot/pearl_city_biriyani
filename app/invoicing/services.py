from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.models.customer import Customer
from app.models.invoice import Invoice, InvoiceLine, InvoiceStatus
from app.models.invoice_series import DocumentType
from app.models.mixins import utcnow
from app.models.tenant import Gstin, Tenant
from app.utils.financial_year import financial_year_for
from app.utils.gst import allowed_document_types, compute_invoice_totals, compute_line
from app.utils.numbering import allocate_number, get_or_create_series


class InvoiceValidationError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class LineInput:
    description: str
    hsn_or_sac_code: str
    qty: Decimal
    rate: Decimal
    discount_percent: Decimal
    gst_rate: Decimal
    product_id: int | None = None
    unit: str = "pcs"


@dataclass
class InvoiceInput:
    gstin_id: int
    document_type: DocumentType
    customer_id: int
    place_of_supply_state_code: str
    invoice_date: date
    notes: str | None
    lines: list[LineInput] = field(default_factory=list)


def _to_decimal(value: str, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise InvoiceValidationError(f"'{field_name}' must be a number.")


def parse_line_arrays(
    descriptions, hsns, qtys, rates, discounts, gst_rates, product_ids, units
) -> list[LineInput]:
    lines = []
    for i, description in enumerate(descriptions):
        description = (description or "").strip()
        if not description:
            continue
        lines.append(
            LineInput(
                description=description,
                hsn_or_sac_code=(hsns[i] if i < len(hsns) else "").strip(),
                qty=_to_decimal(qtys[i] if i < len(qtys) else "0", "quantity"),
                rate=_to_decimal(rates[i] if i < len(rates) else "0", "rate"),
                discount_percent=_to_decimal(
                    discounts[i] if i < len(discounts) and discounts[i] else "0", "discount"
                ),
                gst_rate=_to_decimal(gst_rates[i] if i < len(gst_rates) else "0", "GST rate"),
                product_id=int(product_ids[i]) if i < len(product_ids) and product_ids[i] else None,
                unit=(units[i] if i < len(units) and units[i] else "pcs"),
            )
        )
    if not lines:
        raise InvoiceValidationError("Add at least one item to the invoice.")
    return lines


def create_invoice(tenant: Tenant, created_by, data: InvoiceInput) -> Invoice:
    if data.document_type not in allowed_document_types(tenant.registration_type):
        raise InvoiceValidationError(
            f"{tenant.legal_name} is registered as {tenant.registration_type.value} "
            "and cannot issue this document type."
        )

    gstin = Gstin.query.filter_by(id=data.gstin_id, tenant_id=tenant.id, is_active=True).first()
    if not gstin:
        raise InvoiceValidationError("Select a valid GSTIN to bill from.")

    customer = Customer.query.filter_by(id=data.customer_id, tenant_id=tenant.id).first()
    if not customer:
        raise InvoiceValidationError("Select a valid customer.")

    charge_gst = data.document_type != DocumentType.BILL_OF_SUPPLY
    is_intra_state = data.place_of_supply_state_code == gstin.state_code

    computed_lines = []
    for line in data.lines:
        computed = compute_line(
            qty=line.qty,
            rate=line.rate,
            discount_percent=line.discount_percent,
            gst_rate=line.gst_rate,
            is_intra_state=is_intra_state,
            charge_gst=charge_gst,
        )
        computed_lines.append({**computed, "input": line})

    totals = compute_invoice_totals(computed_lines)

    series = get_or_create_series(
        tenant.id, gstin.id, data.document_type, financial_year_for(data.invoice_date)
    )
    invoice_number = allocate_number(series)

    invoice = Invoice(
        tenant_id=tenant.id,
        gstin_id=gstin.id,
        invoice_series_id=series.id,
        invoice_number=invoice_number,
        document_type=data.document_type,
        customer_id=customer.id,
        customer_snapshot={
            "name": customer.name,
            "gstin": customer.gstin,
            "address_line1": customer.address_line1,
            "address_line2": customer.address_line2,
            "city": customer.city,
            "state_code": customer.state_code,
            "state_name": customer.state_name,
            "pincode": customer.pincode,
        },
        place_of_supply_state_code=data.place_of_supply_state_code,
        invoice_date=data.invoice_date,
        status=InvoiceStatus.ISSUED,
        notes=data.notes,
        created_by_id=created_by.id,
        **totals,
    )
    db.session.add(invoice)
    db.session.flush()

    for order, item in enumerate(computed_lines):
        line_in = item["input"]
        db.session.add(
            InvoiceLine(
                invoice_id=invoice.id,
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

    return invoice


def void_invoice(invoice: Invoice, voided_by, reason: str) -> None:
    if invoice.status == InvoiceStatus.VOID:
        raise InvoiceValidationError("Invoice is already void.")
    if not reason or not reason.strip():
        raise InvoiceValidationError("A reason is required to void an invoice.")
    invoice.status = InvoiceStatus.VOID
    invoice.voided_by_id = voided_by.id
    invoice.void_reason = reason.strip()
    invoice.voided_at = utcnow()
