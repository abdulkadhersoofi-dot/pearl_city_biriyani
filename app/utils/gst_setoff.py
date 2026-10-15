"""Paying GSTR-3B: setting input tax credit off against output tax
(s.49(5) CGST Act and Rule 88A, in force since 01.02.2019), and what is
left to pay in cash.

  1. IGST credit is used first - against IGST output tax, then against
     CGST and SGST output tax (in any proportion; here it is spread so the
     cash left to pay is as small as possible).
  2. CGST credit: against CGST, then IGST. Never against SGST.
  3. SGST credit: against SGST, then IGST. Never against CGST.
  4. Whatever output tax is left is paid in cash. Tax on reverse-charge
     purchases (3.1(d)) is always paid in cash - no credit can be used for
     it (s.49(4) read with the definition of output tax).
  5. Credit left over is carried forward to the next month.

All amounts Decimal; nothing is rounded here (inputs are already to the
paisa)."""

from decimal import Decimal

ZERO = Decimal("0.00")
HEADS = ("igst", "cgst", "sgst")


def _d(value) -> Decimal:
    return Decimal(value or 0)


def set_off(output: dict, credit: dict, rcm: dict | None = None) -> dict:
    """output / credit / rcm: {"igst", "cgst", "sgst"} amounts.

    Returns the utilisation matrix ({credit_head: {liability_head: amount}}),
    cash payable per head (output left + RCM), credit carried forward."""
    liab = {h: max(_d(output.get(h)), ZERO) for h in HEADS}
    cred = {h: max(_d(credit.get(h)), ZERO) for h in HEADS}
    used = {c: {l: ZERO for l in HEADS} for c in HEADS}

    def use(c, l, cap=None):
        amount = min(cred[c], liab[l]) if cap is None else min(cred[c], liab[l], cap)
        if amount > 0:
            used[c][l] += amount
            cred[c] -= amount
            liab[l] -= amount

    # 1. IGST credit: IGST first, then CGST / SGST - the part of each that
    # its own credit can't cover first, then the rest.
    use("igst", "igst")
    for head in ("cgst", "sgst"):
        use("igst", head, cap=max(liab[head] - cred[head], ZERO))
    use("igst", "cgst")
    use("igst", "sgst")
    # 2-3. CGST / SGST credit: own head, then IGST.
    use("cgst", "cgst")
    use("sgst", "sgst")
    use("cgst", "igst")
    use("sgst", "igst")

    rcm = {h: max(_d((rcm or {}).get(h)), ZERO) for h in HEADS}
    cash = {h: liab[h] + rcm[h] for h in HEADS}
    return {
        "output": {h: max(_d(output.get(h)), ZERO) for h in HEADS},
        "credit": {h: max(_d(credit.get(h)), ZERO) for h in HEADS},
        "utilised": used,
        "paid_by_credit": {l: sum((used[c][l] for c in HEADS), ZERO) for l in HEADS},
        "output_in_cash": dict(liab),
        "rcm_in_cash": rcm,
        "cash": cash,
        "cash_total": sum(cash.values(), ZERO),
        "carry_forward": dict(cred),
    }
