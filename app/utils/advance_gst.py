"""GST on advances received (s.13 CGST Act; Rule 50 receipt voucher,
Rule 51 refund voucher; GSTR-1 tables 11A / 11B).

  - Services: GST is due when the advance is received. The advance is
    taken as tax-inclusive: taxable = amount x 100 / (100 + rate), the
    rest is the tax, split CGST + SGST (half each, half-up to the paisa,
    SGST gets any odd paisa) or IGST by place of supply.
  - Goods: no GST on advances (Notification 66/2017-CT) - the voucher is
    still issued, with no tax.
  - When an advance is later set against an invoice (adjusted) or given
    back on a refund voucher, the tax already paid on that part comes off
    (11B). That part's tax is worked out "cumulatively": the tax on
    everything adjusted so far minus the tax on what was adjusted before
    it. So however an advance is split up, the pieces always add back to
    exactly the tax that was paid on it - never a paisa more.

All amounts Decimal, rounded half-up to the paisa.
"""

from decimal import Decimal, ROUND_HALF_UP

ZERO = Decimal("0.00")
HUNDRED = Decimal("100")
PAISA = Decimal("0.01")


def _money(value) -> Decimal:
    return Decimal(value).quantize(PAISA, rounding=ROUND_HALF_UP)


def compute_advance_gst(amount, gst_rate, is_intra_state: bool, for_services: bool = True) -> dict:
    """Tax in a tax-inclusive advance of `amount`."""
    amount = _money(amount)
    rate = Decimal(gst_rate or 0)
    if amount <= 0 or not for_services or rate <= 0:
        return {"amount": amount, "taxable_value": amount if amount > 0 else ZERO,
                "cgst_amount": ZERO, "sgst_amount": ZERO, "igst_amount": ZERO, "tax": ZERO}
    taxable = _money(amount * HUNDRED / (HUNDRED + rate))
    tax = amount - taxable
    if is_intra_state:
        cgst = _money(tax / 2)
        sgst, igst = tax - cgst, ZERO
    else:
        cgst, sgst, igst = ZERO, ZERO, tax
    return {"amount": amount, "taxable_value": taxable, "cgst_amount": cgst, "sgst_amount": sgst,
            "igst_amount": igst, "tax": tax}


_KEYS = ("taxable_value", "cgst_amount", "sgst_amount", "igst_amount", "tax")


def advance_portion(receipt_amount, receipt_tax: dict, used_before, part, gst_rate, is_intra_state: bool,
                    for_services: bool = True) -> dict:
    """The taxable value and tax in `part` of an advance, when `used_before`
    of it has already been adjusted / refunded. The last piece (bringing
    the total used up to the whole advance) takes exactly what's left of
    the tax recorded on the receipt, so any rounding lands nowhere else."""
    receipt_amount, used_before, part = _money(receipt_amount), _money(used_before), _money(part)
    if part <= 0:
        return {"amount": ZERO, **{k: ZERO for k in _KEYS}}
    if used_before + part > receipt_amount:
        raise ValueError("More than the advance would be adjusted.")
    if used_before + part == receipt_amount:
        before = advance_portion_total(receipt_amount, receipt_tax, used_before, gst_rate, is_intra_state, for_services)
        return {"amount": part, **{k: Decimal(receipt_tax[k]) - before[k] for k in _KEYS}}
    after = compute_advance_gst(used_before + part, gst_rate, is_intra_state, for_services)
    before = compute_advance_gst(used_before, gst_rate, is_intra_state, for_services)
    return {"amount": part, **{k: after[k] - before[k] for k in _KEYS}}


def advance_portion_total(receipt_amount, receipt_tax: dict, used, gst_rate, is_intra_state: bool,
                          for_services: bool = True) -> dict:
    """Taxable value and tax in the first `used` of an advance."""
    used = _money(used)
    if used >= _money(receipt_amount):
        return {k: Decimal(receipt_tax[k]) for k in _KEYS}
    return {k: v for k, v in compute_advance_gst(used, gst_rate, is_intra_state, for_services).items() if k in _KEYS}
