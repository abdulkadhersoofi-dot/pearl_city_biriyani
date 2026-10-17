"""Money as people in India read it: lakh / crore grouping (1,64,944.00)
and a short form for chart labels (1.6L, 45K, 2.1Cr). Display only - every
figure is still computed in Decimal by the module that owns it."""

from decimal import ROUND_HALF_UP, Decimal


def group_indian(whole: int) -> str:
    """1234567 -> "12,34,567": the last three digits, then pairs."""
    digits = str(abs(whole))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        pairs = []
        while len(head) > 2:
            pairs.insert(0, head[-2:])
            head = head[:-2]
        if head:
            pairs.insert(0, head)
        digits = ",".join(pairs + [tail])
    return ("-" if whole < 0 else "") + digits


def inr(value, decimals: int = 2) -> str:
    """Decimal/number -> "1,64,944.00" (no currency sign)."""
    amount = Decimal(value or 0)
    quant = Decimal(1).scaleb(-decimals) if decimals else Decimal(1)
    amount = amount.quantize(quant, rounding=ROUND_HALF_UP)
    sign = "-" if amount < 0 else ""
    whole, _, frac = f"{abs(amount):f}".partition(".")
    text = group_indian(int(whole))
    return sign + text + (("." + frac) if decimals else "")


def inr_compact(value) -> str:
    """1640000 -> "16.4L", 45000 -> "45K", 21000000 -> "2.1Cr", 950 -> "950"."""
    amount = Decimal(value or 0)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    for size, suffix in ((Decimal(10_000_000), "Cr"), (Decimal(100_000), "L"), (Decimal(1_000), "K")):
        if amount >= size:
            short = (amount / size).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            text = f"{short:f}".rstrip("0").rstrip(".")
            return f"{sign}{text}{suffix}"
    return sign + f"{amount.quantize(Decimal(1), rounding=ROUND_HALF_UP):f}"
