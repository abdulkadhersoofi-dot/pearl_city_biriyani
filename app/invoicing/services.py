from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.models.customer import Customer
from app.models.invoice import Invoice, InvoiceLine, InvoiceStatus
from app.models.invoice_series import DocumentType
from app.models.mixins import utcnow
from app.models.tenant import Gstin, Tenant
from app.models.user import BranchType, User, UserRole
from app.pos.stock import allocate_stock
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
    place_of_supply_state_code: str
    invoice_date: date
    notes: str | None
    customer_id: int | None = None
    # Billing a branch instead of an ordinary Customer - exactly one of
    # customer_id/branch_user_id is set (create_invoice enforces this).
    # The document type and GST treatment below are never trusted from
    # the caller for a branch invoice - create_invoice resolves them
    # itself from the branch's own branch_type.
    branch_user_id: int | None = None
    lines: list[LineInput] = field(default_factory=list)


def _to_decimal(value: str, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise InvoiceValidationError(f"'{field_name}' must be a number.")


# Mirrors the column sizes in app.models.invoice.InvoiceLine - checked
# here so a too-long value becomes a clean form error instead of an
# unhandled 500 from the database's own length constraint.
_MAX_DESCRIPTION_LENGTH = 255
_MAX_HSN_LENGTH = 8
_MAX_UNIT_LENGTH = 20


def _bounded(value: str, field_name: str, max_length: int) -> str:
    if len(value) > max_length:
        raise InvoiceValidationError(f"'{field_name}' can be at most {max_length} characters.")
    return value


def parse_line_arrays(
    descriptions, hsns, qtys, rates, discounts, gst_rates, product_ids, units
) -> list[LineInput]:
    lines = []
    for i, description in enumerate(descriptions):
        description = (description or "").strip()
        if not description:
            continue
        hsn = (hsns[i] if i < len(hsns) else "").strip()
        unit = (units[i] if i < len(units) and units[i] else "pcs").strip()
        lines.append(
            LineInput(
                description=_bounded(description, "Item description", _MAX_DESCRIPTION_LENGTH),
                hsn_or_sac_code=_bounded(hsn, "HSN/SAC code", _MAX_HSN_LENGTH),
                qty=_to_decimal(qtys[i] if i < len(qtys) else "0", "quantity"),
                rate=_to_decimal(rates[i] if i < len(rates) else "0", "rate"),
                discount_percent=_to_decimal(
                    discounts[i] if i < len(discounts) and discounts[i] else "0", "discount"
                ),
                gst_rate=_to_decimal(gst_rates[i] if i < len(gst_rates) else "0", "GST rate"),
                product_id=int(product_ids[i]) if i < len(product_ids) and product_ids[i] else None,
                unit=_bounded(unit, "Unit", _MAX_UNIT_LENGTH),
            )
        )
    if not lines:
        raise InvoiceValidationError("Add at least one item to the invoice.")
    return lines


def create_invoice(tenant: Tenant, created_by, data: InvoiceInput) -> Invoice:
    branch = None
    if data.branch_user_id:
        branch = User.query.filter_by(id=data.branch_user_id, tenant_id=tenant.id, role=UserRole.STAFF).first()
        if not branch:
            raise InvoiceValidationError("Select a valid branch.")
        # Never the operator's choice: a PCB-owned branch is an internal
        # stock transfer (Delivery Challan, no GST); a third-party branch
        # is a real sale (Tax Invoice, GST as normal). Whatever document
        # type the caller passed in is overridden here so this can never
        # drift from the branch's own type.
        document_type = DocumentType.DELIVERY_CHALLAN if branch.branch_type == BranchType.OWNED else DocumentType.TAX_INVOICE
    elif data.customer_id:
        document_type = data.document_type
    else:
        raise InvoiceValidationError("Select a customer or a branch to bill.")

    if document_type not in allowed_document_types(tenant.registration_type):
        raise InvoiceValidationError(
            f"{tenant.legal_name} is registered as {tenant.registration_type.value} "
            "and cannot issue this document type."
        )

    gstin = Gstin.query.filter_by(id=data.gstin_id, tenant_id=tenant.id, is_active=True).first()
    if not gstin:
        raise InvoiceValidationError("Select a valid GSTIN to bill from.")

    customer = None
    if branch:
        customer_snapshot = {
            "name": branch.name,
            "gstin": None,
            "address_line1": "",
            "address_line2": "",
            "city": "",
            "state_code": "",
            "state_name": "",
            "pincode": "",
        }
    else:
        customer = Customer.query.filter_by(id=data.customer_id, tenant_id=tenant.id).first()
        if not customer:
            raise InvoiceValidationError("Select a valid customer.")
        customer_snapshot = {
            "name": customer.name,
            "gstin": customer.gstin,
            "address_line1": customer.address_line1,
            "address_line2": customer.address_line2,
            "city": customer.city,
            "state_code": customer.state_code,
            "state_name": customer.state_name,
            "pincode": customer.pincode,
        }

    # A branch-owned (internal) transfer never carries GST, same as a
    # Bill of Supply - everything else charges GST as normal.
    charge_gst = document_type not in (DocumentType.BILL_OF_SUPPLY, DocumentType.DELIVERY_CHALLAN)
    # Under reverse charge, the recipient - not this tenant - pays the tax
    # shown on this invoice. It's still computed the normal way below, but
    # kept out of the cgst/sgst/igst columns a Tax Invoice uses (those feed
    # GSTR-3B's own output-tax-payable figure) and stored in the separate
    # rcgst/rsgst/rigst columns instead.
    is_rcm = document_type == DocumentType.RCM_INVOICE
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
    if is_rcm:
        totals["total_rcgst"] = totals.pop("total_cgst")
        totals["total_rsgst"] = totals.pop("total_sgst")
        totals["total_rigst"] = totals.pop("total_igst")
        totals["total_cgst"] = Decimal("0.00")
        totals["total_sgst"] = Decimal("0.00")
        totals["total_igst"] = Decimal("0.00")
    else:
        totals["total_rcgst"] = Decimal("0.00")
        totals["total_rsgst"] = Decimal("0.00")
        totals["total_rigst"] = Decimal("0.00")

    series = get_or_create_series(
        tenant.id, gstin.id, document_type, financial_year_for(data.invoice_date)
    )
    invoice_number = allocate_number(series)

    invoice = Invoice(
        tenant_id=tenant.id,
        gstin_id=gstin.id,
        invoice_series_id=series.id,
        invoice_number=invoice_number,
        document_type=document_type,
        customer_id=customer.id if customer else None,
        branch_user_id=branch.id if branch else None,
        customer_snapshot=customer_snapshot,
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
        cgst_amount = sgst_amount = igst_amount = Decimal("0.00")
        rcgst_amount = rsgst_amount = rigst_amount = Decimal("0.00")
        if is_rcm:
            rcgst_amount, rsgst_amount, rigst_amount = item["cgst_amount"], item["sgst_amount"], item["igst_amount"]
        else:
            cgst_amount, sgst_amount, igst_amount = item["cgst_amount"], item["sgst_amount"], item["igst_amount"]
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
                cgst_amount=cgst_amount,
                sgst_amount=sgst_amount,
                igst_amount=igst_amount,
                rcgst_amount=rcgst_amount,
                rsgst_amount=rsgst_amount,
                rigst_amount=rigst_amount,
                line_total=item["line_total"],
                sort_order=order,
            )
        )

    if branch:
        # Billing this invoice to a branch is how stock physically reaches
        # it - this is the only place that happens, there is no separate
        # "send stock" action any more. An ad-hoc line (no catalog
        # product) has nothing to track against, so it's skipped.
        stock_lines = [(line.product_id, line.qty) for line in data.lines if line.product_id]
        if stock_lines:
            allocate_stock(tenant.id, branch.id, created_by.id, stock_lines, on_date=data.invoice_date)

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
