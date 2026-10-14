"""Turning raw Excel cell values into clean Python values.

openpyxl hands back whatever the sheet stored - a typed "0401" may arrive
as the number 401, 18% as 0.18, a price as a float - so every column goes
through one of these instead of being trusted as-is. Each returns the
cleaned value or raises CellError with a plain-language reason that ends
up next to that row in the import preview.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.utils import gst_config


class CellError(ValueError):
    pass


_TRUE = {"yes", "y", "true", "1"}
_FALSE = {"no", "n", "false", "0"}


def text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    value = str(value).strip()
    return value or None


def decimal(value, places: int = 2) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise CellError("expected a number")
    raw = str(value).strip().replace(",", "").replace("₹", "")
    for prefix in ("Rs.", "Rs", "INR"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):].strip()
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise CellError(f"'{value}' is not a number")
    if number < 0:
        raise CellError("can't be negative")
    return number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def yes_no(value, default: bool) -> bool:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, bool):
        return value
    raw = str(value).strip().lower()
    if isinstance(value, (int, float)):
        raw = str(int(value)) if float(value).is_integer() else raw
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise CellError(f"'{value}' - write Yes or No")


def hsn(value) -> str | None:
    """A numeric cell loses leading zeros (0401 -> 401); real HSN/SAC
    codes are always an even number of digits, so an odd-length number
    gets its zero back. Text cells are taken exactly as typed."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not value.is_integer():
            raise CellError(f"'{value}' is not an HSN/SAC code")
        code = str(int(value))
        if len(code) % 2:
            code = "0" + code
        return code
    return text(value)


def gst_rate(value) -> Decimal:
    """Accepts 18, "18", "18%" or a percent-formatted cell (0.18)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise CellError("GST rate is required (0 for nil-rated)")
    raw = str(value).strip().rstrip("%").strip()
    try:
        rate = Decimal(raw)
    except InvalidOperation:
        raise CellError(f"'{value}' is not a GST rate")
    slabs = set(gst_config.GST_RATE_SLABS)
    if rate in slabs:
        return rate
    if not str(value).strip().endswith("%") and (rate * 100) in slabs:
        return (rate * 100).normalize()
    allowed = ", ".join(f"{s.normalize():f}" for s in gst_config.GST_RATE_SLABS)
    raise CellError(f"{value} isn't a GST rate - use one of {allowed}")
