"""Purchase-side GST: what a purchase bill costs, what of its GST comes
back as input tax credit (ITC), and what each unit of stock really cost.

The line arithmetic (taxable value, CGST/SGST or IGST, rounding) is the
same app.utils.gst.compute_line the sales side uses - a purchase line is
computed exactly like a sale line. What is purchase-specific lives here:

  - ITC eligibility per line (s.16-17 CGST Act). ELIGIBLE lines give
    their GST back as credit; INELIGIBLE and BLOCKED (s.17(5)) lines
    don't, and that GST becomes part of what the goods cost.
  - Reverse charge (s.9(3)/9(4)): the supplier doesn't charge GST; the
    buyer pays it to the government and (if eligible) claims it back.
    So an RCM bill's payable-to-supplier total excludes the tax, while
    the tax is still computed, reported and (if eligible) credited.
  - Landed cost: freight and other charges on the bill are spread over
    the lines by taxable value (gst.allocate_amount), so stock carries
    its true cost into the weighted average.
"""

from decimal import Decimal, ROUND_HALF_UP

from app.models.purchase import ItcEligibility
from app.utils.gst import allocate_amount, compute_invoice_totals, compute_line, round_money

ZERO = Decimal("0")
UNIT_COST_PLACES = Decimal("0.0001")


def compute_purchase_line(qty, rate, discount_percent, gst_rate, is_intra_state: bool) -> dict:
    """A supplier's line, priced before tax as purchase bills are."""
    return compute_line(
        qty=qty,
        rate=rate,
        discount_percent=discount_percent,
        gst_rate=gst_rate,
        is_intra_state=is_intra_state,
        charge_gst=True,
        price_includes_tax=False,
    )


def claimable(line: dict, eligibility: ItcEligibility) -> tuple[Decimal, Decimal, Decimal]:
    """(CGST, SGST, IGST) of this line that comes back as input tax credit."""
    if eligibility != ItcEligibility.ELIGIBLE:
        return ZERO, ZERO, ZERO
    return Decimal(line["cgst_amount"]), Decimal(line["sgst_amount"]), Decimal(line["igst_amount"])


def line_tax(line: dict) -> Decimal:
    return Decimal(line["cgst_amount"]) + Decimal(line["sgst_amount"]) + Decimal(line["igst_amount"])


def purchase_totals(lines: list[dict], eligibilities: list[ItcEligibility], other_charges, is_rcm: bool) -> dict:
    """Bill totals plus the ITC it gives.

    grand_total is what is owed to the supplier: taxable + GST + other
    charges, rounded to the rupee - except under reverse charge, where the
    supplier charges no GST, so it's taxable + other charges only."""
    other_charges = round_money(Decimal(other_charges or 0))
    base = compute_invoice_totals(lines)
    tax = base["total_cgst"] + base["total_sgst"] + base["total_igst"]
    exact = base["total_taxable_value"] + other_charges + (ZERO if is_rcm else tax)
    grand_total = exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    itc = [claimable(line, e) for line, e in zip(lines, eligibilities)]
    return {
        "subtotal": base["subtotal"],
        "total_discount": base["total_discount"],
        "total_taxable_value": base["total_taxable_value"],
        "total_cgst": base["total_cgst"],
        "total_sgst": base["total_sgst"],
        "total_igst": base["total_igst"],
        "round_off": round_money(grand_total - exact),
        "grand_total": round_money(grand_total),
        "itc_cgst": round_money(sum((c for c, _, _ in itc), ZERO)),
        "itc_sgst": round_money(sum((s for _, s, _ in itc), ZERO)),
        "itc_igst": round_money(sum((i for _, _, i in itc), ZERO)),
    }


def landed_costs(lines: list[dict], qtys: list[Decimal], eligibilities: list[ItcEligibility], other_charges) -> list[tuple[Decimal, Decimal]]:
    """[(this line's share of the other charges, cost of one unit)].

    Cost of a unit = (taxable value + share of charges + any GST that
    won't come back as credit) / quantity. Credit-able GST isn't a cost -
    it's recovered from the government."""
    shares = allocate_amount([Decimal(l["taxable_value"]) for l in lines], Decimal(other_charges or 0))
    result = []
    for line, qty, eligibility, share in zip(lines, qtys, eligibilities, shares):
        qty = Decimal(qty)
        lost_tax = ZERO if eligibility == ItcEligibility.ELIGIBLE else line_tax(line)
        total = Decimal(line["taxable_value"]) + share + lost_tax
        unit = (total / qty).quantize(UNIT_COST_PLACES, rounding=ROUND_HALF_UP) if qty > 0 else ZERO
        result.append((share, unit))
    return result


def return_itc(line: dict, eligibility: ItcEligibility) -> tuple[Decimal, Decimal, Decimal]:
    """ITC to reverse for goods sent back - the credit their GST gave."""
    return claimable(line, eligibility)


def compute_expense_gst(amount, gst_rate, is_intra_state: bool, includes_gst: bool, is_rcm: bool,
                        eligibility: ItcEligibility, can_claim: bool) -> dict:
    """An expense's GST bill, kept to one rate: taxable value, the tax
    split, what the payee is paid, and the credit it gives.

    `amount` is the bill total with GST inside it (includes_gst) or the
    taxable value. Under reverse charge the payee never charges GST, so
    the amount is always the taxable value and the payee is paid just
    that; the tax is still computed (the business pays it) and credited
    if eligible. No credit at all when the business can't claim ITC
    (not a regular registration - see purchases.services.can_claim_itc)."""
    includes_gst = includes_gst and not is_rcm
    line = compute_line(qty=Decimal("1"), rate=Decimal(amount), discount_percent=ZERO, gst_rate=gst_rate,
                        is_intra_state=is_intra_state, charge_gst=True, price_includes_tax=includes_gst)
    tax = line_tax(line)
    taxable = Decimal(line["taxable_value"])
    cgst, sgst, igst = claimable(line, eligibility) if can_claim else (ZERO, ZERO, ZERO)
    return {
        "taxable_value": taxable,
        "cgst_amount": Decimal(line["cgst_amount"]),
        "sgst_amount": Decimal(line["sgst_amount"]),
        "igst_amount": Decimal(line["igst_amount"]),
        "amount": round_money(taxable if is_rcm else taxable + tax),
        "itc_cgst": cgst,
        "itc_sgst": sgst,
        "itc_igst": igst,
    }
