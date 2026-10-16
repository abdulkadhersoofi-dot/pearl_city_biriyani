"""Weighing-scale barcode labels (module "weighing").

A label printed by a shop scale is an EAN-13 in the in-store range:
  PP  CCCCC  VVVVV  K
  |   |      |      check digit (EAN-13)
  |   |      value: the weight in grams (3 decimals of a kg), or the
  |   |             price in paise
  |   item code (the item's PLU code on the scale) - 4 to 6 digits; the
  |             value takes the rest of the 10
  prefix - which kind of value (the client's scale settings, e.g.
           21 = weight, 22 = price)

Parsed here once; the counter asks the server what a scanned label means."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class ScaleLabel:
    plu: str
    kind: str  # weight / price
    value: Decimal  # kg for weight, rupees for price


def ean13_check_digit(first12: str) -> int:
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(first12))
    return (10 - total % 10) % 10


def parse(code: str, prefixes: dict[str, str], code_digits: int = 5) -> ScaleLabel | None:
    """The label's item code and value, or None if this isn't a scale label
    under these settings (wrong length, prefix or check digit)."""
    code = (code or "").strip()
    if len(code) != 13 or not code.isdigit():
        return None
    kind = prefixes.get(code[:2])
    if kind not in ("weight", "price"):
        return None
    if ean13_check_digit(code[:12]) != int(code[12]):
        return None
    code_digits = max(4, min(6, int(code_digits)))
    plu = code[2:2 + code_digits].lstrip("0") or "0"
    raw = int(code[2 + code_digits:12])
    if raw == 0:
        return None
    value = (Decimal(raw) / 1000) if kind == "weight" else (Decimal(raw) / 100)
    return ScaleLabel(plu=plu, kind=kind, value=value)
