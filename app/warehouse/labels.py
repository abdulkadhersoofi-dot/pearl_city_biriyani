"""Reading a box label with ARFA - the warehouse scanner's last resort, when
neither the barcodes nor the phone's own text reading matched a serial.

The same Gemini set-up as Scan bills (app/scan/services.engine_settings:
Vertex AI through the Cloud project, or the AI Studio key), on a small
768-pixel picture, with a strict JSON answer. What comes back is a guess
like any scan: the API checks it exactly as a scanned barcode (it is never
trusted on its own). LABEL_DAILY_LIMIT reads per business per day.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, time as dtime, timezone

from flask import current_app

from app.models.audit_log import AuditLog

PROMPT = """Photo of a product's box or rating label (an AC, fridge, washing machine, TV or phone).
Return ONLY JSON: {{"model_no": "...", "serial_no": "...", "confidence": 0.0}}
- model_no: the model number exactly as printed (labels say Model, Model No, M/N).
- serial_no: the unit's serial number exactly as printed (Serial No, S/N, SR No, IMEI) - not the model,
  not a barcode of the EAN/MRP, not a date.
- Copy characters exactly; never guess a character you can't read - leave the field "" instead.
- confidence: 0 to 1, how sure you are of serial_no.
{hint}"""

DEFAULT_DAILY_LIMIT = 500


class LabelError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def reads_today(tenant_id: int) -> int:
    start = datetime.combine(date.today(), dtime.min).astimezone(timezone.utc)
    return AuditLog.query.filter(AuditLog.tenant_id == tenant_id, AuditLog.action == "warehouse_label_read",
                                 AuditLog.created_at >= start).count()


def extract(tenant_id: int, image: bytes, products: list[str] | None = None, model_nos: list[str] | None = None,
            http=None) -> dict:
    """{model_no, serial_no, confidence} read from a label photo."""
    from app.scan.engine import reader
    from app.scan.services import engine_settings

    settings = engine_settings()
    if settings is None:
        raise LabelError("ARFA's label reader isn't set up on this server - scan the barcode or type the serial.", 503)
    limit = int(current_app.config.get("LABEL_DAILY_LIMIT") or DEFAULT_DAILY_LIMIT)
    if reads_today(tenant_id) >= limit:
        raise LabelError(f"Today's {limit} label reads are used up - scan the barcode instead.", 429)
    hint = ""
    if products:
        hint += "This box should be one of: " + "; ".join(p[:80] for p in products[:5]) + "."
    if model_nos:
        hint += " Its model number is likely one of: " + ", ".join(m[:40] for m in model_nos[:5] if m) + "."
    picture = reader.compress(image, 768, q=82, max_kb=300)
    parts = [{"text": PROMPT.format(hint=hint.strip())}, reader._image_part(picture)]
    kwargs = {"http": http} if http is not None else {}
    text, model = reader.call_model(parts, settings, list(settings.models), time.time() + 25, max_tokens=256,
                                    **kwargs)
    if not text:
        raise LabelError("The label couldn't be read just now - try again, or scan the barcode.", 502)
    try:
        data = json.loads(reader._clean_json(text))
    except ValueError:
        raise LabelError("The label couldn't be read - take the photo closer and straight on.", 422)
    serial = str(data.get("serial_no") or "").strip()[:64]
    model_no = str(data.get("model_no") or "").strip()[:64]
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    return {"model_no": model_no, "serial_no": serial, "confidence": round(confidence, 2), "model": model}
