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
# Vertex AI: the same Gemini models through the Google Cloud project itself -
# no API key (the server's own service account), billed with the project.
VERTEX_URL = ("https://{host}/v1/projects/{project}/locations/{location}/publishers/google/models/"
              "{model}:generateContent")
# Checked against this project's Vertex AI on 2 Nov 2026: the 2.5 models the
# parser used are withdrawn; 3.5 Flash reads in Mumbai, 3.8 Flash only on the
# global endpoint. A setting (SCAN_MODELS / SCAN_STRONG_MODEL), not code.
DEFAULT_MODELS = ("gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash")
DEFAULT_STRONG_MODEL = "gemini-4-argon"   # generally available; read the test bill correctly
METADATA_TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"

# Pixel-heavy work (rasterising, compressing, QR) is serialised per
# process so a burst of uploads can't multiply peak memory; the network
# calls to the model stay outside the lock and overlap freely.
_RASTER = threading.BoundedSemaphore(2)
_QR_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="scan-qr")


@dataclass
class Settings:
    api_key: str = ""
    provider: str = "gemini"        # "gemini" (an AI Studio key) or "vertex" (the Cloud project)
    project: str = ""               # vertex: the Google Cloud project
    location: str = "asia-south1"   # vertex: where the model runs; "global" if a model isn't offered there
    models: list[str] = field(default_factory=lambda: list(DEFAULT_MODELS))
    strong_model: str = DEFAULT_STRONG_MODEL
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
    # From the ARFA Scan app: the text its on-device OCR read, and the
    # e-invoice QR it decoded (parse_einvoice_jwt's fields) - a second
    # pair of eyes, never taken over what the picture shows.
    phone_text: str = ""
    phone_qr: dict | None = None
    # What this business taught the reader (app/scan/lessons.py): the
    # prompt text for bills from each supplier GSTIN, and "" for every bill.
    lessons: dict[str, str] = field(default_factory=dict)
    # Headings taught (lessons.heading_rules): per supplier GSTIN and "" for
    # every bill, [(the heading as printed, the field it means)].
    headings: dict[str, list] = field(default_factory=dict)


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
One line_item per SKU. Serial / IMEI numbers printed for an item go in THAT line's serial_numbers list (every one, exactly as printed) - never as extra line_items. Also copy per line, when printed: item_code (the supplier's item / part / model / SKU code), batch, mfg_date, expiry (YYYY-MM-DD; a month-year expiry like 03/27 is the month's last day), mrp, discount_percent and discount_amount. For EACH line item ALWAYS fill gst_rate (the % rate for that item, e.g. 18) AND taxable_value (that line's taxable amount before tax). If the invoice prints tax per line, fill the line's cgst/sgst/igst too; if tax is shown only as one consolidated total at the bottom, still fill each line's gst_rate and taxable_value and leave the per-line tax 0 — the rate and taxable are enough.
Pages may be STACKED vertically in one image, separated by dark 'PAGE x of y' bars. Totals (CGST/SGST/IGST, grand total, HSN summary) usually sit at the BOTTOM of the LAST page — read the ENTIRE image to the bottom before answering.
category: "Expenses" for a bill for a service or a consumable the business uses itself (restaurant / hotel / fuel / travel / telephone / internet / electricity / rent / repairs / courier / stationery / professional fees); "Purchase" for goods or services bought from a supplier on a tax invoice addressed to the business; "Sales" only when the business itself issued the invoice; a credit or debit note is "... - Credit Note" / "... - Debit Note"; a payment receipt is "Receipts".
{"supplier_name":"","supplier_gstin":"","supplier_address":"","supplier_phone":"","supplier_email":"","supplier_pan":"","supplier_state":"","receiver_name":"","receiver_gstin":"","receiver_address":"","receiver_state":"","invoice_number":"","invoice_date":"YYYY-MM-DD","due_date":"","place_of_supply":"","reverse_charge":"","irn":"","ack_number":"","ack_date":"","ewb_number":"","bank_name":"","bank_account":"","bank_ifsc":"","bank_branch":"","bank_upi":"","po_number":"","po_date":"","order_number":"","challan_number":"","dispatch_doc_number":"","dispatched_through":"","vehicle_number":"","lr_number":"","payment_terms":"","supplier_reference":"","other_references":"","warranty":"","tds_section":"","original_invoice_number":"","original_invoice_date":"","hsn_sac":"","notes":"","category":"Purchase or Sales or Purchase - Credit Note or Purchase - Debit Note or Sales - Credit Note or Sales - Debit Note or Expenses or Receipts","line_items":[{"description":"","item_code":"","hsn_sac":"","quantity":0,"unit":"","rate_per_unit":0,"discount_percent":0,"discount_amount":0,"gst_rate":0,"taxable_value":0,"igst":0,"cgst":0,"sgst":0,"cess":0,"amount":0,"serial_numbers":[],"batch":"","mfg_date":"","expiry":"","mrp":0,"box":[0,0,0,0,0]}],"taxable_value":0,"igst":0,"cgst":0,"sgst":0,"cess":0,"tds":0,"discount":0,"round_off":0,"invoice_value":0,"confidence":0.9,"flags":[],"boxes":{},"columns":{},"rotation":[0]}
Amounts=numbers; dates=YYYY-MM-DD; root totals=Σ line_items.
References, when printed: po_number/po_date = the buyer's order no. and date; order_number = the supplier's own order / sales order no.; challan_number = delivery note / challan no.; dispatch_doc_number, dispatched_through, vehicle_number, lr_number (LR / RR / docket / AWB no.); payment_terms (e.g. "30 days"); supplier_reference = the supplier's internal reference no.; other_references = any other reference numbers, as "label: value; ...". warranty = warranty terms printed. For a credit or debit note: original_invoice_number / original_invoice_date = the invoice it adjusts ("against invoice no.", "ref. invoice", "original invoice"), and put the reason printed in notes. tds = TDS deducted on the bill (a "Less: TDS" line), tds_section its section (194Q, 194C, 194J...).
SELF-CHECK (re-read any digit that fails):
1. taxable+igst+cgst+sgst+cess-tds-discount+round_off == invoice_value.
2. CGST==SGST. Same state code→CGST+SGST only; different→IGST only; never both.
3. Per line: taxes == taxable×gst_rate/100.
4. Printed totals are authoritative — if your sum disagrees, re-read digits.
5. For ANY field where a character is ambiguous, touching, watermark-obscured, or you are not fully certain of even ONE character (numbers, GSTIN, invoice number, dates — anything), add that field's name to "flags" and lower "confidence". It is better to flag a correct read than to silently pass a wrong one — flags route the field to human review.
WHERE IT IS PRINTED (so a person can check your reading against the bill): add "boxes":{"<field>":[i,ymin,xmin,ymax,xmax],...} with a box for EVERY top-level field you filled - supplier_name, supplier_gstin, supplier_address, supplier_phone, supplier_email, receiver_name, receiver_gstin, invoice_number, invoice_date, due_date, place_of_supply, taxable_value, cgst, sgst, igst, cess, tds, discount, round_off, invoice_value, irn, ack_number, ack_date, ewb_number, po_number, po_date, order_number, challan_number, dispatch_doc_number, dispatched_through, vehicle_number, lr_number, payment_terms, supplier_reference, other_references, warranty, bank_name, bank_account, bank_ifsc, bank_branch, bank_upi - each around the printed VALUE (not its label). Give EACH line_item a "box":[i,ymin,xmin,ymax,xmax] around its whole row. And add "columns":{"<column>":[i,ymin,xmin,ymax,xmax],...} - for each column of the items table the bill has (description, item_code, hsn_sac, quantity, unit, rate_per_unit, discount, taxable_value, gst_rate, cgst, sgst, igst, amount), the box from its heading down to the last item row, its full width. i = which image the value is on (0 = the first; always 0 when there is one image); ymin,xmin,ymax,xmax = the box around the printed value, as 0-1000 of that image's height and width. Boxes never change any value.
"rotation":[...] - one number per page, in order (each image, or each PAGE panel of a stacked image): the clockwise turn in degrees, 0, 90, 180 or 270, that would make that page's printed text read upright, left to right. Usually 0; a landscape bill photographed or scanned sideways is 90 or 270; upside down is 180."""

TEXT_FIELDS = ("supplier_name", "supplier_gstin", "supplier_address", "supplier_phone", "supplier_email",
               "supplier_pan", "supplier_state", "receiver_name", "receiver_gstin", "receiver_address",
               "receiver_state", "invoice_number", "invoice_date", "due_date", "irn", "ack_number", "ack_date",
               "ewb_number", "bank_name", "bank_account", "bank_ifsc", "bank_branch", "bank_upi", "hsn_sac",
               "place_of_supply", "reverse_charge", "notes", "po_number", "po_date", "order_number",
               "challan_number", "dispatch_doc_number", "dispatched_through", "vehicle_number", "lr_number",
               "payment_terms", "supplier_reference", "other_references", "warranty", "tds_section",
               "original_invoice_number", "original_invoice_date")
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


_TOKEN = {"value": None, "expires": 0.0}
_TOKEN_LOCK = threading.Lock()
# Models a region turned out not to offer: they go to the global endpoint.
_GLOBAL_ONLY: set[str] = set()


def _vertex_token(http=requests) -> str:
    """The server's own access token from the metadata server (Cloud Run),
    reused until a minute before it expires."""
    with _TOKEN_LOCK:
        if _TOKEN["value"] and time.time() < _TOKEN["expires"] - 60:
            return _TOKEN["value"]
        r = http.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}, timeout=5)
        r.raise_for_status()
        data = r.json()
        _TOKEN["value"], _TOKEN["expires"] = data["access_token"], time.time() + float(data.get("expires_in", 300))
        return _TOKEN["value"]


def _thinking(model: str) -> dict:
    # The Pro model can't switch thinking off (a budget of 0 is refused),
    # so it gets a small one; the fast models answer straight away.
    return {"thinkingBudget": 1024} if "pro" in model else {"thinkingBudget": 0}


def _request(model: str, settings: Settings, http) -> tuple[str, dict]:
    if settings.provider == "vertex":
        location = "global" if model in _GLOBAL_ONLY else (settings.location or "global")
        host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
        url = VERTEX_URL.format(host=host, project=settings.project, location=location, model=model)
        return url, {"Authorization": f"Bearer {_vertex_token(http)}"}
    return GEMINI_URL.format(model=model), {"x-goog-api-key": settings.api_key}


def call_model(parts: list, settings: Settings, models: list[str], deadline: float, max_tokens: int = 8192,
               http=requests) -> tuple[str | None, str | None]:
    """One prompt through the model list, falling back on 404 / 429 / 503 /
    errors, never past the deadline. Returns (text, model) or (None, None).
    On Vertex AI, a model the region doesn't offer (404) is tried once more
    on the global endpoint, and kept there."""
    for model in models:
        thinking = _thinking(model)
        body = {"contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"temperature": 0.0, "topK": 1, "topP": 1.0, "seed": 42,
                                     "maxOutputTokens": max_tokens + thinking["thinkingBudget"],
                                     "responseMimeType": "application/json", "thinkingConfig": thinking}}
        for _attempt in range(3):
            left = deadline - time.time()
            if left < 4:
                return None, None
            try:
                url, headers = _request(model, settings, http)
                r = http.post(url, json=body, headers=headers, timeout=(5, min(settings.call_timeout, max(4, int(left)))))
                if (r.status_code == 404 and settings.provider == "vertex" and model not in _GLOBAL_ONLY
                        and settings.location != "global"):
                    log.warning("model %s isn't offered in %s - using the global endpoint", model, settings.location)
                    _GLOBAL_ONLY.add(model)
                    continue
                if r.status_code == 400 and "thinking" in (r.text or "").lower() \
                        and "thinkingConfig" in body["generationConfig"]:
                    # A model that takes a thinking level, not a budget (or neither).
                    body["generationConfig"].pop("thinkingConfig")
                    body["generationConfig"]["maxOutputTokens"] = max_tokens
                    continue
                if r.status_code in (404, 429, 500, 503):
                    log.info("model %s answered %s - trying the next", model, r.status_code)
                    break
                r.raise_for_status()
                cands = r.json().get("candidates") or []
                content_parts = (cands[0].get("content") or {}).get("parts") or [] if cands else []
                # A thinking model may put its thoughts first: the answer is the last text part.
                texts = [p.get("text") for p in content_parts if p.get("text") and not p.get("thought")]
                if texts:
                    return texts[-1], model
                break
            except Exception as exc:  # noqa: BLE001 - next model
                log.info("model %s failed: %s", model, exc)
                break
    return None, None


def _image_part(jpeg: bytes) -> dict:
    return {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(jpeg).decode()}}


def _stitch_pages(jpeg_list, layout: dict | None = None):
    """2-4 pages stacked into one tall image with separator bars: the model
    keeps the totals (last page) in view with the line items. `layout`,
    when given, is filled with where each page sits ({"size", "pages":
    [(left, top, width, height)]})."""
    from PIL import Image, ImageDraw
    imgs = [Image.open(io.BytesIO(b)).convert("RGB") for b in jpeg_list]
    w = max(im.width for im in imgs)
    gap, bar = 14, 26
    canvas = Image.new("RGB", (w, sum(im.height for im in imgs) + (gap + bar) * len(imgs)), (255, 255, 255))
    d = ImageDraw.Draw(canvas)
    y, placed = 0, []
    for i, im in enumerate(imgs):
        d.rectangle([0, y, w, y + bar], fill=(16, 64, 78))
        d.text((8, y + 6), f"--- PAGE {i + 1} of {len(imgs)} ---", fill=(255, 255, 255))
        y += bar
        canvas.paste(im, ((w - im.width) // 2, y))
        placed.append(((w - im.width) // 2, y, im.width, im.height))
        y += im.height + gap
    if layout is not None:
        layout.update({"size": canvas.size, "pages": placed})
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
        serials = item.get("serial_numbers") or []
        if isinstance(serials, str):
            serials = re.split(r"[,;\n]+", serials)
        ft = lambda k, n=64: str(item.get(k) or "").strip()[:n]  # noqa: E731
        items.append({
            "description": str(item.get("description") or "")[:255], "hsn_sac": str(item.get("hsn_sac") or ""),
            "quantity": fi("quantity"), "unit": str(item.get("unit") or ""), "rate_per_unit": fi("rate_per_unit"),
            "gst_rate": fi("gst_rate"), "taxable_value": fi("taxable_value"), "igst": fi("igst"),
            "cgst": fi("cgst"), "sgst": fi("sgst"), "cess": fi("cess"), "amount": fi("amount"),
            "discount_percent": fi("discount_percent"), "discount_amount": fi("discount_amount"),
            "serial_numbers": [str(x).strip()[:64] for x in serials if str(x).strip()][:500] if isinstance(serials, list) else [],
            "item_code": ft("item_code"), "batch": ft("batch"), "mfg_date": ft("mfg_date", 20),
            "expiry": ft("expiry", 20), "mrp": fi("mrp"), "box": _box(item.get("box")),
        })
    if not items and out["taxable_value"]:
        items.append({"description": "Total", "hsn_sac": out["hsn_sac"] or "", "quantity": 0, "unit": "",
                      "rate_per_unit": 0, "gst_rate": 0, "taxable_value": out["taxable_value"],
                      "igst": out["igst"], "cgst": out["cgst"], "sgst": out["sgst"], "cess": out["cess"],
                      "amount": out["invoice_value"] or 0})
    out["line_items"] = items
    out["category"] = parsed.get("category") or "Purchase"
    # The model's own word, kept apart: classify_category() decides the
    # direction exactly as the parser did (the validator's swap repair
    # depends on it), and "it's an expense" is only a hint for posting.
    out["model_category"] = out["category"]
    try:
        out["confidence"] = float(parsed.get("confidence", 0.8))
    except (TypeError, ValueError):
        out["confidence"] = 0.5
    flags = parsed.get("flags") or []
    out["flags"] = [str(f) for f in flags] if isinstance(flags, list) else []
    known = set(TEXT_FIELDS) | set(MONEY_FIELDS) | {"invoice_value", "taxable_value"}
    boxes = parsed.get("boxes") if isinstance(parsed.get("boxes"), dict) else {}
    out["boxes"] = {k: b for k, b in ((k, _box(v)) for k, v in boxes.items() if k in known) if b}
    columns = parsed.get("columns") if isinstance(parsed.get("columns"), dict) else {}
    out["columns"] = {k: b for k, b in ((k, _box(v)) for k, v in columns.items() if k in TABLE_COLUMNS) if b}
    # The labels the values were read from - asked for only when headings
    # have been taught (apply_headings uses them).
    for key, allowed in (("headings", known), ("column_headings", set(LINE_FIELDS))):
        said = parsed.get(key) if isinstance(parsed.get(key), dict) else {}
        kept = {k: str(v).strip()[:80] for k, v in said.items() if k in allowed and str(v or "").strip()}
        if kept:
            out[key] = kept
    turns = parsed.get("rotation")
    turns = turns if isinstance(turns, list) else [turns]
    out["rotation"] = [int(_f(t) or 0) % 360 if int(_f(t) or 0) % 360 in ROTATIONS else 0 for t in turns[:50]]
    return out


ROTATIONS = (0, 90, 180, 270)
# A line item's fields a column heading can be taught to mean: numbers, and text.
LINE_NUMBERS = ("quantity", "rate_per_unit", "discount_percent", "discount_amount", "taxable_value", "gst_rate",
                "amount", "mrp", "igst", "cgst", "sgst", "cess")
LINE_TEXTS = ("description", "item_code", "hsn_sac", "unit", "batch", "expiry", "mfg_date")
LINE_FIELDS = LINE_NUMBERS + LINE_TEXTS
# The items table's columns the reader places (each line's cell = its row
# across the column).
TABLE_COLUMNS = ("description", "item_code", "hsn_sac", "quantity", "unit", "rate_per_unit", "discount",
                 "taxable_value", "gst_rate", "cgst", "sgst", "igst", "amount")


def turn_point(x: float, y: float, deg: int) -> tuple[float, float]:
    """A point (0-1 of the page) after turning the page `deg` clockwise."""
    if deg == 90:
        return 1 - y, x
    if deg == 180:
        return 1 - x, 1 - y
    if deg == 270:
        return y, 1 - x
    return x, y


def turn_box(box, deg: int):
    """[page, ymin, xmin, ymax, xmax] (0-1000) on a page turned `deg` clockwise."""
    if not box or not deg:
        return box
    p, y0, x0, y1, x1 = box
    a = turn_point(x0 / 1000, y0 / 1000, deg)
    b = turn_point(x1 / 1000, y1 / 1000, deg)
    xs, ys = sorted((a[0], b[0])), sorted((a[1], b[1]))
    return [p, round(ys[0] * 1000, 1), round(xs[0] * 1000, 1), round(ys[1] * 1000, 1), round(xs[1] * 1000, 1)]


def turn_boxes(extracted: dict, turns: dict) -> None:
    """Every box on a page that is turned, turned with it."""
    if not turns:
        return
    turns = {int(k): int(v) for k, v in turns.items()}
    for key in ("boxes", "columns"):
        extracted[key] = {k: turn_box(b, turns.get(b[0], 0)) for k, b in (extracted.get(key) or {}).items()}
    for item in extracted.get("line_items") or []:
        if item.get("box"):
            item["box"] = turn_box(item["box"], turns.get(item["box"][0], 0))


def pdf_text_turns(pdf_bytes: bytes) -> dict[int, int]:
    """For a PDF with real text, how far each page must turn clockwise for
    that text to read upright - worked out from the text's own direction,
    no guessing. Pages without enough text are left out."""
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = {}
    try:
        for i, pg in enumerate(doc):
            weight = dict.fromkeys(ROTATIONS, 0)
            for block in pg.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    dx, dy = line.get("dir", (1, 0))
                    chars = sum(len((s.get("text") or "").strip()) for s in line.get("spans", []))
                    if abs(dx) > 0.9:
                        weight[0 if dx > 0 else 180] += chars
                    elif abs(dy) > 0.9:
                        weight[90 if dy < 0 else 270] += chars
            total = sum(weight.values())
            best = max(weight, key=weight.get)
            if total < 40 or weight[best] < total * 0.6:
                continue
            extra = (best - pg.rotation) % 360
            if extra:
                out[i] = extra
    finally:
        doc.close()
    return out


def turn_pdf(pdf_bytes: bytes, turns: dict) -> bytes:
    """The PDF with pages turned clockwise (only the page's rotation is
    changed - nothing is re-drawn or lost)."""
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for i, deg in turns.items():
            i = int(i)
            if 0 <= i < len(doc) and int(deg) % 360:
                doc[i].set_rotation((doc[i].rotation + int(deg)) % 360)
        return doc.tobytes()
    finally:
        doc.close()


def turn_image(img_bytes: bytes, deg: int, quality: int = 92) -> bytes:
    """A photo turned `deg` clockwise (and upright from its EXIF first)."""
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(img_bytes))).convert("RGB")
    flip = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}
    if int(deg) % 360 in flip:
        img = img.transpose(flip[int(deg) % 360])
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def _box(value):
    """[image, ymin, xmin, ymax, xmax] (0-1000) from the model, or None
    when it isn't a sensible box."""
    if not isinstance(value, (list, tuple)) or len(value) not in (4, 5):
        return None
    try:
        nums = [float(v) for v in value]
    except (TypeError, ValueError):
        return None
    if len(nums) == 4:
        nums = [0.0] + nums
    i, y0, x0, y1, x1 = int(nums[0]), *(min(1000.0, max(0.0, v)) for v in nums[1:])
    if i < 0 or y1 - y0 < 2 or x1 - x0 < 2:
        return None
    return [i, round(y0, 1), round(x0, 1), round(y1, 1), round(x1, 1)]


def place_boxes(extracted: dict, page_map: list[int], layout: dict | None = None) -> None:
    """The model's boxes are on the images it was sent; turn them into
    boxes on the document's own pages ([page, ymin, xmin, ymax, xmax],
    0-1000 of that page): through the stitch layout when the pages went
    as one tall image, and the page map when some pages were left out."""
    def on_page(box):
        if not box:
            return None
        i, y0, x0, y1, x1 = box
        if layout:
            W, H = layout["size"]
            cy = (y0 + y1) / 2 / 1000 * H
            p = next((n for n, (_, top, _, h) in enumerate(layout["pages"]) if top <= cy < top + h),
                     len(layout["pages"]) - 1)
            left, top, w, h = layout["pages"][p]
            conv = lambda v, size, off, span: round(min(1000.0, max(0.0, (v / 1000 * size - off) / span * 1000)), 1)  # noqa: E731
            y0, y1 = conv(y0, H, top, h), conv(y1, H, top, h)
            x0, x1 = conv(x0, W, left, w), conv(x1, W, left, w)
            i = p
        if i >= len(page_map) or y1 - y0 < 1 or x1 - x0 < 1:
            return None
        return [page_map[i], y0, x0, y1, x1]

    for key in ("boxes", "columns"):
        extracted[key] = {k: b for k, b in ((k, on_page(v)) for k, v in (extracted.get(key) or {}).items()) if b}
    for item in extracted.get("line_items") or []:
        item["box"] = on_page(item.get("box"))


def _empty_read() -> dict:
    out = {k: None for k in TEXT_FIELDS}
    out.update({"invoice_value": None, "taxable_value": None, "line_items": [], "category": "Purchase",
                "confidence": 0.0, "flags": ["all_fields"]})
    out.update({k: 0 for k in MONEY_FIELDS})
    return out


def extract_fields(pages: list[bytes], settings: Settings, deadline: float, hint: str = "",
                   models: list[str] | None = None, http=requests,
                   page_map: list[int] | None = None, phone_text: str = "",
                   teach: str = "") -> tuple[dict, str | None]:
    """`page_map`: the document page each image is (pages left out of the
    read shift the rest); the boxes read come back on those pages."""
    parts, layout = [], None
    page_map = page_map if page_map and len(page_map) == len(pages) else list(range(len(pages)))
    if 2 <= len(pages) <= 4:
        try:
            layout = {}
            parts.append(_image_part(_stitch_pages(pages, layout)))
        except Exception:  # noqa: BLE001 - separate pages instead
            parts, layout = [], None
    if not parts:
        for i, page in enumerate(pages):
            if len(pages) > 1:
                parts.append({"text": f"PAGE {i + 1} of {len(pages)}:"})
            parts.append(_image_part(page))
    prompt = PROMPT
    if phone_text.strip():
        prompt += ("\nTEXT THE PHONE'S OWN OCR READ FROM THIS BILL (it can contain mistakes - use it only to "
                   "settle characters you are unsure of, never over what the image clearly shows):\n<<<\n"
                   + phone_text.strip()[:6000] + "\n>>>")
    if teach.strip():
        prompt += ("\nWHAT THIS BUSINESS HAS TAUGHT YOU FROM ITS EARLIER BILLS (follow it where it fits this "
                   "bill; never invent a row or a value this bill doesn't show):\n" + teach.strip()[:4000])
    parts.append({"text": prompt + ("\nIMPORTANT CORRECTION PASS: " + hint if hint else "")})
    text, model = call_model(parts, settings, models or settings.models, deadline, http=http)
    if not text:
        return _empty_read(), None
    try:
        shaped = _shape(json.loads(_clean_json(text)))
    except (ValueError, TypeError, AttributeError):
        return _empty_read(), model
    turns = shaped.pop("rotation", None) or []
    if len(turns) == 1 and len(page_map) > 1:
        turns = turns * len(page_map)   # one answer for the whole bill
    shaped["turns"] = {page_map[i]: t for i, t in enumerate(turns[:len(page_map)]) if t}
    try:
        place_boxes(shaped, page_map, layout)
    except Exception:  # noqa: BLE001 - the boxes are a nicety, never a failed read
        log.info("couldn't place the boxes", exc_info=True)
        shaped["boxes"] = {}
    return shaped, model


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


def pdf_to_pages(pdf_bytes: bytes, settings: Settings, max_px: int = 2000,
                 kept: list | None = None) -> tuple[list[bytes], list[bytes]]:
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
                if kept is not None:
                    kept.append(i)
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
    then the company name, then the model's own word, then Purchase -
    exactly the parser's _classify_category, which the validator relies on."""
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
    """The e-invoice QR is signed by the IRP, so what it says wins: the
    GSTINs, invoice number, date and total from it replace a different
    read (the print's reading is kept in a note)."""
    replaced, notes = [], []
    for fld in ("supplier_gstin", "receiver_gstin"):
        qv, ov = clean_gstin(qr.get(fld)), clean_gstin(extracted.get(fld))
        if qv and ov != qv:
            extracted[fld] = qv
            replaced.append(fld)
            notes.append(f"{fld.replace('_', ' ').capitalize()} taken from the e-invoice QR (the print read "
                         f"'{ov or 'blank'}').")
    q_inv, o_inv = (qr.get("invoice_number") or "").strip(), (extracted.get("invoice_number") or "").strip()
    if q_inv and o_inv != q_inv:
        extracted["invoice_number"] = q_inv
        replaced.append("invoice_number")
        if o_inv and norm_invno(o_inv) != norm_invno(q_inv):
            notes.append(f"Invoice number taken from the e-invoice QR (the print read '{o_inv}').")
    q_dt, o_dt = (qr.get("invoice_date") or "").strip(), (extracted.get("invoice_date") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", q_dt) and q_dt != o_dt:
        extracted["invoice_date"] = q_dt
        replaced.append("invoice_date")
        if o_dt:
            notes.append(f"Invoice date taken from the e-invoice QR (the print read {o_dt}).")
    q_val, o_val = _to_num(qr.get("invoice_value")), _to_num(extracted.get("invoice_value"))
    tds = _to_num(extracted.get("tds"))
    # The QR carries the invoice's total; a bill printing "Less TDS" was
    # read as the net, which already agrees with it.
    net_agrees = tds > 0 and abs(q_val - (o_val + tds)) <= 1.0
    if q_val > 0 and abs(q_val - o_val) > 0.009 and not net_agrees:
        extracted["invoice_value"] = q_val
        replaced.append("invoice_value")
        if o_val > 0 and abs(q_val - o_val) > 1.0:
            notes.append(f"Total taken from the e-invoice QR: Rs. {q_val:,.2f} (the print read Rs. {o_val:,.2f}).")
    if qr.get("irn"):
        extracted["irn"] = qr["irn"]
    extracted["einvoice"] = {"present": True, "verified": not notes, "replaced": replaced, "mismatch": [],
                             "notes": notes, "qr": {k: qr.get(k) for k in ("supplier_gstin", "receiver_gstin",
                                                                          "invoice_number", "invoice_date",
                                                                          "invoice_value")}}
    # A value from the QR is certain: never flagged for a second look.
    extracted["flags"] = [f for f in (extracted.get("flags") or []) if f not in replaced]


def _hold_qr(extracted: dict) -> None:
    """After every repair: the QR's values stand (a repair or a near-miss
    snap must not undo them)."""
    qr = (extracted.get("einvoice") or {}).get("qr") or {}
    for fld in ("supplier_gstin", "receiver_gstin"):
        if clean_gstin(qr.get(fld)):
            extracted[fld] = clean_gstin(qr.get(fld))
    if (qr.get("invoice_number") or "").strip():
        extracted["invoice_number"] = qr["invoice_number"].strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", (qr.get("invoice_date") or "").strip()):
        extracted["invoice_date"] = qr["invoice_date"].strip()


def rows_gap(extracted: dict) -> tuple[float, float, float]:
    """(the rows' taxable added up, the bill's own taxable value, how much
    of it no row accounts for). 0 short when the bill has no taxable figure."""
    rows = round(sum(_f(li.get("taxable_value")) or 0 for li in extracted.get("line_items") or []), 2)
    bill = _f(extracted.get("taxable_value")) or 0
    return rows, bill, round(bill - rows, 2) if bill else 0.0


def rows_short(extracted: dict) -> bool:
    """The rows come to less than the bill's taxable value - rows were missed
    (a page break, a sub-heading, freight or a discount row)."""
    _, bill, missing = rows_gap(extracted)
    return bill > 0 and missing > max(1.0, bill * 0.005)


def lessons_for(ctx: Context, gstins) -> tuple[str, list[str]]:
    """The taught text for these supplier GSTINs (and for every bill), and
    which of them were used."""
    used, texts = [], []
    for key in [""] + [g for g in dict.fromkeys(gstins) if g]:
        if ctx.lessons.get(key):
            used.append(key)
            texts.append(ctx.lessons[key])
    return "\n".join(texts), used


def _heading_key(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def headings_for(ctx: Context, gstins) -> list[tuple[str, str]]:
    """The heading rules for these supplier GSTINs, then every bill's; the
    first rule for a heading wins."""
    out, seen = [], set()
    for key in [g for g in dict.fromkeys(gstins) if g] + [""]:
        for heading, fld in ctx.headings.get(key) or []:
            k = _heading_key(heading)
            if k and k not in seen:
                seen.add(k)
                out.append((heading, fld))
    return out


def apply_headings(extracted: dict, rules: list[tuple[str, str]]) -> list[str]:
    """What was taught about headings, applied as fixed rules to what the
    reader returned: a value the reader put under another field although its
    own printed label is a taught heading is moved to the taught field; so is
    a "label: value" left in other_references; a column read under the wrong
    name is moved (or swapped with the column that took its place). Returns
    what it changed, in words. Nothing is invented: only values the reader
    read are moved."""
    if not rules:
        return []
    notes = []
    top = {_heading_key(h): (h, f) for h, f in reversed(rules) if not f.startswith("col:")}
    cols = {_heading_key(h): (h, f[4:]) for h, f in reversed(rules) if f.startswith("col:")}
    numbers = set(MONEY_FIELDS) | {"invoice_value", "taxable_value"}

    def put(fld, value):
        if fld in numbers:
            number = _f(value)
            if number is None:
                return False
            extracted[fld] = number
        else:
            extracted[fld] = str(value).strip() or None
        return True

    def pending(said, taught, same_kind):
        """(the field the value is under, the taught heading, the field it belongs in) still to move."""
        out = []
        for was, label in said.items():
            rule = taught.get(_heading_key(label))
            if rule and rule[1] != was and same_kind(was, rule[1]):
                out.append((was, rule[0], rule[1]))
        return out

    def next_move(moves):
        # A field that is itself waiting to move is emptied first, so nothing is overwritten.
        waiting = {m[0] for m in moves}
        return next((m for m in moves if m[2] not in waiting), moves[0])

    printed = {k: v for k, v in (extracted.get("headings") or {}).items() if extracted.get(k) not in (None, "", 0)}
    for _ in range(len(printed)):
        moves = pending(printed, top, lambda a, b: (a in numbers) == (b in numbers))
        if not moves:
            break
        was, heading, fld = next_move(moves)
        if not put(fld, extracted[was]):
            printed.pop(was)
            continue
        extracted[was] = 0 if was in numbers else None
        boxes = extracted.get("boxes") or {}
        if was in boxes:
            boxes[fld] = boxes.pop(was)
        printed[fld] = printed.pop(was)
        notes.append(f"'{heading}' is {fld.replace('_', ' ')} on this supplier's bills (taught): moved from "
                     f"{was.replace('_', ' ')}.")
    if extracted.get("headings"):
        extracted["headings"] = printed

    # "Cust. PO: 4500012345; Indent: 77" left under other references.
    pairs = [p.strip() for p in re.split(r"[;\n]+", str(extracted.get("other_references") or "")) if p.strip()]
    left = []
    for pair in pairs:
        label, sep, value = pair.partition(":")
        rule = top.get(_heading_key(label)) if sep and value.strip() else None
        if rule and extracted.get(rule[1]) in (None, "", 0) and put(rule[1], value):
            notes.append(f"'{rule[0]}' is {rule[1].replace('_', ' ')} on this supplier's bills (taught): taken "
                         "from the other references.")
        else:
            left.append(pair)
    if len(left) != len(pairs):
        extracted["other_references"] = "; ".join(left) or None

    said = dict(extracted.get("column_headings") or {})
    for _ in range(len(said)):
        moves = pending(said, cols, lambda a, b: b in LINE_FIELDS and (a in LINE_NUMBERS) == (b in LINE_NUMBERS))
        if not moves:
            break
        was, heading, fld = next_move(moves)
        empty = 0 if fld in LINE_NUMBERS else ""
        # Two columns read under each other's names swap back.
        swap = fld in said
        for item in extracted.get("line_items") or []:
            item[fld], item[was] = item.get(was, empty), (item.get(fld, empty) if swap else empty)
        columns = extracted.get("columns") or {}
        if was in columns:
            mine = columns.pop(was)
            if swap and fld in columns:
                columns[was] = columns[fld]
            columns[fld] = mine
        if swap:
            said[fld], said[was] = said[was], said[fld]
        else:
            said[fld] = said.pop(was)
        notes.append(f"The column '{heading}' is {fld.replace('_', ' ')} on this supplier's bills (taught): "
                     f"{'swapped with' if swap else 'moved from'} {was.replace('_', ' ')}.")
    if said:
        extracted["column_headings"] = said
    return notes


LABEL_PROMPT = ('This is a small piece cut from a bill, around the point a person clicked (its centre). Return ONLY '
                'JSON {"text":"<the heading or label printed at or nearest the centre, exactly as printed, without '
                'the value beside or under it and without a trailing colon>"}. If nothing is printed there, '
                '{"text":""}.')


def read_label(page_jpeg: bytes, x: float, y: float, settings: Settings, http=requests, budget: float = 12.0) -> str:
    """The heading printed where a person clicked on the bill (x, y as 0-1
    of the page) - read from a strip cut around the click."""
    from PIL import Image

    with _RASTER:
        img = Image.open(io.BytesIO(page_jpeg)).convert("RGB")
        w, h = img.size
        half_w, half_h = max(int(w * 0.17), 120), max(int(h * 0.035), 60)
        cx, cy = min(max(x, 0.0), 1.0) * w, min(max(y, 0.0), 1.0) * h
        crop = img.crop((int(max(cx - half_w, 0)), int(max(cy - half_h, 0)), int(min(cx + half_w, w)),
                         int(min(cy + half_h, h))))
        if crop.width < 900:
            scale = 900 / crop.width
            crop = crop.resize((900, max(int(crop.height * scale), 1)))
        buf = io.BytesIO()
        crop.save(buf, "JPEG", quality=90)
    text, _model = call_model([_image_part(buf.getvalue()), {"text": LABEL_PROMPT}], settings, settings.models,
                              time.time() + budget, max_tokens=256, http=http)
    if not text:
        raise ReadError("The reading service didn't answer - type the heading instead.", "model_unavailable")
    try:
        found = json.loads(_clean_json(text)).get("text")
    except (ValueError, TypeError, AttributeError):
        found = text
    return re.sub(r"\s+", " ", str(found or "")).strip(" :-")[:80]


def _gstins_in(text: str) -> list[str]:
    return [m.group(0) for m in re.finditer(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]\b", (text or "").upper())]


def read(file_bytes: bytes, is_pdf: bool, ctx: Context, settings: Settings, http=requests) -> dict:
    """One document -> {extracted, validation, rejection_code,
    rejection_reason, preview, own_gstin, pages_total, engine}."""
    start = time.time()
    deadline = start + settings.budget
    engine = {"models": [], "tier2": False, "retry": False, "qr": "none"}
    original = file_bytes
    turned: dict[int, int] = {}

    if is_pdf:
        # A PDF with real text says itself which way up it is: turned upright
        # before anything is read, so the model and the boxes see it straight.
        try:
            turned = pdf_text_turns(file_bytes)
            if turned:
                file_bytes = turn_pdf(file_bytes, turned)
        except Exception:  # noqa: BLE001 - read it as it is
            log.info("couldn't check the PDF's text direction", exc_info=True)
            turned = {}
        with _RASTER:
            kept: list[int] = []
            pages_full, model_pages = pdf_to_pages(file_bytes, settings, kept=kept)
        if len(model_pages) > settings.max_invoice_pages:
            raise ReadError(f"This {len(model_pages)}-page PDF looks like several invoices together - use "
                            "\"Split into invoices\", or upload the invoices one by one.", "bundle")
        qr_candidates = pages_full
        hires_source = pages_full
        preview_src = pages_full[0]
    else:
        with _RASTER:
            model_pages = [compress(file_bytes, settings.cheap_px, 85, 1200)]
        kept = [0]
        qr_candidates = [file_bytes]
        hires_source = [file_bytes]
        preview_src = file_bytes
    pages_total = len(qr_candidates)

    # The QR is read alongside the model, not after it.
    qr_future = _QR_POOL.submit(decode_einvoice_qr, list(qr_candidates), settings.qr_budget)

    # Lessons already known for this bill: every bill's, and the supplier's
    # when the phone saw its GSTIN (its QR or its own OCR).
    early = ([(ctx.phone_qr or {}).get("supplier_gstin") or ""] + _gstins_in(ctx.phone_text)) if ctx.lessons else []
    teach, used = lessons_for(ctx, early)
    engine["lessons"] = used
    extracted, model = extract_fields(model_pages, settings, deadline, http=http, page_map=kept,
                                      phone_text=ctx.phone_text, teach=teach)
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
    if not qr and ctx.phone_qr and ctx.phone_qr.get("supplier_gstin"):
        qr = ctx.phone_qr   # the phone could read the QR where the picture's copy couldn't be
        engine["qr"] = "phone"
    if qr:
        engine["qr"] = engine["qr"] if engine["qr"] == "phone" else "found"
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

    # Rows missed: the rows come to less than the bill's own taxable value.
    # One more read, told exactly how much is missing and where rows hide,
    # with the strong model on a sharper picture - and with what this
    # business taught about the supplier's bills. Its rows are taken only
    # when they come closer to the bill's taxable value. Checked before the
    # validator, which could otherwise "repair" the total down to the rows.
    rows_note = None
    if rows_short(extracted) and time.time() - start < settings.budget * 0.6:
        rows, bill, missing = rows_gap(extracted)
        engine["rows_pass"] = True
        teach2, used2 = lessons_for(ctx, [clean_gstin(extracted.get("supplier_gstin") or "")] + early)
        engine["lessons"] = sorted(set(engine.get("lessons") or []) | set(used2))
        with _RASTER:
            sharp = [compress(hires_source[i], settings.hires_px, 92, 3500) for i in kept if i < len(hires_source)] \
                if len(kept) <= 2 else list(model_pages)
        if len(sharp) != len(kept):
            sharp = list(model_pages)
        hint = (f"Your item rows add up to Rs {rows:,.2f} but the bill's taxable value is Rs {bill:,.2f} - rows worth "
                f"Rs {missing:,.2f} are missing. Read the item table again from its first row to its last, on every "
                "page: rows after a page break or a 'continued' line, rows under a sub-heading or group title, a "
                "second table, and small rows such as freight, packing, forwarding, loading, insurance or "
                "installation, and a discount or scheme row (a discount is a row with a negative value). Return "
                "EVERY row, each with its own taxable value, so that the rows add up to the taxable value.")
        second, model4 = extract_fields(sharp, settings, deadline, http=http, page_map=kept,
                                        models=[settings.strong_model] + [m for m in settings.models
                                                                          if m != settings.strong_model],
                                        phone_text=ctx.phone_text, teach=teach2, hint=hint)
        engine["models"].append(model4)
        if model4 and second.get("line_items"):
            rows2 = round(sum(_f(li.get("taxable_value")) or 0 for li in second["line_items"]), 2)
            if abs(bill - rows2) < abs(missing) - 0.5:
                extracted["line_items"] = second["line_items"]
                if second.get("columns"):
                    extracted["columns"] = second["columns"]
                if second.get("column_headings"):
                    extracted["column_headings"] = second["column_headings"]
                engine["rows_pass"] = "fixed" if abs(bill - rows2) <= max(1.0, bill * 0.005) else "closer"
                rows_note = (f"Rows re-read: the first reading's rows came to Rs {rows:,.2f} of the Rs {bill:,.2f} "
                             f"taxable; now Rs {rows2:,.2f} ({len(second['line_items'])} rows).")

    # Headings this business taught, as fixed rules on what was read.
    taught_rules = headings_for(ctx, [clean_gstin(extracted.get("supplier_gstin") or "")] + list(early)) \
        if ctx.headings else []
    heading_notes = apply_headings(extracted, taught_rules)
    if heading_notes:
        engine["headings"] = len(heading_notes)

    extracted["category"] = classify_category(extracted, own, company_name)
    validation = validate_and_repair(extracted, company_gstin=own)
    if side_note:
        validation.setdefault("repairs", []).append(side_note)
    for note in heading_notes:
        validation.setdefault("repairs", []).append(note)
    if rows_note:
        validation.setdefault("repairs", []).append(rows_note)

    if (validation.get("status") == "failed" and len(model_pages) <= 2 and not engine.get("rows_pass")
            and time.time() - start < settings.budget * 0.55):
        fails = "; ".join(f"{c['label']} - {c['detail']}" for c in validation.get("checks", [])
                          if not c.get("ok") and c.get("level") == "hard")
        engine["retry"] = True
        second, model3 = extract_fields(model_pages, settings, deadline, http=http, page_map=kept,
                                        phone_text=ctx.phone_text,
                                        hint=f"Your previous reading failed these checks: {fails}. Re-read every "
                                             "amount and GSTIN character-by-character.")
        engine["models"].append(model3)
        if model3:
            if qr:
                _apply_qr(second, qr)
            notes2 = apply_headings(second, taught_rules)
            second["category"] = classify_category(second, own, company_name)
            v2 = validate_and_repair(second, company_gstin=own)
            for note in notes2:
                v2.setdefault("repairs", []).append(note)
            if v2.get("score", 0) > validation.get("score", 0):
                extracted, validation = second, v2

    extracted["category"] = classify_category(extracted, own, company_name)

    # Snap near-misses: to our own GSTIN, then to a supplier we already know.
    known = [clean_gstin(g) for g in ctx.known_gstins if g]
    qr_held = (extracted.get("einvoice") or {}).get("qr") or {}
    for fld in ("supplier_gstin", "receiver_gstin"):
        val = clean_gstin(extracted.get(fld) or "")
        if not val or clean_gstin(qr_held.get(fld)):   # from the signed QR: right as it is
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

    if qr_held:
        _hold_qr(extracted)

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

    # A photo or scan the model says is sideways or upside down: the bill is
    # turned upright, and the boxes read on it with it.
    turns = {int(p): int(t) for p, t in (extracted.pop("turns", None) or {}).items() if int(t) % 360}
    if turns:
        try:
            with _RASTER:
                if is_pdf:
                    file_bytes = turn_pdf(file_bytes, turns)
                    if 0 in turns:
                        preview_src = turn_image(preview_src, turns[0])
                else:
                    file_bytes = turn_image(file_bytes, turns.get(0, 0))
                    preview_src = file_bytes
            turn_boxes(extracted, turns)
            for p, t in turns.items():
                turned[p] = (turned.get(p, 0) + t) % 360
        except Exception:  # noqa: BLE001 - keep it as it came
            log.info("couldn't turn the bill upright", exc_info=True)
    if turned:
        extracted["turned"] = {str(p): t for p, t in turned.items() if t}
        engine["turned"] = extracted["turned"]

    with _RASTER:
        preview = preview_jpeg(preview_src)
    engine["seconds"] = round(time.time() - start, 1)
    return {"extracted": extracted, "validation": validation, "rejection_code": rejection,
            "rejection_reason": reason, "preview": preview, "own_gstin": own, "pages_total": pages_total,
            "engine": engine, "file_data": file_bytes if file_bytes is not original else None}
