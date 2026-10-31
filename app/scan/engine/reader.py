"""The reading engine, ported from the ARFA Invoice Parser (app_parser.py)
with its proven strategy kept intact:

  1. e-invoice QR - the government-signed JWT on an e-invoice gives the
     GSTINs, number, date and total as ground truth. Read IN PARALLEL with
     the AI pass (it used to run after it).
  2. Tier 1 - a cheap, low-resolution read by the fast Gemini models.
  3. Tier 2 - only when a GSTIN fails its checksum (a proven misread): the
     same pages at high resolution through the strong model.
  4. Deterministic validation and repair (validators.py) - totals, tax
     arithmetic, GSTIN checksums - and one targeted re-read when the hard
     checks fail.
  5. Ownership - which of the client's own GSTINs is on the bill, snapping
     near-misses to it and to the client's known suppliers; a bill that
     provably belongs to someone else, or can't be read at all, is rejected.

Differences from the stand-alone parser: no S3 / DynamoDB / auth (the
module stores in ARFA's database and uses ARFA's sign-in), several own
GSTINs per client instead of one, ARFA's suppliers as the counterparty
memory, the API key sent as a header (never in a URL that could be
logged), and one time budget sized for a Cloud Run request.

Everything here is pure: bytes and settings in, a result dict out. The
caller (app/scan/services.py) owns the database."""

from __future__ import annotations

import base64
import gc
import io
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import requests

from app.scan.engine.validators import clean_gstin, gstin_checksum_ok, validate_and_repair
from app.scan.engine.validators import _n as _to_num

log = logging.getLogger("arfa.scan")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Pixel-heavy work (rasterising, compressing, QR) is serialised per
# process so a burst of uploads can't multiply peak memory; the network
# calls to the model stay outside the lock and overlap freely.
_RASTER = threading.BoundedSemaphore(2)
_QR_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="scan-qr")


@dataclass
class Settings:
    api_key: str
    models: list[str] = field(default_factory=lambda: ["gemini-2.5-flash-lite", "gemini-2.5-flash"])
    strong_model: str = "gemini-2.5-pro"
    call_timeout: int = 25          # one model call
    budget: float = 48.0            # the whole read - under Cloud Run's 60 s request limit
    cheap_px: int = 1600
    hires_px: int = 3072
    max_invoice_pages: int = 4
    hard_page_cap: int = 25
    qr_budget: float = 3.0


@dataclass
class Context:
    """Who is reading: the client's own GSTINs (the first is the primary),
    its name, and GSTINs it has confirmed before (its suppliers)."""
    own_gstins: list[str]
    company_name: str = ""
    known_gstins: list[str] = field(default_factory=list)


class ReadError(Exception):
    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.message = message
        self.code = code


# ╔═══════════════════════════════════════════════════════════════════╗
# ║  E-INVOICE QR - signed ground truth for GSTINs / number / total   ║
# ╚═══════════════════════════════════════════════════════════════════╝

_PYZBAR_OK = None


def _pyzbar_decode(pil_img):
    """QR codes only: zbar's DataBar decoder can abort() the whole process
    on some patterns, and e-invoice QRs are always QRCODE symbology."""
    global _PYZBAR_OK
    if _PYZBAR_OK is False:
        return []
    try:
        from pyzbar.pyzbar import ZBarSymbol
        from pyzbar.pyzbar import decode as _zbar_decode
        _PYZBAR_OK = True
    except Exception as exc:  # noqa: BLE001 - no libzbar: OCR only
        if _PYZBAR_OK is None:
            log.warning("pyzbar unavailable (%s) - QR check off, reading only", exc)
        _PYZBAR_OK = False
        return []
    try:
        return [r.data for r in _zbar_decode(pil_img, symbols=[ZBarSymbol.QRCODE]) if r.data]
    except Exception as exc:  # noqa: BLE001
        log.info("QR decode error: %s", exc)
        return []


def _qr_bytes_to_str(data):
    try:
        return data.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None


def _extract_qr_raw(img_bytes, budget_s: float = 3.0):
    """Straight decode, then enhanced variants, then the usual e-invoice QR
    corners - all inside one wall-clock budget so a bill with no QR costs
    at most a few seconds."""
    deadline = time.time() + budget_s
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(img_bytes))).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    for data in _pyzbar_decode(img):
        s = _qr_bytes_to_str(data)
        if s:
            return s
    g = ImageOps.grayscale(img)
    for scale in (1, 2, 3):
        if time.time() > deadline:
            return None
        base = g if scale == 1 else g.resize((g.width * scale, g.height * scale), Image.LANCZOS)
        for variant in (base, ImageEnhance.Contrast(base).enhance(2.2), base.filter(ImageFilter.SHARPEN)):
            for data in _pyzbar_decode(variant):
                s = _qr_bytes_to_str(data)
                if s:
                    return s
    try:
        w, h = img.size
        regions = [
            (int(w * 0.55), 0, w, int(h * 0.35)), (0, 0, int(w * 0.45), int(h * 0.35)),
            (int(w * 0.50), 0, w, int(h * 0.50)), (0, 0, w, int(h * 0.25)),
            (int(w * 0.55), int(h * 0.6), w, h), (0, int(h * 0.6), int(w * 0.45), h),
        ]
        for (x0, y0, x1, y1) in regions:
            if time.time() > deadline:
                break
            if x1 <= x0 or y1 <= y0:
                continue
            crop = img.crop((x0, y0, x1, y1))
            for scale in (2, 3, 5):
                c = crop.resize((max(1, crop.width * scale), max(1, crop.height * scale)), Image.LANCZOS)
                for variant in (ImageOps.grayscale(c), ImageEnhance.Contrast(ImageOps.grayscale(c)).enhance(2.2),
                                c.filter(ImageFilter.SHARPEN)):
                    for data in _pyzbar_decode(variant):
                        s = _qr_bytes_to_str(data)
                        if s:
                            return s
    except Exception as exc:  # noqa: BLE001
        log.info("QR region scan error: %s", exc)
    return None


def parse_einvoice_jwt(raw):
    """An e-invoice QR payload (a JWT signed by the IRP) -> its invoice
    fields, or None when it isn't one."""
    if not raw or raw.count(".") < 2:
        return None
    try:
        payload_b64 = raw.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:  # noqa: BLE001
        return None
    data = payload.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(data, dict) or not data.get("SellerGstin"):
        return None
    inv_dt = (data.get("DocDt") or "").strip()
    iso_dt = inv_dt
    try:
        if "/" in inv_dt:
            dd, mm, yyyy = inv_dt.split("/")
            iso_dt = f"{yyyy}-{mm.zfill(2)}-{dd.zfill(2)}"
    except ValueError:
        iso_dt = inv_dt
    return {
        "supplier_gstin": (data.get("SellerGstin") or "").strip().upper(),
        "receiver_gstin": (data.get("BuyerGstin") or "").strip().upper(),
        "invoice_number": (data.get("DocNo") or "").strip(),
        "invoice_date": iso_dt,
        "invoice_value": _to_num(data.get("TotInvVal")),
        "item_count": data.get("ItemCnt"),
        "main_hsn": (data.get("MainHsnCode") or "").strip(),
        "irn": (data.get("Irn") or "").strip(),
        "doc_type": (data.get("DocTyp") or "").strip(),
    }


def decode_einvoice_qr(candidates: list[bytes], budget_s: float = 3.0):
    for img_bytes in candidates:
        with _RASTER:
            raw = _extract_qr_raw(img_bytes, budget_s)
        parsed = parse_einvoice_jwt(raw) if raw else None
        if parsed:
            return parsed
    return None


# ╔═══════════════════════════════════════════════════════════════════╗
# ║  The model                                                         ║
# ╚═══════════════════════════════════════════════════════════════════╝

PROMPT = """Indian GST invoice (all pages given). Return ONLY JSON.
SUPPLIER vs RECEIVER — read carefully, do not swap:
- SUPPLIER = the seller who ISSUED the invoice: the company in the TOP letterhead/logo, and the same name that appears after "for" near the signature/seal at the bottom. Its GSTIN is the one printed in the header block.
- RECEIVER = the BUYER: the party under "Bill To" / "Buyer" / "Billed to" / "Consignee (Ship to)".
Match each GSTIN to the party it is printed NEXT TO. The header GSTIN is the supplier_gstin; the Bill-To GSTIN is the receiver_gstin. NEVER put the header GSTIN in receiver or the Bill-To GSTIN in supplier. Ignore any separate "Ship To" address. If only one GSTIN is printed, it is the supplier and receiver_gstin is "".
GSTIN=exactly 15 chars, copy verbatim char-by-char. Common scan confusions to read CAREFULLY: I vs 1, O vs 0, K vs A, J vs U vs V, S vs 5, B vs 8, Z vs 2, G vs 6. The GSTIN is ALWAYS present on a tax invoice (labelled 'GSTIN/UIN') even if a watermark/logo overlaps it — look under the watermark and transcribe every character; NEVER leave GSTIN blank if any 15-char code is visible. Positions 3-7 are letters (PAN), 8-11 digits, 12 letter, 13 digit, 14='Z', 15 alnum.
invoice_number: transcribe the 'Invoice No.' field character-for-character EXACTLY as printed — every slash '/', dash '-', letter and digit. Do NOT normalise, simplify, merge adjacent strokes (e.g. two strokes 'VV' are 'VV', not 'W'), or expand ligatures. If two characters touch or a separator is faint, prefer what is literally drawn. It is ALWAYS printed near the top — never leave it blank.
taxable_value=TAXABLE column (not unit price); invoice_value=grand total.
One line_item per SKU; skip serial/IMEI lists. For EACH line item ALWAYS fill gst_rate (the % rate for that item, e.g. 18) AND taxable_value (that line's taxable amount before tax). If the invoice prints tax per line, fill the line's cgst/sgst/igst too; if tax is shown only as one consolidated total at the bottom, still fill each line's gst_rate and taxable_value and leave the per-line tax 0 — the rate and taxable are enough.
Pages may be STACKED vertically in one image, separated by dark 'PAGE x of y' bars. Totals (CGST/SGST/IGST, grand total, HSN summary) usually sit at the BOTTOM of the LAST page — read the ENTIRE image to the bottom before answering.
category: "Expenses" for a bill for a service or a consumable the business uses itself (restaurant / hotel / fuel / travel / telephone / internet / electricity / rent / repairs / courier / stationery / professional fees); "Purchase" for goods or services bought from a supplier on a tax invoice addressed to the business; "Sales" only when the business itself issued the invoice; a credit or debit note is "... - Credit Note" / "... - Debit Note"; a payment receipt is "Receipts".
{"supplier_name":"","supplier_gstin":"","supplier_address":"","supplier_phone":"","supplier_email":"","supplier_pan":"","supplier_state":"","receiver_name":"","receiver_gstin":"","receiver_address":"","receiver_state":"","invoice_number":"","invoice_date":"YYYY-MM-DD","due_date":"","place_of_supply":"","reverse_charge":"","irn":"","ack_number":"","ack_date":"","ewb_number":"","bank_name":"","bank_account":"","bank_ifsc":"","bank_branch":"","hsn_sac":"","notes":"","category":"Purchase or Sales or Purchase - Credit Note or Purchase - Debit Note or Sales - Credit Note or Sales - Debit Note or Expenses or Receipts","line_items":[{"description":"","hsn_sac":"","quantity":0,"unit":"","rate_per_unit":0,"gst_rate":0,"taxable_value":0,"igst":0,"cgst":0,"sgst":0,"cess":0,"amount":0}],"taxable_value":0,"igst":0,"cgst":0,"sgst":0,"cess":0,"tds":0,"discount":0,"round_off":0,"invoice_value":0,"confidence":0.9,"flags":[]}
Amounts=numbers; dates=YYYY-MM-DD; root totals=Σ line_items.
SELF-CHECK (re-read any digit that fails):
1. taxable+igst+cgst+sgst+cess-tds-discount+round_off == invoice_value.
2. CGST==SGST. Same state code→CGST+SGST only; different→IGST only; never both.
3. Per line: taxes == taxable×gst_rate/100.
4. Printed totals are authoritative — if your sum disagrees, re-read digits.
5. For ANY field where a character is ambiguous, touching, watermark-obscured, or you are not fully certain of even ONE character (numbers, GSTIN, invoice number, dates — anything), add that field's name to "flags" and lower "confidence". It is better to flag a correct read than to silently pass a wrong one — flags route the field to human review."""

TEXT_FIELDS = ("supplier_name", "supplier_gstin", "supplier_address", "supplier_phone", "supplier_email",
               "supplier_pan", "supplier_state", "receiver_name", "receiver_gstin", "receiver_address",
               "receiver_state", "invoice_number", "invoice_date", "due_date", "irn", "ack_number", "ack_date",
               "ewb_number", "bank_name", "bank_account", "bank_ifsc", "bank_branch", "hsn_sac",
               "place_of_supply", "reverse_charge", "notes")
MONEY_FIELDS = ("igst", "cgst", "sgst", "cess", "tds", "discount", "round_off")


def _clean_json(text):
    text = (text or "").strip()
    if "```" in text:
        for part in text.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                return part
    s, e = text.find("{"), text.rfind("}") + 1
    return text[s:e] if s >= 0 and e > s else text


def _f(val):
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "").replace("₹", "").replace("Rs", "").strip())
    except (TypeError, ValueError):
        return None


def call_model(parts: list, settings: Settings, models: list[str], deadline: float, max_tokens: int = 8192,
               http=requests) -> tuple[str | None, str | None]:
    """One prompt through the model list, falling back on 404 / 429 / 503 /
    errors, never past the deadline. Returns (text, model) or (None, None)."""
    for model in models:
        left = deadline - time.time()
        if left < 4:
            break
        body = {"contents": [{"parts": parts}],
                "generationConfig": {"temperature": 0.0, "topK": 1, "topP": 1.0, "seed": 42,
                                     "maxOutputTokens": max_tokens, "responseMimeType": "application/json",
                                     "thinkingConfig": {"thinkingBudget": 0}}}
        try:
            r = http.post(GEMINI_URL.format(model=model), json=body,
                          headers={"x-goog-api-key": settings.api_key},
                          timeout=(5, min(settings.call_timeout, max(4, int(left)))))
            if r.status_code in (404, 429, 500, 503):
                log.info("model %s answered %s - trying the next", model, r.status_code)
                continue
            r.raise_for_status()
            cands = r.json().get("candidates") or []
            text = ((cands[0].get("content") or {}).get("parts") or [{}])[0].get("text") if cands else None
            if text:
                return text, model
        except Exception as exc:  # noqa: BLE001 - next model
            log.info("model %s failed: %s", model, exc)
    return None, None


def _image_part(jpeg: bytes) -> dict:
    return {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}}


def _stitch_pages(jpeg_list):
    """2-4 pages stacked into one tall image with separator bars: the model
    keeps the totals (last page) in view with the line items."""
    from PIL import Image, ImageDraw
    imgs = [Image.open(io.BytesIO(b)).convert("RGB") for b in jpeg_list]
    w = max(im.width for im in imgs)
    gap, bar = 14, 26
    canvas = Image.new("RGB", (w, sum(im.height for im in imgs) + (gap + bar) * len(imgs)), (255, 255, 255))
    d = ImageDraw.Draw(canvas)
    y = 0
    for i, im in enumerate(imgs):
        d.rectangle([0, y, w, y + bar], fill=(16, 64, 78))
        d.text((8, y + 6), f"--- PAGE {i + 1} of {len(imgs)} ---", fill=(255, 255, 255))
        y += bar
        canvas.paste(im, ((w - im.width) // 2, y))
        y += im.height + gap
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()


def _shape(parsed: dict) -> dict:
    """The model's JSON -> the record every later step works on."""
    out = {k: (parsed.get(k) or None) for k in TEXT_FIELDS}
    out["invoice_value"] = _f(parsed.get("invoice_value"))
    out["taxable_value"] = _f(parsed.get("taxable_value"))
    for k in MONEY_FIELDS:
        out[k] = _f(parsed.get(k)) or 0
    items = []
    for item in parsed.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        fi = lambda k: _f(item.get(k)) or 0  # noqa: E731
        items.append({
            "description": str(item.get("description") or "")[:255], "hsn_sac": str(item.get("hsn_sac") or ""),
            "quantity": fi("quantity"), "unit": str(item.get("unit") or ""), "rate_per_unit": fi("rate_per_unit"),
            "gst_rate": fi("gst_rate"), "taxable_value": fi("taxable_value"), "igst": fi("igst"),
            "cgst": fi("cgst"), "sgst": fi("sgst"), "cess": fi("cess"), "amount": fi("amount"),
        })
    if not items and out["taxable_value"]:
        items.append({"description": "Total", "hsn_sac": out["hsn_sac"] or "", "quantity": 0, "unit": "",
                      "rate_per_unit": 0, "gst_rate": 0, "taxable_value": out["taxable_value"],
                      "igst": out["igst"], "cgst": out["cgst"], "sgst": out["sgst"], "cess": out["cess"],
                      "amount": out["invoice_value"] or 0})
    out["line_items"] = items
    out["category"] = parsed.get("category") or "Purchase"
    try:
        out["confidence"] = float(parsed.get("confidence", 0.8))
    except (TypeError, ValueError):
        out["confidence"] = 0.5
    flags = parsed.get("flags") or []
    out["flags"] = [str(f) for f in flags] if isinstance(flags, list) else []
    return out


def _empty_read() -> dict:
    out = {k: None for k in TEXT_FIELDS}
    out.update({"invoice_value": None, "taxable_value": None, "line_items": [], "category": "Purchase",
                "confidence": 0.0, "flags": ["all_fields"]})
    out.update({k: 0 for k in MONEY_FIELDS})
    return out


def extract_fields(pages: list[bytes], settings: Settings, deadline: float, hint: str = "",
                   models: list[str] | None = None, http=requests) -> tuple[dict, str | None]:
    parts = []
    if 2 <= len(pages) <= 4:
        try:
            parts.append(_image_part(_stitch_pages(pages)))
        except Exception:  # noqa: BLE001 - separate pages instead
            parts = []
    if not parts:
        for i, page in enumerate(pages):
            if len(pages) > 1:
                parts.append({"text": f"PAGE {i + 1} of {len(pages)}:"})
            parts.append(_image_part(page))
    parts.append({"text": PROMPT + ("\nIMPORTANT CORRECTION PASS: " + hint if hint else "")})
    text, model = call_model(parts, settings, models or settings.models, deadline, http=http)
    if not text:
        return _empty_read(), None
    try:
        return _shape(json.loads(_clean_json(text))), model
    except (ValueError, TypeError, AttributeError):
        return _empty_read(), model


# ╔═══════════════════════════════════════════════════════════════════╗
# ║  Pages                                                             ║
# ╚═══════════════════════════════════════════════════════════════════╝

def _is_blank_page(img) -> bool:
    """Provably blank only (<0.05% ink on a near-white page)."""
    try:
        from PIL import ImageStat
        g = img.convert("L")
        if ImageStat.Stat(g).mean[0] <= 244:
            return False
        return sum(g.histogram()[:200]) / (g.width * g.height) < 0.0005
    except Exception:  # noqa: BLE001
        return False


def compress(img_bytes: bytes, px: int, q: int = 85, max_kb: int = 1200) -> bytes:
    """An image for the model: upright (EXIF), at most px on the long side,
    JPEG under max_kb."""
    try:
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(img_bytes))).convert("RGB")
        if max(img.width, img.height) > px:
            scale = px / max(img.width, img.height)
            img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q, optimize=True)
        data = buf.getvalue()
        floor = max(70, q - 12)
        while len(data) > max_kb * 1024 and q > floor:
            q -= 4
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=q, optimize=True)
            data = buf.getvalue()
        return data
    except Exception as exc:  # noqa: BLE001 - send what we have
        log.info("compress failed: %s", exc)
        return img_bytes


_GST_PATTERN = re.compile(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]")
_AMT_PATTERN = re.compile(r"[0-9]{1,3}(?:,[0-9]{3})+(?:[.][0-9]{2})?|[0-9]+\.[0-9]{2}\b")
_KEYWORDS = ("HSN", "TOTAL", "TAXABLE", "INVOICE", "TAX", "QTY", "QUANTITY", "RATE", "AMOUNT", "DESCRIPTION",
             "PARTICULARS", "ITEM", "S.NO", "SL.NO", "SR.NO", "UNIT", "UOM", "GROSS", "NET", "DISCOUNT")


def pdf_page_count(pdf_bytes: bytes) -> int:
    import fitz
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return len(doc)


def pdf_to_pages(pdf_bytes: bytes, settings: Settings, max_px: int = 2000) -> tuple[list[bytes], list[bytes]]:
    """Every page as a display JPEG, plus the pages worth sending to the
    model (first, last, any with invoice content - never a page that only
    might be blank) at the cheap resolution."""
    import fitz
    from PIL import Image
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        n = len(doc)
        if n > settings.hard_page_cap:
            raise ReadError(f"This PDF has {n} pages - split it into smaller files first "
                            f"(at most {settings.hard_page_cap}).", "too_many_pages")
        full, model_pages = [], []
        for i, pg in enumerate(doc):
            scale = max_px / max(pg.rect.width, pg.rect.height)
            pix = pg.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            pix = None
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=88, optimize=True)
            full.append(buf.getvalue())
            text = pg.get_text("text") if 0 < i < n - 1 else ""
            if i in (0, n - 1):
                keep = True
            elif len(text.strip()) > 20:
                upper = text.upper()
                keep = bool(_GST_PATTERN.search(text) or _AMT_PATTERN.search(text) or "CONTINUED" in upper
                            or any(k in upper for k in _KEYWORDS))
            else:
                keep = not _is_blank_page(img)
            if keep:
                gw = int(img.width * settings.cheap_px / max(img.width, img.height))
                gh = int(img.height * settings.cheap_px / max(img.width, img.height))
                small = img.resize((gw, gh), Image.LANCZOS)
                q, out = 85, io.BytesIO()
                small.save(out, format="JPEG", quality=q, optimize=True)
                data = out.getvalue()
                while len(data) > 900 * 1024 and q > 55:
                    q -= 10
                    out = io.BytesIO()
                    small.save(out, format="JPEG", quality=q, optimize=True)
                    data = out.getvalue()
                model_pages.append(data)
                small.close()
            img.close()
        return full, model_pages
    finally:
        doc.close()
        gc.collect()


def preview_jpeg(img_bytes: bytes, width: int = 1400) -> bytes:
    """The picture shown on the review screen and kept with the bill."""
    return compress(img_bytes, px=width if width else 1400, q=85, max_kb=900)


# ╔═══════════════════════════════════════════════════════════════════╗
# ║  Bundles - one PDF holding several invoices                        ║
# ╚═══════════════════════════════════════════════════════════════════╝

_INV_NO_RE = re.compile(r"invoice\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9/\-]{2,20})", re.I)
_PAGE2_RE = re.compile(r"(?:page\s*2|continued|page\s*\d+\s*of\s*\d+)", re.I)
_SUMMARY_RE = re.compile(r"voucher\s*register|grand\s*total|day\s*book|summary\s*of|list\s*of\s*vouchers", re.I)
_TAXINV_RE = re.compile(r"tax\s*invoice|bill\s*of\s*supply", re.I)


def norm_invno(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def group_pages(pages: list[dict]) -> list[dict]:
    """pages: [{i, inv, summary, cont, force_start}] -> [{invoice_number,
    page_indices}], summary pages left out."""
    groups, cur, last_inv = [], None, None
    for p in pages:
        if p.get("summary"):
            cur = None
            last_inv = None
            continue
        inv = p.get("inv") or ""
        is_start = True if p.get("force_start") else (not p.get("cont")) and (inv != last_inv or cur is None)
        if not is_start and cur is not None and (p.get("cont") or (inv and inv == last_inv)):
            cur["page_indices"].append(p["i"])
            if inv:
                cur["invoice_number"] = cur["invoice_number"] or inv
        else:
            cur = {"invoice_number": inv, "page_indices": [p["i"]]}
            groups.append(cur)
        if inv:
            last_inv = inv
    return groups


def _segment_by_text(doc):
    if sum(len(p.get_text("text").strip()) for p in doc) < 40 * len(doc):
        return None
    pages = []
    for i, pg in enumerate(doc):
        t = pg.get_text("text")
        m = _INV_NO_RE.search(t)
        pages.append({"i": i, "inv": norm_invno(m.group(1)) if m else "",
                      "summary": bool(_SUMMARY_RE.search(t)) and not _TAXINV_RE.search(t),
                      "cont": bool(_PAGE2_RE.search(t))})
    return group_pages(pages)


_SEGMENT_PROMPT = (
    "This file is a BUNDLE of multiple separate tax invoices that were scanned together. Most pages are the FIRST "
    "page of a DIFFERENT invoice. Examine EACH page independently and read its 'Invoice No.' field. RULES: (1) A "
    "page headed 'Tax Invoice' with an Invoice No. that DIFFERS from the previous page is a NEW invoice → role "
    "'invoice_start'. (2) A page headed 'Tax Invoice(Page 2)' or showing only tax totals with the SAME Invoice No. "
    "as the page before it continues it → 'invoice_continuation'. (3) A 'Voucher Register' / 'Day Book' / 'Grand "
    "Total' listing of many vouchers is → 'summary'. Do NOT merge invoices with different numbers. Most bundles "
    "have MANY invoices, not one. Reply ONLY with JSON: "
    '{"pages":[{"page":1,"role":"summary","invoice_number":""},'
    '{"page":2,"role":"invoice_start","invoice_number":"11/JKE/26-27"}]}. Copy invoice_number verbatim including '
    "slashes.")


def split_groups(pdf_bytes: bytes, settings: Settings, http=requests) -> tuple[list[dict], str]:
    """How a PDF divides into invoices: from its text layer when it has one,
    else one cheap model pass over page thumbnails. Never worse than "one
    invoice" when unsure."""
    import fitz
    from PIL import Image
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        n = len(doc)
        if n == 1:
            return [{"invoice_number": "", "page_indices": [0]}], "single"
        groups = _segment_by_text(doc)
        if groups:
            return groups, "text"
        parts = []
        for i, pg in enumerate(doc):
            pix = pg.get_pixmap(matrix=fitz.Matrix(0.85, 0.85), alpha=False)
            im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            im.thumbnail((1000, 1000))
            b = io.BytesIO()
            im.save(b, "JPEG", quality=80)
            parts += [{"text": f"PAGE {i + 1}:"}, _image_part(b.getvalue())]
        parts.append({"text": _SEGMENT_PROMPT})
        text, _ = call_model(parts, settings, settings.models, time.time() + 25, max_tokens=2048, http=http)
        if text:
            try:
                pages = []
                for v in json.loads(_clean_json(text)).get("pages", []):
                    idx = int(v.get("page", 0)) - 1
                    if 0 <= idx < n:
                        role = (v.get("role") or "").lower()
                        pages.append({"i": idx, "inv": norm_invno(v.get("invoice_number", "")),
                                      "summary": role == "summary", "cont": role == "invoice_continuation",
                                      "force_start": role == "invoice_start"})
                pages.sort(key=lambda x: x["i"])
                groups = group_pages(pages)
                if groups:
                    return groups, "vision"
            except (ValueError, TypeError, AttributeError):
                pass
        return [{"invoice_number": "", "page_indices": list(range(n))}], "fallback"
    finally:
        doc.close()


def split_pdf(pdf_bytes: bytes, page_indices: list[int]) -> bytes:
    import fitz
    with fitz.open(stream=pdf_bytes, filetype="pdf") as src, fitz.open() as out:
        for idx in page_indices:
            out.insert_pdf(src, from_page=idx, to_page=idx)
        return out.tobytes()


# ╔═══════════════════════════════════════════════════════════════════╗
# ║  Who the bill belongs to                                           ║
# ╚═══════════════════════════════════════════════════════════════════╝

def name_matches(a: str, b: str) -> bool:
    def toks(s):
        s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
        stop = {"private", "pvt", "limited", "ltd", "llp", "company", "co", "and", "the", "enterprises",
                "enterprise", "industries", "traders", "trading", "corporation", "inc"}
        return {w for w in s.split() if w and w not in stop and len(w) > 2}
    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return False
    inter = ta & tb
    small = min(len(ta), len(tb))
    return len(inter) >= max(1, small // 2 + (1 if small == 1 else 0)) or len(inter) >= 2


def gstin_near(a: str, b: str, max_diff: int = 3) -> bool:
    a, b = clean_gstin(a), clean_gstin(b)
    if len(a) != 15 or len(b) != 15:
        return False
    return sum(1 for x, y in zip(a, b) if x != y) <= max_diff


def _pan(g):
    return g[2:12] if len(g) >= 12 else g


def pick_own_gstin(extracted: dict, own_gstins: list[str]) -> str:
    """Which of the client's registrations the bill is about: an exact
    match on either side, then a near-miss, then the same PAN; else the
    primary one."""
    own = [clean_gstin(g) for g in own_gstins if g]
    sup = clean_gstin(extracted.get("supplier_gstin") or "")
    rec = clean_gstin(extracted.get("receiver_gstin") or "")
    for test in (lambda g: g in (sup, rec), lambda g: gstin_near(g, sup) or gstin_near(g, rec),
                 lambda g: len(g) >= 12 and (_pan(g) == _pan(sup) or _pan(g) == _pan(rec))):
        for g in own:
            if test(g):
                return g
    return own[0] if own else ""


def classify_category(extracted: dict, company_gstin: str, company_name: str = "") -> str:
    """Direction from the GSTINs first (exact, then PAN, then elimination),
    then the company name, then the model's own word, then Purchase."""
    def ng(g):
        return re.sub(r"[^A-Z0-9]", "", str(g or "").upper().strip())
    own, sup_g, rec_g = ng(company_gstin), ng(extracted.get("supplier_gstin")), ng(extracted.get("receiver_gstin"))
    gem = (extracted.get("category") or "Purchase").strip()
    gem_l = gem.lower()
    is_cn, is_dn = "credit note" in gem_l, "debit note" in gem_l

    def result(direction):
        if is_cn:
            return f"{direction} - Credit Note"
        if is_dn:
            return f"{direction} - Debit Note"
        if direction == "Purchase" and gem_l == "expenses":
            return "Expenses"   # bought for the business's own use
        return direction

    if own and len(own) >= 10:
        if own == sup_g:
            return result("Sales")
        if own == rec_g:
            return result("Purchase")
    if own and len(own) >= 12:
        if sup_g and len(sup_g) >= 10 and _pan(own) == _pan(sup_g):
            return result("Sales")
        if rec_g and len(rec_g) >= 10 and _pan(own) == _pan(rec_g):
            return result("Purchase")
        if not is_cn and not is_dn:
            sup_other = sup_g and len(sup_g) >= 12 and _pan(sup_g) != _pan(own)
            rec_other = rec_g and len(rec_g) >= 12 and _pan(rec_g) != _pan(own)
            if sup_other and not rec_other:
                return result("Purchase")
            if rec_other and not sup_other:
                return result("Sales")
    if company_name and not is_cn and not is_dn:
        sup_us = name_matches(extracted.get("supplier_name") or "", company_name)
        rec_us = name_matches(extracted.get("receiver_name") or "", company_name)
        if sup_us and not rec_us:
            return result("Sales")
        if rec_us and not sup_us:
            return result("Purchase")
    valid = {"purchase", "sales", "purchase - credit note", "purchase - debit note", "sales - credit note",
             "sales - debit note", "expenses", "receipts"}
    if gem_l in valid:
        return gem
    if is_cn or is_dn:
        sfx = "Credit Note" if is_cn else "Debit Note"
        return f"Sales - {sfx}" if (sup_g and not rec_g) else f"Purchase - {sfx}"
    return "Purchase"


# ╔═══════════════════════════════════════════════════════════════════╗
# ║  The whole read                                                    ║
# ╚═══════════════════════════════════════════════════════════════════╝

def _gstin_suspect(ex: dict) -> bool:
    s = clean_gstin(ex.get("supplier_gstin") or "")
    r = clean_gstin(ex.get("receiver_gstin") or "")
    return (len(s) == 15 and not gstin_checksum_ok(s)) or (len(r) == 15 and not gstin_checksum_ok(r))


def _apply_qr(extracted: dict, qr: dict) -> None:
    """GSTINs from the signed QR replace a different read (flagged); the
    number, date and total are only flagged when they differ."""
    replaced, mismatch, notes = [], [], []
    for fld in ("supplier_gstin", "receiver_gstin"):
        qv, ov = clean_gstin(qr.get(fld)), clean_gstin(extracted.get(fld))
        if qv and ov != qv:
            extracted[fld] = qv
            replaced.append(fld)
            notes.append(f"{fld.replace('_', ' ')} corrected from the e-invoice QR (read '{ov or 'blank'}', "
                         f"QR '{qv}')")
    q_inv, o_inv = (qr.get("invoice_number") or "").strip(), (extracted.get("invoice_number") or "").strip()
    if q_inv and norm_invno(o_inv) != norm_invno(q_inv):
        if not o_inv:
            extracted["invoice_number"] = q_inv
        else:
            mismatch.append("invoice_number")
            notes.append(f"Invoice number differs from the e-invoice QR (read '{o_inv}', QR '{q_inv}').")
    q_dt, o_dt = (qr.get("invoice_date") or "").strip(), (extracted.get("invoice_date") or "").strip()
    if q_dt and o_dt and q_dt != o_dt:
        mismatch.append("invoice_date")
        notes.append(f"Invoice date differs from the e-invoice QR (read {o_dt}, QR {q_dt}).")
    elif q_dt and not o_dt:
        extracted["invoice_date"] = q_dt
    q_val, o_val = _to_num(qr.get("invoice_value")), _to_num(extracted.get("invoice_value"))
    if q_val > 0 and o_val > 0 and abs(q_val - o_val) > 1.0:
        mismatch.append("invoice_value")
        notes.append(f"Total differs from the e-invoice QR (read Rs. {o_val:,.2f}, QR Rs. {q_val:,.2f}).")
    elif q_val > 0 and o_val == 0:
        extracted["invoice_value"] = q_val
    if qr.get("irn"):
        extracted["irn"] = qr["irn"]
    extracted["einvoice"] = {"present": True, "verified": not (replaced or mismatch), "replaced": replaced,
                             "mismatch": mismatch, "notes": notes}
    flags = list(extracted.get("flags") or [])
    extracted["flags"] = flags + [f for f in replaced + mismatch if f not in flags]


def read(file_bytes: bytes, is_pdf: bool, ctx: Context, settings: Settings, http=requests) -> dict:
    """One document -> {extracted, validation, rejection_code,
    rejection_reason, preview, own_gstin, pages_total, engine}."""
    start = time.time()
    deadline = start + settings.budget
    engine = {"models": [], "tier2": False, "retry": False, "qr": "none"}

    if is_pdf:
        with _RASTER:
            pages_full, model_pages = pdf_to_pages(file_bytes, settings)
        if len(model_pages) > settings.max_invoice_pages:
            raise ReadError(f"This {len(model_pages)}-page PDF looks like several invoices together - use "
                            "\"Split into invoices\", or upload the invoices one by one.", "bundle")
        qr_candidates = pages_full
        hires_source = pages_full
        preview_src = pages_full[0]
    else:
        with _RASTER:
            model_pages = [compress(file_bytes, settings.cheap_px, 85, 1200)]
        qr_candidates = [file_bytes]
        hires_source = [file_bytes]
        preview_src = file_bytes
    pages_total = len(qr_candidates)

    # The QR is read alongside the model, not after it.
    qr_future = _QR_POOL.submit(decode_einvoice_qr, list(qr_candidates), settings.qr_budget)

    extracted, model = extract_fields(model_pages, settings, deadline, http=http)
    engine["models"].append(model)
    if model is None:
        qr_future.cancel()
        raise ReadError("The reading service didn't answer - try again in a minute.", "model_unavailable")

    if _gstin_suspect(extracted) and time.time() - start < settings.budget * 0.4:
        engine["tier2"] = True
        with _RASTER:
            hi = [compress(p, settings.hires_px, 92, 3500) for p in hires_source]
        strong, model2 = extract_fields(hi, settings, deadline, models=[settings.strong_model],
                                        hint="Read every GSTIN with extreme care, character by character, "
                                             "especially small or watermark-overlapped text.", http=http)
        engine["models"].append(model2)
        if model2 and not _gstin_suspect(strong):
            extracted = strong

    try:
        qr = qr_future.result(timeout=max(1.0, settings.qr_budget * 3))
    except Exception:  # noqa: BLE001 - the read carries on without it
        qr = None
    if qr:
        engine["qr"] = "found"
        _apply_qr(extracted, qr)

    own = pick_own_gstin(extracted, ctx.own_gstins)
    company_name = ctx.company_name or ""
    raw_sup, raw_rec = clean_gstin(extracted.get("supplier_gstin") or ""), clean_gstin(extracted.get("receiver_gstin") or "")
    raw_sup_ok, raw_rec_ok = gstin_checksum_ok(raw_sup), gstin_checksum_ok(raw_rec)

    # Our own side of the bill, garbled by the scan: put our GSTIN back when
    # there's evidence it's ours (a near-miss, or our name on that side).
    side_note = None
    if len(own) == 15 and gstin_checksum_ok(own):
        pre_cat = classify_category(extracted, own, company_name)
        if pre_cat.startswith("Sales"):
            side, raw, party = "supplier_gstin", raw_sup, extracted.get("supplier_name") or ""
        else:
            side, raw, party = "receiver_gstin", raw_rec, extracted.get("receiver_name") or ""
        if raw != own and ((raw and len(raw) >= 13 and gstin_near(raw, own))
                           or (company_name and name_matches(party, company_name))):
            extracted[side] = own
            side_note = f"{side.replace('_', ' ')} set to your GSTIN {own} (read '{raw or 'blank'}')"

    extracted["category"] = classify_category(extracted, own, company_name)
    validation = validate_and_repair(extracted, company_gstin=own)
    if side_note:
        validation.setdefault("repairs", []).append(side_note)

    if (validation.get("status") == "failed" and len(model_pages) <= 2
            and time.time() - start < settings.budget * 0.55):
        fails = "; ".join(f"{c['label']} - {c['detail']}" for c in validation.get("checks", [])
                          if not c.get("ok") and c.get("level") == "hard")
        engine["retry"] = True
        second, model3 = extract_fields(model_pages, settings, deadline, http=http,
                                        hint=f"Your previous reading failed these checks: {fails}. Re-read every "
                                             "amount and GSTIN character-by-character.")
        engine["models"].append(model3)
        if model3:
            if qr:
                _apply_qr(second, qr)
            second["category"] = classify_category(second, own, company_name)
            v2 = validate_and_repair(second, company_gstin=own)
            if v2.get("score", 0) > validation.get("score", 0):
                extracted, validation = second, v2

    extracted["category"] = classify_category(extracted, own, company_name)

    # Snap near-misses: to our own GSTIN, then to a supplier we already know.
    known = [clean_gstin(g) for g in ctx.known_gstins if g]
    for fld in ("supplier_gstin", "receiver_gstin"):
        val = clean_gstin(extracted.get(fld) or "")
        if not val:
            continue
        if own and val != own and gstin_near(own, val):
            extracted[fld] = own
            continue
        if val != own and val not in known:
            match = next((k for k in known if gstin_near(k, val)), None)
            if match:
                extracted[fld] = match
                validation.setdefault("repairs", []).append(f"{fld.replace('_', ' ')} matched to your supplier "
                                                            f"{match} (read '{val}')")

    rejection, reason = None, ""
    sup_g, rec_g = clean_gstin(extracted.get("supplier_gstin") or ""), clean_gstin(extracted.get("receiver_gstin") or "")
    inv_n = (extracted.get("invoice_number") or "").strip()
    inv_v = extracted.get("invoice_value")
    conf = float(extracted.get("confidence") or 0)
    owns = {clean_gstin(g) for g in ctx.own_gstins if g}
    if conf < 0.25 and not sup_g and not rec_g and not inv_n and inv_v in (None, 0):
        rejection = "UNREADABLE"
        reason = "This picture couldn't be read - it may be blurry, dark, cut off or not a bill. Take a clearer photo."
    elif owns and sup_g and rec_g and sup_g not in owns and rec_g not in owns and raw_sup_ok and raw_rec_ok \
            and not any(_pan(o) in (_pan(sup_g), _pan(rec_g)) for o in owns):
        rejection = "WRONG_COMPANY"
        reason = (f"This bill isn't addressed to your business: neither the seller ({sup_g}) nor the buyer "
                  f"({rec_g}) is one of your GSTINs.")

    with _RASTER:
        preview = preview_jpeg(preview_src)
    engine["seconds"] = round(time.time() - start, 1)
    return {"extracted": extracted, "validation": validation, "rejection_code": rejection,
            "rejection_reason": reason, "preview": preview, "own_gstin": own, "pages_total": pages_total,
            "engine": engine}
