"""Dynamic UPI QR codes and shareable bill links.

UPI: a standard `upi://pay` deep link (NPCI's UPI linking spec - pa =
payee VPA, pn = payee name, am = amount, cu = INR, tn = note) drawn as a
QR code server-side with segno, so any UPI app can scan it with the
amount filled in. Nothing is called on any payment network - the cashier
still confirms the money arrived.

Sharing: a signed, expiring token for one bill's PDF (itsdangerous, the
app's SECRET_KEY), so a customer can open their bill from a WhatsApp
message without signing in, and nobody can walk other bills' numbers.
`wa.me` is WhatsApp's own public click-to-chat link - no WhatsApp API.
"""

from decimal import Decimal
from io import BytesIO
from urllib.parse import quote, urlencode

import segno
from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_SHARE_SALT = "pos-bill-share-v1"
DEFAULT_SHARE_DAYS = 7


def upi_uri(vpa: str, payee_name: str | None, amount: Decimal, note: str) -> str:
    params = {"pa": vpa.strip(), "pn": (payee_name or "").strip(), "am": f"{Decimal(amount):.2f}", "cu": "INR", "tn": note[:50]}
    return "upi://pay?" + urlencode({k: v for k, v in params.items() if v}, quote_via=quote)


def qr_svg(data: str, scale: int = 6) -> bytes:
    buf = BytesIO()
    segno.make(data, error="m").save(buf, kind="svg", scale=scale, border=2, xmldecl=False)
    return buf.getvalue()


def qr_svg_data_uri(data: str, scale: int = 4) -> str:
    """For embedding in a printed receipt (WeasyPrint renders it)."""
    return segno.make(data, error="m").svg_data_uri(scale=scale, border=2)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=_SHARE_SALT)


def share_token(bill) -> str:
    return _serializer().dumps({"t": bill.tenant_id, "b": bill.id})


def share_max_age_seconds() -> int:
    return int(current_app.config.get("BILL_SHARE_LINK_DAYS", DEFAULT_SHARE_DAYS)) * 86400


def load_share_token(token: str) -> tuple[int, int] | None:
    """(tenant_id, bill_id), or None if the link is forged or expired."""
    try:
        data = _serializer().loads(token, max_age=share_max_age_seconds())
    except (BadSignature, SignatureExpired):
        return None
    try:
        return int(data["t"]), int(data["b"])
    except (KeyError, TypeError, ValueError):
        return None


def whatsapp_link(phone: str | None, text: str) -> str:
    """wa.me click-to-chat. An Indian 10-digit number gets the 91 prefix;
    without a number WhatsApp asks who to send it to."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) == 10:
        digits = "91" + digits
    elif len(digits) == 11 and digits.startswith("0"):
        digits = "91" + digits[1:]
    base = f"https://wa.me/{digits}" if digits else "https://wa.me/"
    return f"{base}?text={quote(text)}"
