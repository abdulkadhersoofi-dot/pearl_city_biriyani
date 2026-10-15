"""Margin scheme - Rule 32(5) of the CGST Rules, for dealers in second-hand
goods (used phones, used vehicles...).

GST is paid only on the dealer's margin, never on the whole price:

  margin          = selling price - purchase price
  margin <= 0     -> taxable value 0, GST 0 (a loss is ignored; tax is
                     never negative)
  the margin is treated as tax-inclusive:
  taxable margin  = margin x 100 / (100 + rate)
  GST             = margin - taxable margin
                    split CGST + SGST (intra-state) or IGST (inter-state)

The buyer is never charged GST separately - they pay the selling price.
What isn't margin (selling price - margin) is disclosed in the HSN
summary as a 0% row (see app.reports.gstr). Purchases of margin-scheme
goods never give input tax credit (app.purchases.services).

All amounts are Decimal, rounded half-up to the paisa. A sale line can
draw on several purchase lots at different prices (e.g. 3 used units
bought on different days); the margin is worked out lot by lot, so a
loss on one unit never cancels out the tax on another.
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from app.utils.gst import allocate_amount, round_money

ZERO = Decimal("0.00")
DEFAULT_DECLARATION = (
    "Supply of second-hand goods under the margin scheme - GST is paid on the margin as per "
    "Rule 32(5) of the CGST Rules, 2017. Input tax credit has not been availed on these goods, "
    "and no GST is charged separately on this invoice."
)
HUNDRED = Decimal("100")


@dataclass
class MarginChunk:
    """The part of a sale line that came from one purchase lot."""

    qty: Decimal
    sale_value: Decimal
    purchase_value: Decimal


def compute_margin(sale_value, purchase_value, gst_rate, is_intra_state: bool) -> dict:
    """Margin and the GST in it for one lot of a sale."""
    sale_value = round_money(Decimal(sale_value))
    purchase_value = round_money(Decimal(purchase_value))
    rate = Decimal(gst_rate or 0)
    raw_margin = sale_value - purchase_value
    margin = raw_margin if raw_margin > 0 else ZERO
    taxable = round_money(margin * HUNDRED / (HUNDRED + rate)) if margin > 0 else ZERO
    tax = margin - taxable
    if is_intra_state:
        cgst = (tax / 2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        sgst, igst = tax - cgst, ZERO
    else:
        cgst, sgst, igst = ZERO, ZERO, tax
    return {
        "sale_value": sale_value,
        "purchase_value": purchase_value,
        "raw_margin": raw_margin,
        "margin": margin,
        "taxable_value": taxable,
        "cgst_amount": cgst,
        "sgst_amount": sgst,
        "igst_amount": igst,
        "tax": tax,
    }


def split_sale_value(line_total, qtys: list[Decimal]) -> list[Decimal]:
    """A sale line's total spread over its lots by quantity, to the paisa,
    always adding back to the line total."""
    return allocate_amount([Decimal(q) for q in qtys], Decimal(line_total))


def compute_margin_line(chunks: list[MarginChunk], gst_rate, is_intra_state: bool) -> dict:
    """A whole sale line: the per-lot margins summed. `line_total` is what
    the buyer pays (the selling price - no GST is added on top);
    `remainder` is the part of it that isn't margin (the HSN 0% row)."""
    parts = [compute_margin(c.sale_value, c.purchase_value, gst_rate, is_intra_state) for c in chunks]
    total = {key: sum((p[key] for p in parts), ZERO) for key in
             ("sale_value", "purchase_value", "margin", "taxable_value", "cgst_amount", "sgst_amount", "igst_amount", "tax")}
    total["line_total"] = total.pop("sale_value")
    total["remainder"] = total["line_total"] - total["margin"]
    total["parts"] = parts
    return total


def declaration_for(tenant) -> str:
    return (getattr(tenant, "margin_scheme_declaration", None) or "").strip() or DEFAULT_DECLARATION


def compute_margin_totals(lines: list[dict]) -> dict:
    """Document totals for a margin-scheme invoice. Unlike a tax invoice,
    the buyer pays the lines' selling prices - no GST is added on top - so
    the grand total is their sum (rounded to the rupee); the taxable value
    and tax are the margins'. Same keys as gst.compute_invoice_totals."""
    exact = sum((Decimal(l["line_total"]) for l in lines), ZERO)
    discount = sum((Decimal(l["discount_amount"]) for l in lines), ZERO)
    grand_total = exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return {
        "subtotal": round_money(exact + discount),
        "total_discount": round_money(discount),
        "total_taxable_value": round_money(sum((Decimal(l["taxable_value"]) for l in lines), ZERO)),
        "total_cgst": round_money(sum((Decimal(l["cgst_amount"]) for l in lines), ZERO)),
        "total_sgst": round_money(sum((Decimal(l["sgst_amount"]) for l in lines), ZERO)),
        "total_igst": round_money(sum((Decimal(l["igst_amount"]) for l in lines), ZERO)),
        "round_off": round_money(grand_total - exact),
        "grand_total": round_money(grand_total),
    }
