"""GST calculation and document-type gating.

Keeps the "which tax split" and "which document types can this client
issue" decisions in one place so invoicing, POS, and notes never disagree.
"""

from decimal import Decimal, ROUND_HALF_UP

from app.models.invoice_series import DocumentType
from app.models.tenant import RegistrationType
from app.utils import gst_config

TWO_PLACES = Decimal("0.01")

# The official GSTIN structure (every GSTIN input field in the app
# validates against this, nothing else is accepted):
#   - 2 digits: state code
#   - 10 chars: PAN (5 letters, 4 digits, 1 letter)
#   - 1 char: entity number under that PAN (1-9 or A-Z)
#   - literal "Z"
#   - 1 char: checksum (alphanumeric)
# This checks structure/format only, not the actual check-digit algorithm -
# "NIL", "-", "N/A" and the like are rejected by this just as reliably as a
# malformed real-looking GSTIN would be. A customer/client with no GSTIN
# must leave the field blank; nothing else means "unregistered".
GSTIN_REGEX = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$"


def normalize_gstin(value):
    """WTForms filter: blank/whitespace becomes None (not a stored empty
    string a later `bool()` check could misread as "has a GSTIN"), and a
    real GSTIN is uppercased before the Regexp validator checks its
    format - so e.g. a lowercase-typed GSTIN or one with stray
    leading/trailing whitespace isn't rejected on a technicality."""
    return (value or "").strip().upper() or None

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


def taxable_from_inclusive(inclusive_amount: Decimal, gst_rate: Decimal) -> Decimal:
    """The pre-tax value hidden inside a tax-inclusive amount:
    amount x 100 / (100 + rate), to the paisa."""
    gst_rate = Decimal(gst_rate or 0)
    return round_money(Decimal(inclusive_amount) * Decimal(100) / (Decimal(100) + gst_rate))


def compute_line(
    qty: Decimal,
    rate: Decimal,
    discount_percent: Decimal,
    gst_rate: Decimal,
    is_intra_state: bool,
    charge_gst: bool = True,
    price_includes_tax: bool = False,
    discount_flat: Decimal = Decimal("0"),
) -> dict:
    """Computes one invoice line's taxable value, tax split and total.

    charge_gst=False is used for Bill of Supply (composition/unregistered
    clients never charge GST on the invoice, even if the item has a rate
    on file for internal costing).

    price_includes_tax=True treats `rate` as the shelf price the customer
    pays, GST already inside it (restaurants, retail with MRP). The
    taxable value is then backed out of the discounted inclusive amount
    and the tax is the exact remainder, so the line total is always the
    inclusive amount to the paisa - the number on the menu/tag is the
    number on the bill. discount_amount is always reported pre-tax
    either way, so subtotal (taxable + discount) means the same thing on
    every document.

    discount_flat is rupees off the line on its own price basis (pre-tax
    for an exclusive price, inclusive for an inclusive one), taken after
    any percentage - see price_basis_amount() to turn "Rs. 10 off what
    the customer pays" into this. Callers must keep it within the line.
    """
    qty = Decimal(qty)
    rate = Decimal(rate)
    discount_percent = Decimal(discount_percent or 0)
    discount_flat = Decimal(discount_flat or 0)
    gst_rate = Decimal(gst_rate or 0)

    if not charge_gst:
        gst_rate = Decimal(0)

    gross = qty * rate
    if price_includes_tax:
        gross_inclusive = round_money(gross)
        net_inclusive = round_money(
            gross_inclusive - round_money(gross_inclusive * discount_percent / Decimal(100)) - discount_flat
        )
        taxable_value = taxable_from_inclusive(net_inclusive, gst_rate)
        discount_amount = taxable_from_inclusive(gross_inclusive, gst_rate) - taxable_value
        tax_total = net_inclusive - taxable_value
    else:
        discount_amount = round_money(gross * discount_percent / Decimal(100)) + round_money(discount_flat)
        taxable_value = round_money(gross - discount_amount)
        tax_total = round_money(taxable_value * gst_rate / Decimal(100))

    if not charge_gst or tax_total == 0:
        cgst = sgst = igst = Decimal("0.00")
    elif is_intra_state:
        if price_includes_tax:
            # Split the exact remainder rather than re-rounding each half,
            # so CGST + SGST never drifts a paisa off the inclusive total
            # (they may differ from each other by 0.01 - within the GST
            # portal's per-head tolerance).
            cgst = round_money(tax_total / 2)
            sgst = tax_total - cgst
        else:
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


def line_gross(qty: Decimal, rate: Decimal) -> Decimal:
    """qty x rate to the paisa - the amount a line's discounts come off."""
    return round_money(Decimal(qty) * Decimal(rate))


def price_basis_amount(payable: Decimal, gst_rate: Decimal, price_includes_tax: bool, charge_gst: bool = True) -> Decimal:
    """Rupees off what the customer pays -> rupees off the line's own
    price basis (compute_line's discount_flat). A cashier saying "Rs. 50
    off" means off the total; on a pre-tax price that's Rs. 50 less the
    GST it would have carried."""
    payable = Decimal(payable or 0)
    if price_includes_tax or not charge_gst:
        return round_money(payable)
    return taxable_from_inclusive(payable, gst_rate)


def allocate_amount(weights: list[Decimal], amount: Decimal) -> list[Decimal]:
    """Splits `amount` over lines in proportion to `weights` (each line's
    payable value), to the paisa, with the rounding remainder on the
    heaviest line so the parts always add back to exactly `amount`.
    Used to spread a bill-level discount over the bill's lines - tax is
    always computed per line."""
    amount = round_money(Decimal(amount or 0))
    weights = [Decimal(w or 0) for w in weights]
    total = sum(weights, Decimal("0"))
    if not weights or total <= 0 or amount == 0:
        return [Decimal("0.00")] * len(weights)
    parts = [round_money(amount * w / total) for w in weights]
    heaviest = max(range(len(weights)), key=lambda i: weights[i])
    parts[heaviest] += amount - sum(parts, Decimal("0"))
    return parts


_LINE_AMOUNT_FIELDS = ("discount_amount", "taxable_value", "cgst_amount", "sgst_amount", "igst_amount", "line_total")


def return_line_amounts(
    original: dict,
    original_qty: Decimal,
    already_returned: dict,
    already_returned_qty: Decimal,
    return_qty: Decimal,
) -> dict:
    """Amounts for goods coming back against one sold line (a POS sales
    return / credit-note equivalent).

    `original` holds the sold line's inputs (rate, discount_percent,
    discount_flat, gst_rate, price_includes_tax, is_intra_state) and its
    stored amounts (_LINE_AMOUNT_FIELDS). A part return is recomputed with
    compute_line from the same inputs, the rupee discount pro-rated by
    quantity; the return that brings the line back to zero takes exactly
    what is left, so however the goods come back, the returns add up to
    the original line to the paisa - never more.
    """
    original_qty = Decimal(original_qty)
    return_qty = Decimal(return_qty)
    already_returned_qty = Decimal(already_returned_qty or 0)
    remaining_qty = original_qty - already_returned_qty
    if return_qty <= 0 or return_qty > remaining_qty:
        raise ValueError("Return quantity must be more than 0 and no more than what is left on the bill.")

    if return_qty == remaining_qty:
        return {f: round_money(Decimal(original[f]) - Decimal(already_returned.get(f) or 0)) for f in _LINE_AMOUNT_FIELDS}

    flat = round_money(Decimal(original.get("discount_flat") or 0) * return_qty / original_qty)
    gst_rate = Decimal(original.get("gst_rate") or 0)
    return compute_line(
        qty=return_qty,
        rate=original["rate"],
        discount_percent=original.get("discount_percent") or 0,
        gst_rate=gst_rate,
        is_intra_state=original.get("is_intra_state", True),
        charge_gst=gst_rate > 0,
        price_includes_tax=bool(original.get("price_includes_tax")),
        discount_flat=flat,
    )


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


def hsn_min_digits(aato) -> int:
    """Minimum HSN/SAC digits this taxpayer must show, by its Aggregate
    Annual Turnover (preceding FY) - see gst_config for the source. An
    unknown AATO (not yet entered) gets the lower bracket's minimum."""
    if aato is not None and Decimal(aato) > gst_config.HSN_AATO_THRESHOLD:
        return gst_config.HSN_MIN_DIGITS_ABOVE_THRESHOLD
    return gst_config.HSN_MIN_DIGITS_UP_TO_THRESHOLD


def hsn_error(code, aato=None, is_service: bool = False) -> str | None:
    """None if `code` is an acceptable HSN (goods) / SAC (services) code
    for a taxpayer with this AATO, otherwise a plain-language reason."""
    code = (code or "").strip()
    label = "SAC code" if is_service else "HSN code"
    if not code.isdigit():
        return f"{label} must be digits only."
    if len(code) not in gst_config.HSN_VALID_LENGTHS:
        return f"{label} must be 4, 6 or 8 digits (a missing leading zero? e.g. 0401, not 401)."
    minimum = hsn_min_digits(aato)
    if len(code) < minimum:
        return (
            f"{label} needs at least {minimum} digits for a business with turnover above "
            f"Rs. {gst_config.HSN_AATO_THRESHOLD / 10000000:.0f} crore."
        )
    if is_service and not code.startswith(gst_config.SAC_PREFIX):
        return f"A service's SAC code starts with {gst_config.SAC_PREFIX} (e.g. 998719)."
    return None


def inclusive_price(price: Decimal, gst_rate: Decimal, price_includes_tax: bool) -> Decimal:
    """What the customer actually pays for one unit at this price."""
    price = Decimal(price or 0)
    if price_includes_tax:
        return round_money(price)
    return round_money(price * (Decimal(100) + Decimal(gst_rate or 0)) / Decimal(100))


def exceeds_mrp(price: Decimal, gst_rate: Decimal, price_includes_tax: bool, mrp) -> bool:
    """Selling above MRP (which is always tax-inclusive) is barred by the
    Legal Metrology (Packaged Commodities) Rules - True if this price,
    once GST is on it, would be more than the MRP."""
    if mrp is None or Decimal(mrp) <= 0:
        return False
    return inclusive_price(price, gst_rate, price_includes_tax) > round_money(Decimal(mrp))
