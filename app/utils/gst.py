"""GST calculation and document-type gating.

Keeps the "which tax split" and "which document types can this client
issue" decisions in one place so invoicing, POS, and notes never disagree.
"""

from decimal import Decimal, ROUND_HALF_UP

from app.models.invoice_series import DocumentType
from app.models.tenant import RegistrationType

TWO_PLACES = Decimal("0.01")

# Document types a tenant is allowed to issue, gated by registration type.
# Never left to the operator's judgement - the invoicing UI only offers
# what this table allows for the tenant's registration_type.
ALLOWED_DOCUMENT_TYPES = {
    RegistrationType.REGULAR: {
        DocumentType.TAX_INVOICE,
        DocumentType.EXPORT_INVOICE_IGST,
        DocumentType.EXPORT_INVOICE_LUT,
        DocumentType.RCM_INVOICE,
        DocumentType.DELIVERY_CHALLAN,
        DocumentType.RECEIPT_VOUCHER,
        DocumentType.REFUND_VOUCHER,
        DocumentType.CREDIT_NOTE,
        DocumentType.DEBIT_NOTE,
        DocumentType.POS_BILL,
    },
    RegistrationType.COMPOSITION: {
        DocumentType.BILL_OF_SUPPLY,
        DocumentType.DELIVERY_CHALLAN,
        DocumentType.RECEIPT_VOUCHER,
        DocumentType.REFUND_VOUCHER,
        DocumentType.CREDIT_NOTE,
        DocumentType.DEBIT_NOTE,
        DocumentType.POS_BILL,
    },
    RegistrationType.UNREGISTERED: {
        DocumentType.BILL_OF_SUPPLY,
        DocumentType.DELIVERY_CHALLAN,
        DocumentType.RECEIPT_VOUCHER,
        DocumentType.REFUND_VOUCHER,
        DocumentType.CREDIT_NOTE,
        DocumentType.DEBIT_NOTE,
        DocumentType.POS_BILL,
    },
}


def allowed_document_types(registration_type: RegistrationType) -> set[DocumentType]:
    return ALLOWED_DOCUMENT_TYPES.get(registration_type, set())


def round_money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def compute_line(
    qty: Decimal,
    rate: Decimal,
    discount_percent: Decimal,
    gst_rate: Decimal,
    is_intra_state: bool,
    charge_gst: bool = True,
) -> dict:
    """Computes one invoice line's taxable value, tax split and total.

    charge_gst=False is used for Bill of Supply (composition/unregistered
    clients never charge GST on the invoice, even if the item has a rate
    on file for internal costing).
    """
    qty = Decimal(qty)
    rate = Decimal(rate)
    discount_percent = Decimal(discount_percent or 0)
    gst_rate = Decimal(gst_rate or 0)

    gross = qty * rate
    discount_amount = round_money(gross * discount_percent / Decimal(100))
    taxable_value = round_money(gross - discount_amount)

    if not charge_gst:
        gst_rate = Decimal(0)

    tax_total = round_money(taxable_value * gst_rate / Decimal(100))

    if not charge_gst or tax_total == 0:
        cgst = sgst = igst = Decimal("0.00")
    elif is_intra_state:
        half = round_money(tax_total / 2)
        cgst = sgst = half
        igst = Decimal("0.00")
    else:
        cgst = sgst = Decimal("0.00")
        igst = tax_total

    line_total = round_money(taxable_value + cgst + sgst + igst)

    return {
        "discount_amount": discount_amount,
        "taxable_value": taxable_value,
        "cgst_amount": cgst,
        "sgst_amount": sgst,
        "igst_amount": igst,
        "line_total": line_total,
    }


def compute_invoice_totals(lines: list[dict]) -> dict:
    """Sums computed lines and applies a single invoice-level round-off,
    per standard GST rounding practice (round the final total, keep line
    figures exact)."""
    subtotal = sum((Decimal(l["taxable_value"]) + Decimal(l["discount_amount"]) for l in lines), Decimal("0"))
    total_discount = sum((Decimal(l["discount_amount"]) for l in lines), Decimal("0"))
    total_taxable_value = sum((Decimal(l["taxable_value"]) for l in lines), Decimal("0"))
    total_cgst = sum((Decimal(l["cgst_amount"]) for l in lines), Decimal("0"))
    total_sgst = sum((Decimal(l["sgst_amount"]) for l in lines), Decimal("0"))
    total_igst = sum((Decimal(l["igst_amount"]) for l in lines), Decimal("0"))

    exact_total = total_taxable_value + total_cgst + total_sgst + total_igst
    grand_total = exact_total.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    round_off = round_money(grand_total - exact_total)

    return {
        "subtotal": round_money(subtotal),
        "total_discount": round_money(total_discount),
        "total_taxable_value": round_money(total_taxable_value),
        "total_cgst": round_money(total_cgst),
        "total_sgst": round_money(total_sgst),
        "total_igst": round_money(total_igst),
        "round_off": round_off,
        "grand_total": round_money(grand_total),
    }
