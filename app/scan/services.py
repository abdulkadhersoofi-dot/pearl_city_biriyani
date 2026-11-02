"""Scan bills - everything between the reading engine and ARFA's books:
storing an upload, reading it, splitting a bundle, spotting duplicates,
and posting a checked document as a purchase bill or an expense (which
then flows into ITC, GSTR-2B matching, payables and the Tally export like
any bill typed in by hand)."""

from __future__ import annotations

import hashlib
import io
import logging
import re
from datetime import date, datetime, time as dtime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from flask import current_app

from app.extensions import db
from app.models.mixins import utcnow
from app.models.scan import (
    DISCARDED, FAILED, FILED, POSTED, READING, REJECTED, SPLIT, TO_CHECK, UPLOADED, ScanDocument,
)
from app.scan.engine import reader
from app.scan.engine.validators import clean_gstin, gstin_checksum_ok
from app.utils.audit import record_audit
from app.utils.gst import GSTIN_REGEX
from app.utils.gst_config import GST_RATE_SLABS
from app.utils.indian_states import STATE_NAME_BY_CODE

log = logging.getLogger("arfa.scan")

PDF = "application/pdf"
JPEG = "image/jpeg"
IMAGE_TYPES = {b"\xff\xd8\xff": JPEG, b"\x89PNG": "image/png"}
KEEP_ORIGINAL_BYTES = 4 * 1024 * 1024
ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024
SLABS = [Decimal(str(s)) for s in GST_RATE_SLABS]

# What a read's category means for what the user can do with it.
KIND_PURCHASE, KIND_EXPENSE, KIND_FILE = "purchase", "expense", "file"
CATEGORY_LABELS = {
    "Purchase": "Purchase bill", "Expenses": "Expense bill", "Sales": "Sales invoice (issued by you)",
    "Purchase - Credit Note": "Credit note from a supplier", "Purchase - Debit Note": "Debit note to a supplier",
    "Sales - Credit Note": "Credit note you issued", "Sales - Debit Note": "Debit note you issued",
    "Receipts": "Payment receipt",
}


class ScanError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# --- set-up ----------------------------------------------------------------------------------

def engine_settings() -> reader.Settings | None:
    """How the engine reaches Gemini: Vertex AI through the Cloud project
    (no key) when SCAN_PROVIDER is "vertex", else an AI Studio key. None
    when neither is set up."""
    cfg = current_app.config
    common = {"models": list(cfg.get("SCAN_MODELS") or reader.DEFAULT_MODELS),
              "strong_model": cfg.get("SCAN_STRONG_MODEL") or reader.DEFAULT_STRONG_MODEL}
    if cfg.get("SCAN_PROVIDER") == "vertex":
        project = (cfg.get("GCP_PROJECT") or "").strip()
        if not project:
            return None
        return reader.Settings(provider="vertex", project=project,
                               location=cfg.get("SCAN_VERTEX_LOCATION") or "asia-south1", **common)
    key = (cfg.get("GEMINI_API_KEY") or "").strip()
    if not key:
        return None
    return reader.Settings(api_key=key, **common)


def is_set_up() -> bool:
    return engine_settings() is not None


def reads_today(tenant_id: int) -> int:
    start = datetime.combine(date.today(), dtime.min).astimezone(timezone.utc)
    return ScanDocument.query.filter(ScanDocument.tenant_id == tenant_id, ScanDocument.read_at >= start).count()


def daily_limit() -> int:
    return int(current_app.config.get("SCAN_DAILY_LIMIT") or 300)


# --- upload ----------------------------------------------------------------------------------

def sniff(data: bytes) -> str | None:
    if data[:5] == b"%PDF-":
        return PDF
    for magic, kind in IMAGE_TYPES.items():
        if data.startswith(magic):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _as_jpeg(data: bytes, px: int = 2600) -> bytes:
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    if max(img.size) > px:
        img.thumbnail((px, px))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


def upload(tenant, user, filename: str, data: bytes) -> tuple[ScanDocument, bool]:
    """Stores one photo or PDF. (document, False) - or (the earlier
    upload, True) when this exact file was uploaded before."""
    if not data:
        raise ScanError("That file is empty.")
    limit_mb = int(current_app.config.get("SCAN_MAX_FILE_MB") or 15)
    if len(data) > limit_mb * 1024 * 1024:
        raise ScanError(f"That file is over {limit_mb} MB - take the photo again or split the PDF.")
    kind = sniff(data)
    if kind is None:
        raise ScanError("Send a photo (JPG, PNG or WebP) or a PDF.")
    if kind != PDF and (kind != JPEG or len(data) > KEEP_ORIGINAL_BYTES):
        try:
            data, kind = _as_jpeg(data), JPEG
        except Exception:  # noqa: BLE001
            raise ScanError("That picture couldn't be opened - take it again.")
    pages = 1
    if kind == PDF:
        try:
            pages = reader.pdf_page_count(data)
        except Exception:  # noqa: BLE001
            raise ScanError("That PDF couldn't be opened - it may be damaged or password-protected.")
    digest = hashlib.sha256(data).hexdigest()
    earlier = (ScanDocument.query.filter(ScanDocument.tenant_id == tenant.id, ScanDocument.file_hash == digest,
                                         ScanDocument.status != DISCARDED)
               .order_by(ScanDocument.id.desc()).first())
    if earlier is not None:
        return earlier, True
    doc = ScanDocument(tenant_id=tenant.id, uploaded_by_id=user.id, filename=(filename or "bill")[:255],
                       content_type=kind, file_hash=digest, file_data=data, page_count=pages, status=UPLOADED)
    db.session.add(doc)
    db.session.flush()
    record_audit(user, "scan_uploaded", tenant_id=tenant.id, entity_type="scan_document", entity_id=doc.id,
                 details={"pages": pages, "type": kind})
    return doc, False


# --- reading ---------------------------------------------------------------------------------

def _context(tenant) -> reader.Context:
    from app.models.customer import Customer
    from app.models.purchase import Supplier
    from app.models.tenant import Gstin

    own = [g.gstin for g in tenant.gstins.filter(Gstin.is_active.is_(True), Gstin.gstin.isnot(None))
           .order_by(Gstin.is_primary.desc(), Gstin.id)]
    known = {s for (s,) in db.session.query(Supplier.gstin).filter(Supplier.tenant_id == tenant.id,
                                                                    Supplier.gstin.isnot(None))}
    known |= {c for (c,) in db.session.query(Customer.gstin).filter(Customer.tenant_id == tenant.id,
                                                                     Customer.gstin.isnot(None))}
    return reader.Context(own_gstins=own, company_name=tenant.legal_name or "", known_gstins=sorted(known))


def _own_gstin_row(tenant, gstin: str):
    from app.models.tenant import Gstin

    if not gstin:
        return None
    return tenant.gstins.filter(Gstin.gstin == gstin, Gstin.is_active.is_(True)).first()


def display_category(extracted: dict) -> str | None:
    """What the bill is, for the screens: the validated direction, except a
    bill bought for the business's own use (the model's "Expenses") shows
    as an expense bill."""
    category = extracted.get("category")
    if category == "Purchase" and (extracted.get("model_category") or "").strip().lower() == "expenses":
        return "Expenses"
    return category


def find_duplicate(tenant, doc: ScanDocument) -> str | None:
    """An already-entered bill with the same seller GSTIN and number."""
    from app.models.expense import Expense, ExpenseStatus
    from app.models.purchase import PurchaseBill, PurchaseStatus, Supplier

    ex = doc.extracted or {}
    seller = clean_gstin(ex.get("supplier_gstin") or "")
    number = (ex.get("invoice_number") or "").strip()
    if not number:
        return None
    if seller:
        bill = (PurchaseBill.query.join(Supplier, Supplier.id == PurchaseBill.supplier_id)
                .filter(PurchaseBill.tenant_id == tenant.id, PurchaseBill.status == PurchaseStatus.POSTED,
                        Supplier.gstin == seller, db.func.lower(PurchaseBill.supplier_invoice_no) == number.lower())
                .first())
        if bill is not None:
            return f"Purchase bill {bill.number}"
        expense = Expense.query.filter(Expense.tenant_id == tenant.id, Expense.status == ExpenseStatus.POSTED,
                                       Expense.supplier_gstin == seller,
                                       db.func.lower(Expense.bill_number) == number.lower()).first()
        if expense is not None:
            return f"Expense {expense.number}"
    key = reader.norm_invno(number)
    others = (ScanDocument.query.filter(ScanDocument.tenant_id == tenant.id, ScanDocument.id != doc.id,
                                        ScanDocument.status.in_((TO_CHECK, POSTED, FILED)))
              .order_by(ScanDocument.id.desc()).limit(3000))
    for other in others:
        oex = other.extracted or {}
        if reader.norm_invno(oex.get("invoice_number")) == key and \
                clean_gstin(oex.get("supplier_gstin") or "") == seller:
            return f"Scanned bill #{other.id}"
    return None


def read_document(tenant, user, doc: ScanDocument, http=None) -> list[ScanDocument]:
    """Reads one document. A PDF holding several invoices is split instead,
    and its parts are returned to be read one by one."""
    if doc.status not in (UPLOADED, FAILED, TO_CHECK, REJECTED, READING):
        raise ScanError("This document has already been dealt with.")
    settings = engine_settings()
    if settings is None:
        raise ScanError("Reading bills isn't set up yet - the AI key hasn't been added.")
    if reads_today(tenant.id) >= daily_limit():
        raise ScanError(f"Today's limit of {daily_limit()} bills has been reached - the rest wait until tomorrow.")
    kwargs = {"http": http} if http is not None else {}

    if doc.is_pdf and (doc.page_count or 1) > 1 and doc.parent_id is None:
        try:
            groups, method = reader.split_groups(doc.file_data, settings, **kwargs)
        except Exception as exc:  # noqa: BLE001 - read it whole instead
            log.warning("split check failed for scan %s: %s", doc.id, exc)
            groups, method = [], "error"
        if len(groups) > 1:
            return _split(tenant, user, doc, groups, method)

    doc.status = READING
    db.session.commit()
    try:
        result = reader.read(doc.file_data, doc.is_pdf, _context(tenant), settings, **kwargs)
    except reader.ReadError as exc:
        doc.status, doc.message, doc.rejection_code = FAILED, exc.message, exc.code
        doc.read_at = utcnow()
        db.session.commit()
        return [doc]
    except Exception:  # noqa: BLE001 - never leave it stuck on "reading"
        log.exception("reading scan %s failed", doc.id)
        db.session.rollback()
        doc.status, doc.message, doc.rejection_code = FAILED, "This file couldn't be read - try again.", "error"
        doc.read_at = utcnow()
        db.session.commit()
        return [doc]

    doc.extracted = result["extracted"]
    doc.validation = result["validation"]
    doc.engine = result["engine"]
    doc.preview = result["preview"]
    doc.page_count = result["pages_total"]
    doc.category = display_category(result["extracted"] or {})
    doc.read_at = utcnow()
    own = _own_gstin_row(tenant, result.get("own_gstin"))
    doc.gstin_id = own.id if own else None
    if result["rejection_code"]:
        doc.status, doc.rejection_code, doc.message = REJECTED, result["rejection_code"], result["rejection_reason"]
    else:
        doc.status, doc.rejection_code, doc.message = TO_CHECK, None, None
    doc.duplicate_of = find_duplicate(tenant, doc)
    record_audit(user, "scan_read", tenant_id=tenant.id, entity_type="scan_document", entity_id=doc.id,
                 details={"status": doc.status, "category": doc.category,
                          "check": (doc.validation or {}).get("status"), **(doc.engine or {})})
    db.session.commit()
    return [doc]


def _split(tenant, user, doc, groups, method) -> list[ScanDocument]:
    base = re.sub(r"\.pdf$", "", doc.filename, flags=re.I)
    children = []
    for n, group in enumerate(groups, 1):
        data = reader.split_pdf(doc.file_data, group["page_indices"])
        child = ScanDocument(tenant_id=tenant.id, uploaded_by_id=doc.uploaded_by_id, parent_id=doc.id,
                             filename=f"{base} ({n} of {len(groups)}).pdf"[:255], content_type=PDF,
                             file_hash=hashlib.sha256(data).hexdigest(), file_data=data,
                             page_count=len(group["page_indices"]), status=UPLOADED)
        db.session.add(child)
        children.append(child)
    doc.status = SPLIT
    doc.message = f"Split into {len(groups)} invoices ({method})."
    db.session.flush()
    record_audit(user, "scan_split", tenant_id=tenant.id, entity_type="scan_document", entity_id=doc.id,
                 details={"parts": len(groups), "method": method})
    db.session.commit()
    return children


# --- what the review screen starts from --------------------------------------------------------

def _money(v) -> Decimal:
    try:
        return Decimal(str(v or 0)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    except InvalidOperation:
        return Decimal("0.00")


def _slab(rate) -> Decimal:
    r = _money(rate)
    return min(SLABS, key=lambda s: abs(s - r))


def suggested_kind(tenant, doc: ScanDocument) -> str:
    cat = doc.category or "Purchase"
    purchases, expenses = tenant.has_feature("purchases"), tenant.has_feature("expenses")
    if cat == "Purchase":
        return KIND_PURCHASE if purchases else (KIND_EXPENSE if expenses else KIND_FILE)
    if cat == "Expenses":
        return KIND_EXPENSE if expenses else (KIND_PURCHASE if purchases else KIND_FILE)
    return KIND_FILE


def line_rows(doc: ScanDocument) -> list[dict]:
    """The bill's lines as ARFA purchase lines: quantity x rate less a
    discount gives the taxable value printed on the bill."""
    rows = []
    for item in (doc.extracted or {}).get("line_items") or []:
        taxable = _money(item.get("taxable_value"))
        qty = Decimal(str(item.get("quantity") or 0)).quantize(Decimal("0.001"))
        rate = _money(item.get("rate_per_unit"))
        discount = Decimal("0")
        if qty <= 0:
            qty = Decimal("1")
            rate = taxable if taxable > 0 else rate
        elif taxable > 0:
            gross = (qty * rate).quantize(Decimal("0.01"))
            if rate <= 0 or gross < taxable - Decimal("0.5"):
                rate = (taxable / qty).quantize(Decimal("0.01"), ROUND_HALF_UP)
            elif gross > taxable + Decimal("0.5"):
                discount = ((1 - taxable / gross) * 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
                if not (Decimal("0") < discount < Decimal("100")):
                    discount, rate = Decimal("0"), (taxable / qty).quantize(Decimal("0.01"), ROUND_HALF_UP)
        gst = _slab(item.get("gst_rate"))
        if gst == 0 and taxable > 0:
            tax = _money(item.get("igst")) + _money(item.get("cgst")) + _money(item.get("sgst"))
            if tax > 0:
                gst = _slab(tax / taxable * 100)
        rows.append({"description": (item.get("description") or "Item").strip()[:255] or "Item",
                     "hsn": re.sub(r"\D", "", str(item.get("hsn_sac") or "")),
                     "qty": f"{qty.normalize():f}", "unit": unit_code(item.get("unit") or ""),
                     "rate": f"{rate:.2f}", "discount": f"{discount:.2f}", "gst_rate": f"{gst.normalize():f}"})
    return rows or [{"description": "Item", "hsn": "", "qty": "1", "unit": "NOS",
                     "rate": f"{_money((doc.extracted or {}).get('taxable_value')):.2f}", "discount": "0.00",
                     "gst_rate": "18"}]


def main_gst_rate(doc: ScanDocument) -> Decimal:
    rows = line_rows(doc)
    by_rate: dict[str, Decimal] = {}
    for r in rows:
        by_rate[r["gst_rate"]] = by_rate.get(r["gst_rate"], Decimal(0)) + Decimal(r["rate"]) * Decimal(r["qty"])
    return Decimal(max(by_rate, key=by_rate.get)) if by_rate else Decimal("0")


UNIT_ALIASES = {"KG": "KGS", "KGS.": "KGS", "PC": "PCS", "PCS.": "PCS", "EA": "NOS", "EACH": "NOS", "NO": "NOS",
                "NOS.": "NOS", "UNIT": "UNT", "UNITS": "UNT", "LT": "LTR", "L": "LTR", "LTRS": "LTR", "M": "MTR",
                "MTRS": "MTR", "GM": "GMS", "G": "GMS", "GRAMS": "GMS", "PKT": "PAC", "PACK": "PAC", "PKTS": "PAC",
                "DOZEN": "DOZ", "PAIR": "PRS", "PAIRS": "PRS", "BOTTLE": "BTL", "SETS": "SET", "ROLL": "ROL"}


def unit_code(unit: str) -> str:
    """A unit as printed -> the GST portal's unit code (OTH when unknown)."""
    from app.utils.gst_config import UQC_CODES

    u = re.sub(r"[^A-Z.]", "", (unit or "").upper())
    u = UNIT_ALIASES.get(u, u.rstrip("."))
    return u if u in UQC_CODES else ("NOS" if not u else "OTH")


def supplier_choices(tenant, doc: ScanDocument) -> tuple[object, list]:
    """(the supplier this bill is from, if known, [suppliers it could be]):
    the same GSTIN first, then the same or a similar name."""
    from app.models.purchase import Supplier

    ex = doc.extracted or {}
    gstin = clean_gstin(ex.get("supplier_gstin") or "")
    name = (ex.get("supplier_name") or "").strip()
    suppliers = Supplier.query.filter_by(tenant_id=tenant.id, is_active=True).order_by(Supplier.name).all()
    if gstin:
        exact = next((s for s in suppliers if s.gstin == gstin), None)
        if exact is not None:
            return exact, [exact]
    same_name = [s for s in suppliers if name and s.name.strip().lower() == name.lower()
                 and (not gstin or not s.gstin)]
    similar = [s for s in suppliers if name and s not in same_name and reader.name_matches(s.name, name)]
    candidates = (same_name + similar)[:6]
    return (same_name[0] if same_name else None), candidates


def matched_supplier(tenant, doc: ScanDocument):
    return supplier_choices(tenant, doc)[0]


# Words on a bill that say which expense category it belongs to (matched
# against the client's own category names, which they can rename).
CATEGORY_HINTS = (
    ("repair", ("repair", "service", "servicing", "spare", "mechanic", "garage", "workshop", "auto", "motor",
                "alignment", "washing", "labour", "tyre", "battery", "check charges")),
    ("fuel", ("petrol", "diesel", "fuel", "hpcl", "iocl", "bpcl", "indian oil", "bharat petroleum", "filling station")),
    ("transport", ("transport", "logistic", "courier", "freight", "cargo", "travel", "taxi", "cab ", "uber", "ola ",
                   "bus ", "railway", "airline", "flight", "parcel")),
    ("telephone", ("airtel", "jio", "vodafone", "bsnl", "broadband", "internet", "telephone", "recharge")),
    ("electricity", ("electricity", "tneb", "bescom", "msedcl", "tangedco", "power bill", "energy charges")),
    ("rent", (" rent", "rental", "lease")),
    ("professional", ("consult", "professional", "legal", "advocate", "chartered", "audit fee")),
    ("bank", ("bank charges", "processing fee")),
    ("packing", ("packing", "carton", "corrugated", "tape", "pouch")),
    ("advert", ("advertis", "flex", "banner", "hoarding", "marketing", "pamphlet")),
    ("office", ("stationery", "xerox", "cartridge", "toner", "printing", "office")),
)


def guess_category(doc: ScanDocument, categories: list):
    """The expense category the bill's wording points to, among the client's
    own categories (Miscellaneous, or the first, when nothing fits)."""
    ex = doc.extracted or {}
    text = " " + " ".join([ex.get("supplier_name") or "", ex.get("notes") or ""] +
                          [i.get("description") or "" for i in ex.get("line_items") or []]).lower() + " "
    for key, words in CATEGORY_HINTS:
        if any(w in text for w in words):
            found = next((c for c in categories if key in c.name.lower()), None)
            if found is not None:
                return found
    return next((c for c in categories if "misc" in c.name.lower()), categories[0] if categories else None)


def _tokens(text: str) -> set:
    return {w for w in re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split() if len(w) > 1}


def item_choices(tenant, rows: list[dict]) -> list[dict]:
    """For each bill line, the catalogue items it could be and the one to
    start with: an item of the same name (or a close name with the same
    HSN); else a new item for goods, no item for a service."""
    from app.models.product import Product

    products = [p for p in Product.query.filter_by(tenant_id=tenant.id, is_active=True).all() if not p.has_variants]
    goods_module = tenant.has_feature("inventory") or tenant.has_feature("purchases")
    out = []
    for row in rows:
        desc, hsn = (row.get("description") or "").strip(), (row.get("hsn") or "").strip()
        words = _tokens(desc)
        scored = []
        for p in products:
            if p.name.strip().lower() == desc.lower():
                score = 100
            else:
                overlap = len(words & _tokens(p.name))
                if not overlap:
                    continue
                score = 40 + 10 * overlap + (25 if hsn and p.hsn_or_sac_code == hsn else 0)
            scored.append((score, p))
        scored.sort(key=lambda sp: (-sp[0], sp[1].name))
        options = [{"id": str(p.id), "name": p.name} for _, p in scored[:6]]
        if scored and scored[0][0] >= 80:   # the same name, or most of it with the same HSN
            choice = str(scored[0][1].id)
        elif goods_module and hsn and not hsn.startswith("99"):
            choice = "new"
        else:
            choice = ""
        out.append({**row, "product_id": row.get("product_id") or choice, "options": options})
    return out


# --- posting ---------------------------------------------------------------------------------

def _check_open(doc: ScanDocument):
    if doc.status != TO_CHECK:
        raise ScanError("Only a read bill waiting to be checked can be posted.")


def _date(value, label) -> date:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        raise ScanError(f"Enter the {label}.")


def _supplier_from_form(tenant, form):
    from app.models.purchase import Supplier

    sid = form.get("supplier_id", type=int)
    if sid:
        supplier = Supplier.query.filter_by(id=sid, tenant_id=tenant.id).first()
        if supplier is None:
            raise ScanError("Pick a valid supplier.")
        return supplier
    name = (form.get("supplier_name") or "").strip()[:255]
    gstin = clean_gstin(form.get("supplier_gstin") or "") or None
    if not name:
        raise ScanError("Enter the supplier's name.")
    if gstin:
        if not re.match(GSTIN_REGEX, gstin):
            raise ScanError(f"{gstin} isn't a valid 15-character GSTIN.")
        existing = Supplier.query.filter_by(tenant_id=tenant.id, gstin=gstin).first()
        if existing is not None:
            return existing
    state = (gstin[:2] if gstin else (form.get("supplier_state") or "")).strip()
    supplier = Supplier(tenant_id=tenant.id, name=name, gstin=gstin, state_code=state or None,
                        state_name=STATE_NAME_BY_CODE.get(state), address_line1=(form.get("supplier_address") or "")
                        .strip()[:255] or None, phone=(form.get("supplier_phone") or "").strip()[:20] or None)
    db.session.add(supplier)
    db.session.flush()
    return supplier


def _attachment(doc: ScanDocument) -> int | None:
    from app.models.media import UploadedImage

    if len(doc.file_data or b"") <= ATTACHMENT_MAX_BYTES:
        data, kind = doc.file_data, doc.content_type
    elif doc.preview:
        data, kind = doc.preview, JPEG
    else:
        return None
    image = UploadedImage(content_type=kind, data=data, is_private=True)
    db.session.add(image)
    db.session.flush()
    return image.id


def _expected_total(doc: ScanDocument, form) -> Decimal:
    raw = (form.get("bill_total") or "").strip()
    return _money(raw) if raw else _money((doc.extracted or {}).get("invoice_value"))


def _finish(doc, user, kind, record_id, label):
    doc.status, doc.posted_type, doc.posted_id = POSTED, kind, record_id
    doc.posted_by_id, doc.posted_at = user.id, utcnow()
    record_audit(user, "scan_posted", tenant_id=doc.tenant_id, entity_type="scan_document", entity_id=doc.id,
                 details={"as": kind, "record": label})


def post_purchase(tenant, user, doc: ScanDocument, form):
    from app.inventory.ledger import default_location
    from app.purchases.services import PurchaseError, PurchaseInput, create_purchase_bill, parse_purchase_lines

    _check_open(doc)
    if not tenant.has_feature("purchases"):
        raise ScanError("Purchases is switched off - post this as an expense, or turn Purchases on in Settings.")
    gstin_id = form.get("gstin_id", type=int) or doc.gstin_id
    for name, code in zip(form.getlist("line_description[]"), form.getlist("line_hsn[]")):
        digits = re.sub(r"\D", "", code or "")
        if digits and len(digits) not in (4, 6, 8):
            raise ScanError(f"{(name or 'A line').strip()}: the HSN {digits} has {len(digits)} digits - an HSN or SAC "
                            "code has 4, 6 or 8. Check it against the bill.")
    form = _with_new_items(tenant, user, form)
    try:
        lines = parse_purchase_lines(form)
        supplier = _supplier_from_form(tenant, form)
        bill = create_purchase_bill(tenant, user, PurchaseInput(
            supplier_id=supplier.id, gstin_id=gstin_id, supplier_invoice_no=form.get("invoice_number", ""),
            supplier_invoice_date=_date(form.get("invoice_date"), "bill date"),
            location_id=form.get("location_id", type=int) or default_location(tenant.id).id, lines=lines,
            is_rcm=form.get("is_rcm") == "1", notes=(form.get("notes") or "").strip() or "Entered from a scanned bill.",
            attachment_id=_attachment(doc)))
    except PurchaseError as exc:
        raise ScanError(exc.message)
    expected = _expected_total(doc, form)
    difference = (bill.grand_total - expected).copy_abs()
    if expected > 0 and difference > Decimal("1.00") and form.get("accept_difference") != "1":
        raise ScanError(f"ARFA works this bill out to Rs. {bill.grand_total:,.2f} but the bill says "
                        f"Rs. {expected:,.2f} (a difference of Rs. {difference:,.2f}). Check the lines, or tick "
                        "\"Post anyway\".")
    _finish(doc, user, "purchase_bill", bill.id, bill.number)
    return bill


def _with_new_items(tenant, user, form):
    """Lines marked "new item" become catalogue items first, so the bill
    links to them (and receives their stock)."""
    from app.models.product import Product

    choices = form.getlist("line_product_id[]")
    if "new" not in choices:
        return form
    form = form.copy()
    get = form.getlist
    ids = []
    for i, choice in enumerate(choices):
        if choice != "new":
            ids.append(choice)
            continue
        at = lambda key: (get(key)[i] if i < len(get(key)) else "").strip()  # noqa: E731
        name = at("line_description[]")[:255]
        hsn = re.sub(r"\D", "", at("line_hsn[]"))   # never trimmed: a misread code must be fixed, not cut
        if not name:
            ids.append("")
            continue
        if len(hsn) not in (4, 6, 8):
            raise ScanError(f"{name}: an HSN code of 4, 6 or 8 digits is needed to add it as a new item.")
        try:
            rate = Decimal(at("line_gst_rate[]") or "0")
            cost = _money(at("line_rate[]"))
        except InvalidOperation:
            raise ScanError(f"{name}: check the rate and GST.")
        product = Product(tenant_id=tenant.id, name=name, hsn_or_sac_code=hsn, gst_rate=_slab(rate),
                          unit=unit_code(at("line_unit[]")), sale_price=Decimal("0"), purchase_rate=cost,
                          is_service=hsn.startswith("99"), price_includes_tax=False)
        db.session.add(product)
        db.session.flush()
        record_audit(user, "product_created_from_scan", tenant_id=tenant.id, entity_type="product",
                     entity_id=product.id, details={"name": name})
        ids.append(str(product.id))
    form.setlist("line_product_id[]", ids)
    return form


def post_expense(tenant, user, doc: ScanDocument, form):
    from app.expenses.services import ExpenseError, ExpenseInput, create_expense
    from app.models.expense import ExpensePaymentMode
    from app.models.purchase import ItcEligibility

    _check_open(doc)
    if not tenant.has_feature("expenses"):
        raise ScanError("Expenses is switched off - post this as a purchase bill, or turn Expenses on in Settings.")
    try:
        mode = ExpensePaymentMode(form.get("payment_mode") or "cash")
    except ValueError:
        raise ScanError("Pick how it was paid.")
    is_gst = form.get("is_gst_bill") == "1"
    # The supplier is matched or, when the screen says "new", added to the
    # suppliers - as for a purchase bill - so the expense sits on their account.
    supplier = _supplier_from_form(tenant, form) if form.get("supplier_id") else None
    try:
        amount = _money((form.get("amount") or "").replace(",", ""))
        rate = Decimal(form.get("gst_rate") or "0")
    except InvalidOperation:
        raise ScanError("Enter the amount as a number.")
    try:
        expense = create_expense(tenant, user, ExpenseInput(
            expense_date=_date(form.get("invoice_date"), "bill date"), category_id=form.get("category_id", type=int),
            amount=amount, payment_mode=mode, payee_name=(form.get("supplier_name") or "").strip() or None,
            supplier_id=supplier.id if supplier else None, reference=form.get("invoice_number"),
            notes=(form.get("notes") or "").strip() or "Entered from a scanned bill.", attachment_id=_attachment(doc),
            is_gst_bill=is_gst, gstin_id=form.get("gstin_id", type=int) or doc.gstin_id,
            supplier_gstin=(supplier.gstin if supplier is not None and supplier.gstin
                            else clean_gstin(form.get("supplier_gstin") or "") or None),
            bill_number=form.get("invoice_number"), gst_rate=rate, amount_includes_gst=True,
            is_rcm=form.get("is_rcm") == "1",
            itc_eligibility=ItcEligibility(form.get("itc") or "eligible") if is_gst else ItcEligibility.ELIGIBLE))
    except ExpenseError as exc:
        raise ScanError(exc.message)
    except ValueError:
        raise ScanError("Pick whether the GST can be claimed.")
    _finish(doc, user, "expense", expense.id, expense.number)
    return expense


def file_document(user, doc: ScanDocument, note: str = "") -> None:
    if doc.status not in (TO_CHECK, REJECTED, FAILED):
        raise ScanError("This document has already been dealt with.")
    doc.status = FILED
    doc.message = (note or "").strip()[:500] or doc.message
    record_audit(user, "scan_filed", tenant_id=doc.tenant_id, entity_type="scan_document", entity_id=doc.id)


def discard(user, doc: ScanDocument) -> None:
    if doc.status in (POSTED, SPLIT, DISCARDED):
        raise ScanError("This document can't be discarded.")
    doc.status = DISCARDED
    record_audit(user, "scan_discarded", tenant_id=doc.tenant_id, entity_type="scan_document", entity_id=doc.id)


def reread(user, doc: ScanDocument) -> None:
    if doc.status not in (TO_CHECK, REJECTED, FAILED):
        raise ScanError("Only a read, rejected or failed document can be read again.")
    doc.status, doc.message, doc.rejection_code = UPLOADED, None, None


def gstin_ok(value: str) -> bool:
    v = clean_gstin(value or "")
    return len(v) == 15 and gstin_checksum_ok(v)
