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
    if result.get("file_data"):
        # turned upright - the bill is kept the right way up from now on
        doc.file_data = result["file_data"]
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


def page_image(doc: ScanDocument, n: int, px: int = 2200) -> bytes:
    """Page n of the document as a JPEG, upright, the same shape the
    reader was sent - so the boxes it read land on the right spot."""
    if not doc.is_pdf:
        if n != 0:
            raise ScanError("A photo has one page.")
        with reader._RASTER:
            return reader.compress(doc.file_data, px, 85, 1600)
    import fitz

    with reader._RASTER:
        pdf = fitz.open(stream=doc.file_data, filetype="pdf")
        try:
            if not 0 <= n < len(pdf):
                raise ScanError("No such page.")
            pg = pdf[n]
            scale = px / max(pg.rect.width, pg.rect.height)
            pix = pg.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            return pix.tobytes("jpeg", jpg_quality=85)
        finally:
            pdf.close()


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


def line_discount(gross: Decimal, taxable: Decimal, printed_percent=None) -> tuple[str, Decimal]:
    """How a line's discount is taken so that it lands on the printed
    taxable value to the paisa: ("pct", the percentage) when a percentage
    does that - the one printed, or a round one - else ("amt", the rupees
    off). A percentage rounded to two places would otherwise leave the
    bill a few rupees out."""
    off = (gross - taxable).quantize(Decimal("0.01"), ROUND_HALF_UP)
    if off <= 0:
        return "pct", Decimal("0")
    tries = [_money(printed_percent)] if _money(printed_percent) > 0 else []
    tries.append(((off / gross) * 100).quantize(Decimal("0.01"), ROUND_HALF_UP))
    for pct in tries:
        if Decimal("0") < pct < Decimal("100") and \
                (gross - (gross * pct / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)).quantize(
                    Decimal("0.01"), ROUND_HALF_UP) == taxable:
            return "pct", pct
    return "amt", off


def line_rows(doc: ScanDocument) -> list[dict]:
    """The bill's lines as ARFA purchase lines: quantity x rate less a
    discount (a percentage or rupees, whichever the bill gives exactly)
    makes the taxable value printed on the bill."""
    rows = []
    for item in (doc.extracted or {}).get("line_items") or []:
        taxable = _money(item.get("taxable_value"))
        qty = Decimal(str(item.get("quantity") or 0)).quantize(Decimal("0.001"))
        rate = _money(item.get("rate_per_unit"))
        disc_type, discount = "pct", Decimal("0")
        if qty <= 0:
            qty = Decimal("1")
            rate = taxable if taxable > 0 else rate
        elif taxable > 0:
            gross = (qty * rate).quantize(Decimal("0.01"), ROUND_HALF_UP)
            if rate <= 0 or gross < taxable - Decimal("0.5"):
                rate = (taxable / qty).quantize(Decimal("0.01"), ROUND_HALF_UP)
            elif gross > taxable:
                # worked on qty x rate unrounded, exactly as the bill is computed when posted
                disc_type, discount = line_discount(qty * rate, taxable, item.get("discount_percent"))
        gst = _slab(item.get("gst_rate"))
        if gst == 0 and taxable > 0:
            tax = _money(item.get("igst")) + _money(item.get("cgst")) + _money(item.get("sgst"))
            if tax > 0:
                gst = _slab(tax / taxable * 100)
        mrp = _money(item.get("mrp"))
        rows.append({"description": (item.get("description") or "Item").strip()[:255] or "Item",
                     "hsn": re.sub(r"\D", "", str(item.get("hsn_sac") or "")),
                     "qty": f"{qty.normalize():f}", "unit": unit_code(item.get("unit") or ""),
                     "rate": f"{rate:.2f}", "discount": f"{discount:.2f}", "disc_type": disc_type,
                     "gst_rate": f"{gst.normalize():f}", "serials": "\n".join(item.get("serial_numbers") or []),
                     "item_code": (item.get("item_code") or "").strip()[:64],
                     "batch": (item.get("batch") or "").strip()[:64],
                     "mfg": iso_date(item.get("mfg_date"), month_end=False),
                     "expiry": iso_date(item.get("expiry"), month_end=True),
                     "mrp": f"{mrp:.2f}" if mrp > 0 else "", "box": item.get("box")})
    if rows:
        _spread_bill_discount(rows, doc.extracted or {})
        columns = (doc.extracted or {}).get("columns") or {}
        for row in rows:
            row["cells"] = line_cells(row.get("box"), columns)
    return rows or [{"description": "Item", "hsn": "", "qty": "1", "unit": "NOS",
                     "rate": f"{_money((doc.extracted or {}).get('taxable_value')):.2f}", "discount": "0.00",
                     "disc_type": "pct", "gst_rate": "18", "serials": "", "item_code": "", "batch": "", "mfg": "", "expiry": "",
                     "mrp": ""}]


def iso_date(text, month_end: bool = False) -> str:
    """A date as printed -> YYYY-MM-DD ("" when it can't be told). A
    month and year alone (an expiry "03/27", "MAR 2027") is that month's
    last day for an expiry, its first for a manufacturing date."""
    import calendar

    t = str(text or "").strip().upper()
    if not t:
        return ""
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2}|\d{4})", t)
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            months = {n.upper(): i for i, n in enumerate(calendar.month_abbr) if n}
            m = re.fullmatch(r"(\d{1,2}|[A-Z]{3})[A-Z]*[\s/.-]*(\d{2}|\d{4})", t)
            if not m:
                m2 = re.fullmatch(r"(\d{4})-(\d{1,2})", t)
                if not m2:
                    return ""
                y, mo = int(m2.group(1)), int(m2.group(2))
            else:
                mo = int(m.group(1)) if m.group(1).isdigit() else months.get(m.group(1), 0)
                y = int(m.group(2))
            if not 1 <= mo <= 12:
                return ""
            y = y + 2000 if y < 100 else y
            d = calendar.monthrange(y, mo)[1] if month_end else 1
    y = y + 2000 if y < 100 else y
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return ""


# What else a bill says - kept with the posted bill for warranty and
# follow-up ({key read: label shown}).
DETAIL_FIELDS = (
    ("po_number", "Your order (PO) no."), ("po_date", "PO date"), ("order_number", "Supplier's order no."),
    ("challan_number", "Challan / delivery note"), ("dispatch_doc_number", "Dispatch doc. no."),
    ("dispatched_through", "Dispatched through"), ("vehicle_number", "Vehicle no."),
    ("lr_number", "LR / docket no."), ("ewb_number", "E-way bill no."), ("irn", "IRN"),
    ("ack_number", "Ack. no."), ("ack_date", "Ack. date"), ("due_date", "Due date"),
    ("payment_terms", "Payment terms"), ("supplier_reference", "Supplier's reference"),
    ("other_references", "Other references"), ("warranty", "Warranty"), ("bank_name", "Bank"),
    ("bank_account", "Account no."), ("bank_ifsc", "IFSC"), ("bank_branch", "Branch"), ("bank_upi", "UPI ID"),
)
TDS_SECTION_CODES = ("194Q", "194C", "194J", "194H", "194I")


def detail_values(doc: ScanDocument) -> dict:
    """The bill's other details as read, for the review screen."""
    ex = doc.extracted or {}
    out = {key: str(ex.get(key) or "").strip() for key, _ in DETAIL_FIELDS}
    if _money(ex.get("discount")) > 0:
        out["discount"] = f"{_money(ex.get('discount')):.2f}"
    return out


def tds_read(doc: ScanDocument) -> tuple[str, str]:
    """(amount, section) of any TDS printed on the bill."""
    ex = doc.extracted or {}
    amount = _money(ex.get("tds"))
    section = re.sub(r"[^0-9A-Z]", "", str(ex.get("tds_section") or "").upper())
    section = next((c for c in TDS_SECTION_CODES if c in section), "OTHER" if section else "")
    return (f"{amount:.2f}" if amount > 0 else ""), (section if amount > 0 else "")


def _details_from_form(form) -> dict:
    out = {}
    for key, label in DETAIL_FIELDS:
        value = (form.get(f"detail_{key}") or "").strip()[:500]
        if value:
            out[label] = value
    discount = (form.get("detail_discount") or "").strip()
    if discount and _money(discount) > 0:
        out["Discount on the bill"] = f"Rs. {_money(discount):,.2f}"
    return out


def _tds_from_form(form) -> tuple[Decimal, str | None]:
    try:
        tds = _money((form.get("tds_amount") or "0").replace(",", "").strip() or "0")
    except InvalidOperation:
        raise ScanError("Enter the TDS as a number.")
    if tds < 0:
        raise ScanError("TDS can't be negative.")
    return tds, ((form.get("tds_section") or "").strip()[:10] or None) if tds else None


def line_cells(row_box, columns: dict) -> dict:
    """Where each of a line's values is printed: the line's row across each
    column of the items table ({column: [page, ymin, xmin, ymax, xmax]}),
    so the review can point at one cell, not the whole row. "tax" is the
    CGST and SGST columns together (or IGST)."""
    if not row_box or not columns:
        return {}
    page, y0, _, y1, _ = row_box
    # a column read on another page is the same table carried over: its width still holds
    out = {key: [page, y0, col[2], y1, col[4]] for key, col in columns.items() if col}
    taxes = [out[k] for k in ("cgst", "sgst") if k in out] or ([out["igst"]] if "igst" in out else [])
    if taxes:
        out["tax"] = [page, y0, min(t[2] for t in taxes), y1, max(t[4] for t in taxes)]
    return out


def _row_taxable(row: dict) -> tuple[Decimal, Decimal]:
    """(qty x rate, the taxable value) of a line row, as it will post."""
    gross = Decimal(row["qty"]) * Decimal(row["rate"])
    d = Decimal(row["discount"] or "0")
    off = d if row.get("disc_type") == "amt" else (gross * d / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return gross, (gross - off).quantize(Decimal("0.01"), ROUND_HALF_UP)


def _spread_bill_discount(rows: list[dict], ex: dict) -> None:
    """A discount printed once at the bottom of the bill, before the tax
    ("Less: discount Rs. 500"), goes onto the lines in proportion to their
    value, in rupees - so each line's GST is on what was really charged and
    the bill comes out exactly as printed."""
    discount, printed = _money(ex.get("discount")), _money(ex.get("taxable_value"))
    if discount <= 0 or printed <= 0:
        return
    values = [_row_taxable(r) for r in rows]
    total = sum((t for _, t in values), Decimal("0"))
    if total <= 0 or abs((total - printed) - discount) > Decimal("1.00"):
        return   # the lines already carry it, or it isn't a discount on them
    off, given = total - printed, Decimal("0")
    for i, (row, (gross, taxable)) in enumerate(zip(rows, values)):
        last = i == len(rows) - 1
        share = off - given if last else (off * taxable / total).quantize(Decimal("0.01"), ROUND_HALF_UP)
        given += share
        row["disc_type"] = "amt"
        row["discount"] = f"{(gross - taxable + share).quantize(Decimal('0.01'), ROUND_HALF_UP):.2f}"


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


_LEGAL_WORDS = {"ms", "messrs", "the", "pvt", "private", "ltd", "limited", "llp", "co", "company",
                "corpn", "inc", "and"}
# A word a name is often written with or without.
_OPTIONAL_WORDS = {"sri", "shri", "shree", "sree", "sre", "new", "m", "s"}


def norm_name(name: str) -> str:
    """A business name with its legal form, punctuation and spacing taken
    out: "M/s. A.B.C. Traders Pvt. Ltd." and "ABC TRADERS" agree."""
    text = re.sub(r"^\s*m\s*/\s*s\b\.?", " ", (name or "").lower())
    text = re.sub(r"[^a-z0-9 ]", " ", text.replace("&", " and "))
    words = [w for w in text.split() if w not in _LEGAL_WORDS]
    # Initials written apart ("A B C") join up like "ABC".
    out, run = [], ""
    for w in words:
        if len(w) == 1:
            run += w
            continue
        if run:
            out.append(run)
            run = ""
        out.append(w)
    if run:
        out.append(run)
    return " ".join(out)


def names_alike(a: str, b: str) -> bool:
    """The same business written differently: equal once tidied, one the
    other with a word more ("Sri Balaji Traders" / "Balaji Traders"), or a
    near-identical spelling. Stricter than reader.name_matches - this one
    decides that two names are one supplier."""
    from difflib import SequenceMatcher

    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return False
    if na == nb or na.replace(" ", "") == nb.replace(" ", ""):
        return True
    ta, tb = set(na.split()), set(nb.split())
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if small and small <= big and len(big) - len(small) == 1 and \
            ((big - small) <= _OPTIONAL_WORDS or len(small) >= 3):
        return True
    # The name with a place or tagline after it on the letterhead:
    # "Balaji Autos" / "Balaji Autos (Kavangarai)".
    wa, wb = na.split(), nb.split()
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    if len(short) >= 2 and long_[:len(short)] == short:
        return True
    # A misread or plural: every word the same but one, and that one
    # nearly so ("Spares" / "Spare", "Enterprlses" / "Enterprises").
    if len(wa) != len(wb) or len(wa) < 2:
        return False
    differ = [(x, y) for x, y in zip(wa, wb) if x != y]
    return len(differ) == 1 and min(len(differ[0][0]), len(differ[0][1])) >= 4 and \
        SequenceMatcher(None, *differ[0]).ratio() >= 0.9


def find_supplier(tenant, name: str, gstin: str):
    """The supplier already in the books this bill is from, or None:
      1. the same GSTIN;
      2. a GSTIN off by a character or two (a misread) - when the one read
         fails its check digit or the names agree;
      3. the same name, written alike, when the GSTINs don't conflict
         (one of them blank - an unregistered supplier, or one entered
         before its GSTIN was known).
    Two different valid GSTINs are two registrations, never merged."""
    from app.models.purchase import Supplier

    gstin = clean_gstin(gstin or "")
    gstin = gstin if len(gstin) == 15 else ""
    suppliers = (Supplier.query.filter_by(tenant_id=tenant.id)
                 .order_by(Supplier.is_active.desc(), Supplier.id).all())
    if gstin:
        exact = next((s for s in suppliers if clean_gstin(s.gstin or "") == gstin), None)
        if exact is not None:
            return exact
        valid = gstin_checksum_ok(gstin)
        near = [s for s in suppliers if s.gstin and reader.gstin_near(s.gstin, gstin, 2)
                and (not valid or names_alike(s.name, name))]
        if near:
            return near[0]
    if not (name or "").strip():
        return None
    for s in suppliers:
        if names_alike(s.name, name) and (not gstin or not s.gstin):
            return s
    return None


def _fill_in(supplier, gstin: str, address: str, phone: str, terms_days: int | None) -> None:
    """What the bill tells us that the supplier's record was missing."""
    from app.models.purchase import Supplier

    gstin = clean_gstin(gstin or "")
    if not supplier.gstin and len(gstin) == 15 and re.match(GSTIN_REGEX, gstin) and gstin_checksum_ok(gstin) \
            and Supplier.query.filter_by(tenant_id=supplier.tenant_id, gstin=gstin).first() is None:
        supplier.gstin = gstin
        supplier.state_code = gstin[:2]
        supplier.state_name = STATE_NAME_BY_CODE.get(gstin[:2])
    if not supplier.address_line1 and address:
        supplier.address_line1 = address[:255]
    if not supplier.phone and phone:
        supplier.phone = phone[:20]
    if supplier.payment_terms_days is None and terms_days:
        supplier.payment_terms_days = terms_days
    if not supplier.is_active:
        supplier.is_active = True


def supplier_choices(tenant, doc: ScanDocument) -> tuple[object, list]:
    """(the supplier this bill is from, if known, [suppliers it could be]):
    find_supplier's match first, then other similar names to pick from."""
    from app.models.purchase import Supplier

    ex = doc.extracted or {}
    gstin = clean_gstin(ex.get("supplier_gstin") or "")
    name = (ex.get("supplier_name") or "").strip()
    match = find_supplier(tenant, name, gstin)
    suppliers = Supplier.query.filter_by(tenant_id=tenant.id, is_active=True).order_by(Supplier.name).all()
    similar = [s for s in suppliers if name and s is not match
               and (names_alike(s.name, name) or reader.name_matches(s.name, name))]
    candidates = ([match] if match is not None else []) + similar
    return match, candidates[:6]


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


def _terms_days(text: str) -> int | None:
    m = re.search(r"(\d{1,3})\s*days?", text or "", re.I)
    return int(m.group(1)) if m and 0 < int(m.group(1)) <= 365 else None


def _supplier_from_form(tenant, form):
    """The supplier picked on the screen - or, for "new", the one already
    in the books under a matching GSTIN or name (never a second copy), or
    else a new one."""
    from app.models.purchase import Supplier

    address = (form.get("supplier_address") or "").strip()[:255]
    phone = (form.get("supplier_phone") or "").strip()[:20]
    terms = _terms_days(form.get("detail_payment_terms") or "")
    sid = form.get("supplier_id", type=int)
    if sid:
        supplier = Supplier.query.filter_by(id=sid, tenant_id=tenant.id).first()
        if supplier is None:
            raise ScanError("Pick a valid supplier.")
        _fill_in(supplier, form.get("supplier_gstin") or "", address, phone, terms)
        return supplier
    name = (form.get("supplier_name") or "").strip()[:255]
    gstin = clean_gstin(form.get("supplier_gstin") or "") or None
    if not name:
        raise ScanError("Enter the supplier's name.")
    if gstin and not re.match(GSTIN_REGEX, gstin):
        raise ScanError(f"{gstin} isn't a valid 15-character GSTIN.")
    existing = find_supplier(tenant, name, gstin or "")
    if existing is not None:
        _fill_in(existing, gstin or "", address, phone, terms)
        return existing
    state = (gstin[:2] if gstin else (form.get("supplier_state") or "")).strip()
    supplier = Supplier(tenant_id=tenant.id, name=name, gstin=gstin, state_code=state or None,
                        state_name=STATE_NAME_BY_CODE.get(state), address_line1=address or None,
                        phone=phone or None, payment_terms_days=terms)
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


def total_difference(total: Decimal, tds: Decimal, printed: Decimal) -> Decimal:
    """How far ARFA's total is from the one printed - which is either the
    invoice total or, on a bill that prints "Less TDS", the net after it."""
    gross = (Decimal(total) - printed).copy_abs()
    return min(gross, (Decimal(total) - tds - printed).copy_abs()) if tds else gross


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
    tds, tds_section = _tds_from_form(form)
    form = _with_new_items(tenant, user, form)
    try:
        lines = parse_purchase_lines(form)
        supplier = _supplier_from_form(tenant, form)
        bill = create_purchase_bill(tenant, user, PurchaseInput(
            supplier_id=supplier.id, gstin_id=gstin_id, supplier_invoice_no=form.get("invoice_number", ""),
            supplier_invoice_date=_date(form.get("invoice_date"), "bill date"),
            location_id=form.get("location_id", type=int) or default_location(tenant.id).id, lines=lines,
            is_rcm=form.get("is_rcm") == "1", notes=(form.get("notes") or "").strip() or "Entered from a scanned bill.",
            attachment_id=_attachment(doc), tds_amount=tds, tds_section=tds_section,
            details=_details_from_form(form)))
    except PurchaseError as exc:
        raise ScanError(exc.message)
    expected = _expected_total(doc, form)
    difference = total_difference(bill.grand_total, tds, expected)
    if expected > 0 and difference > Decimal("1.00") and form.get("accept_difference") != "1":
        net = f" (Rs. {bill.grand_total - tds:,.2f} after the TDS)" if tds else ""
        raise ScanError(f"ARFA works this bill out to Rs. {bill.grand_total:,.2f}{net} but the bill says "
                        f"Rs. {expected:,.2f} (a difference of Rs. {difference:,.2f}). Check the lines and the TDS, "
                        "or tick \"Post anyway\".")
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
        serials = [x for x in re.split(r"[,\n]+", at("line_serials[]")) if x.strip()]
        try:
            qty = Decimal(at("line_qty[]") or "0")
        except InvalidOperation:
            qty = Decimal("0")
        goods = not hsn.startswith("99")
        product = Product(tenant_id=tenant.id, name=name, hsn_or_sac_code=hsn, gst_rate=_slab(rate),
                          unit=unit_code(at("line_unit[]")), sale_price=Decimal("0"), purchase_rate=cost,
                          is_service=not goods, price_includes_tax=False,
                          # A bill printing a serial per unit, or a batch: the new item tracks them.
                          track_serial=goods and tenant.has_feature("serial_imei") and qty > 0
                          and len(serials) == qty,
                          track_batch=goods and tenant.has_feature("batch_expiry") and bool(at("line_batch[]")))
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
    tds, tds_section = _tds_from_form(form)
    extra = {**_details_from_form(form)}
    if tds:
        extra[f"TDS deducted{' u/s ' + tds_section if tds_section and tds_section != 'OTHER' else ''}"] = \
            f"Rs. {tds:,.2f} (paid Rs. {amount - tds:,.2f})"
    get = form.getlist
    for i, name in enumerate(get("line_description[]")):
        at = lambda key: (get(key)[i] if i < len(get(key)) else "").strip()  # noqa: E731
        bits = [f"code {at('line_item_code[]')}" if at("line_item_code[]") else "",
                f"batch {at('line_batch[]')}" if at("line_batch[]") else "",
                "serial nos. " + ", ".join(x.strip() for x in re.split(r"[,\n]+", at("line_serials[]")) if x.strip())
                if at("line_serials[]") else ""]
        bits = [b for b in bits if b]
        if bits and (name or "").strip():
            extra[name.strip()[:80]] = "; ".join(bits)
    notes = (form.get("notes") or "").strip() or "Entered from a scanned bill."
    if extra:
        notes += "\n" + "\n".join(f"{k}: {v}" for k, v in extra.items())
    try:
        expense = create_expense(tenant, user, ExpenseInput(
            expense_date=_date(form.get("invoice_date"), "bill date"), category_id=form.get("category_id", type=int),
            amount=amount, payment_mode=mode, payee_name=(form.get("supplier_name") or "").strip() or None,
            supplier_id=supplier.id if supplier else None, reference=form.get("invoice_number"),
            notes=notes, attachment_id=_attachment(doc),
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


def turn(user, doc: ScanDocument, deg: int) -> None:
    """Turns the bill (every page) clockwise - for a picture read the wrong
    way up, or one read before bills were turned by themselves. The
    places the values were read from turn with it."""
    if doc.status not in (TO_CHECK, REJECTED, FAILED, UPLOADED):
        raise ScanError("A bill already posted or filed isn't turned.")
    deg = int(deg) % 360
    if deg not in (90, 180, 270):
        raise ScanError("Turn it by a quarter, a half or three quarters.")
    with reader._RASTER:
        if doc.is_pdf:
            turns = {i: deg for i in range(doc.page_count or reader.pdf_page_count(doc.file_data))}
            doc.file_data = reader.turn_pdf(doc.file_data, turns)
        else:
            turns = {0: deg}
            doc.file_data = reader.turn_image(doc.file_data, deg)
    if doc.extracted:
        ex = dict(doc.extracted)
        ex["line_items"] = [dict(i) for i in ex.get("line_items") or []]
        reader.turn_boxes(ex, turns)
        ex["turned"] = {str(p): (int((ex.get("turned") or {}).get(str(p), 0)) + t) % 360 for p, t in turns.items()}
        doc.extracted = ex
    if doc.preview or not doc.is_pdf:
        doc.preview = reader.preview_jpeg(page_image(doc, 0))
    record_audit(user, "scan_turned", tenant_id=doc.tenant_id, entity_type="scan_document", entity_id=doc.id,
                 details={"degrees": deg})


def reread(user, doc: ScanDocument) -> None:
    if doc.status not in (TO_CHECK, REJECTED, FAILED):
        raise ScanError("Only a read, rejected or failed document can be read again.")
    doc.status, doc.message, doc.rejection_code = UPLOADED, None, None


def gstin_ok(value: str) -> bool:
    v = clean_gstin(value or "")
    return len(v) == 15 and gstin_checksum_ok(v)
