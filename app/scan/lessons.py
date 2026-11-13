"""Teach ARFA: what the bill reader learns from a business's corrections,
and what the owner (or the Ultra Admin, for every business) tells it.

  * A correction is saved when a scanned bill is posted with rows the
    reader had missed (record_correction): the rows that were added, the
    supplier, the invoice. The next bills from that supplier are read with
    them in mind ("last time you missed 'Freight charges', HSN 9965 ...").
  * A note is written on the Bill reader training page (/scan/teach): about
    one supplier's bills (by GSTIN) or about every bill.
  * A heading is taught there too: "on this supplier's bills, 'Cust. PO'
    means your order (PO) number". The reader is told, and after it has
    read, the same rule is applied to what it returned (reader.apply_headings)
    so a taught heading isn't left to chance.

None of this changes the AI model itself; it is memory the reader is handed
with each bill (Context.lessons -> reader.extract_fields). Each business's
lessons are its own; the Ultra Admin's (tenant_id empty) reach every one.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import or_

from app.extensions import db
from app.models.mixins import utcnow
from app.models.scan import LESSON_CORRECTION, LESSON_HEADING, LESSON_NOTE, POSTED, ScanDocument, ScanLesson

CORRECTIONS_SHOWN = 3       # a supplier's latest corrections given to the reader
ROWS_PER_CORRECTION = 8
NOTE_MAX = 600
HEADING_MAX = 60
# What a heading can be taught to mean: the reader's field -> what people call
# it. "col:" fields are columns of the items table.
HEADING_FIELDS = {
    "invoice_number": "Invoice number", "invoice_date": "Invoice date",
    "po_number": "Your order (PO) number", "po_date": "Your order (PO) date",
    "order_number": "Supplier's own order number", "challan_number": "Challan / delivery note number",
    "supplier_reference": "Supplier's reference number", "due_date": "Due date", "payment_terms": "Payment terms",
    "ewb_number": "e-Way bill number", "lr_number": "LR / docket number", "vehicle_number": "Vehicle number",
    "taxable_value": "Taxable value (the bill's total before tax)", "invoice_value": "Bill total",
    "discount": "Discount on the whole bill", "round_off": "Round off", "tds": "TDS deducted",
    "col:description": "Items: name", "col:item_code": "Items: supplier's item code", "col:hsn_sac": "Items: HSN / SAC",
    "col:quantity": "Items: quantity", "col:unit": "Items: unit", "col:rate_per_unit": "Items: rate",
    "col:discount_percent": "Items: discount %", "col:discount_amount": "Items: discount in rupees",
    "col:taxable_value": "Items: taxable value of the row", "col:gst_rate": "Items: GST %",
    "col:amount": "Items: row amount with tax", "col:mrp": "Items: MRP", "col:batch": "Items: batch number",
    "col:expiry": "Items: expiry date",
}
GSTIN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")


class LessonError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _num(value) -> Decimal:
    try:
        return Decimal(str(value or "0").replace(",", "").strip() or "0")
    except InvalidOperation:
        return Decimal("0")


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 1}


# --- what was read, what was posted ------------------------------------------------------------

def posted_rows(form) -> list[dict]:
    """The rows on the posting form, with each row's value before tax."""
    get = form.getlist
    out = []
    for i, description in enumerate(get("line_description[]")):
        description = (description or "").strip()
        if not description:
            continue
        at = lambda key: (get(key)[i] if i < len(get(key)) else "").strip()  # noqa: E731
        qty, rate = _num(at("line_qty[]")), _num(at("line_rate[]"))
        value = qty * rate * (1 - _num(at("line_discount[]")) / 100) - _num(at("line_discount_flat[]"))
        out.append({"description": description[:120], "hsn": at("line_hsn[]")[:12], "qty": float(qty),
                    "rate": float(rate), "value": float(round(value, 2))})
    return out


def read_rows(doc: ScanDocument) -> list[dict]:
    return [{"description": str(li.get("description") or "")[:120], "hsn": str(li.get("hsn_sac") or "")[:12],
             "value": float(li.get("taxable_value") or 0)}
            for li in (doc.extracted or {}).get("line_items") or []]


def missed_rows(read: list[dict], posted: list[dict]) -> list[dict]:
    """The posted rows the reader had no row for: matched one to one, by
    their words or by their value."""
    left = list(read)
    missed = []
    for row in posted:
        words = _words(row["description"])
        match = None
        for cand in left:
            same_words = words and _words(cand["description"]) and \
                len(words & _words(cand["description"])) / max(1, min(len(words), len(_words(cand["description"])))) >= 0.6
            same_value = row["value"] and abs(cand["value"] - row["value"]) <= 1
            if same_words or same_value:
                match = cand
                break
        if match is None:
            missed.append(row)
        else:
            left.remove(match)
    return missed


def record_correction(tenant, user, doc: ScanDocument, form, gstin: str, name: str,
                      read: list[dict] | None = None) -> ScanLesson | None:
    """After a scanned bill is posted: the rows the reader missed, kept as a
    lesson for this supplier's next bills. Nothing when nothing was missed.
    `read`: the reader's rows as they were before posting."""
    if not gstin or not doc.extracted:
        return None
    read = read_rows(doc) if read is None else read
    posted = posted_rows(form)
    if not posted:
        return None
    missed = missed_rows(read, posted)
    if not missed:
        return None
    lesson = ScanLesson(
        tenant_id=tenant.id, kind=LESSON_CORRECTION, supplier_gstin=gstin[:15], supplier_name=(name or "")[:255],
        source_doc_id=doc.id, created_by_id=user.id,
        example={"invoice_number": (doc.extracted or {}).get("invoice_number") or "",
                 "rows": missed[:ROWS_PER_CORRECTION], "read_rows": len(read), "posted_rows": len(posted),
                 "read_total": round(sum(r["value"] for r in read), 2),
                 "posted_total": round(sum(r["value"] for r in posted), 2)},
    )
    db.session.add(lesson)
    return lesson


# --- what the reader is handed --------------------------------------------------------------------

def _row_text(row: dict) -> str:
    bits = [f'"{row["description"]}"']
    if row.get("hsn"):
        bits.append(f"HSN {row['hsn']}")
    if row.get("qty") and row.get("rate"):
        bits.append(f"{row['qty']:g} x {row['rate']:,.2f}")
    bits.append(f"value {row['value']:,.2f}")
    return " ".join(bits)


def norm_heading(text) -> str:
    """"Cust. PO", "CUST PO :" and "cust.po" are one heading."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _heading_line(lesson: ScanLesson, where: str) -> str:
    field = (lesson.example or {}).get("field") or ""
    label = HEADING_FIELDS.get(field, field)
    heading = (lesson.text or "").strip()
    if field.startswith("col:"):
        name = field[4:]
        return (f'- On {where}, the items-table column headed "{heading}" is {name} ({label}): read each row\'s '
                f"value in that column into {name}.")
    return (f'- On {where}, the heading or label "{heading}" means {field} ({label}): read the value printed under '
            f"or beside it into {field}.")


HEADINGS_ASK = ('- For the headings taught above, also return "headings":{"<field>":"<its heading or label exactly as '
                'printed>"} for each top-level field you filled from a labelled value, and "column_headings":'
                '{"<column>":"<the column heading exactly as printed>"} for each column of the items table.')


def active(tenant) -> list[ScanLesson]:
    """This business's lessons and the Ultra Admin's, newest first."""
    return (ScanLesson.query.filter(ScanLesson.active.is_(True),
                                    or_(ScanLesson.tenant_id == tenant.id, ScanLesson.tenant_id.is_(None)))
            .order_by(ScanLesson.created_at.desc(), ScanLesson.id.desc()).limit(500).all())


def for_reading(tenant) -> tuple[dict[str, str], dict[str, list[int]]]:
    """({GSTIN or "": the text the reader gets}, {same key: the lesson ids})."""
    grouped: dict[str, list[ScanLesson]] = defaultdict(list)
    for lesson in active(tenant):
        grouped[lesson.supplier_gstin or ""].append(lesson)
    texts, ids = {}, {}
    for key, lessons in grouped.items():
        lines = []
        notes = [l for l in lessons if l.kind == LESSON_NOTE and (l.text or "").strip()]
        corrections = [l for l in lessons if l.kind == LESSON_CORRECTION and (l.example or {}).get("rows")]
        corrections = corrections[:CORRECTIONS_SHOWN]
        headings = [l for l in lessons if l.kind == LESSON_HEADING and (l.text or "").strip()
                    and (l.example or {}).get("field") in HEADING_FIELDS]
        where = "every bill" if not key else "bills from this supplier"
        for note in notes:
            lines.append(f"- About {where}: {note.text.strip()[:NOTE_MAX]}")
        for heading in headings:
            lines.append(_heading_line(heading, where))
        if headings:
            lines.append(HEADINGS_ASK)
        for c in corrections:
            ex = c.example or {}
            rows = "; ".join(_row_text(r) for r in ex.get("rows", [])[:ROWS_PER_CORRECTION])
            invoice = f" (invoice {ex['invoice_number']})" if ex.get("invoice_number") else ""
            lines.append(f"- On an earlier bill from this supplier{invoice} you missed these rows, which the "
                         f"business had to add: {rows}. Look for rows like these on this bill and include them "
                         f"when it shows them.")
        if lines:
            texts[key] = "\n".join(lines)
            ids[key] = [l.id for l in notes + headings + corrections]
    return texts, ids


def heading_rules(tenant) -> dict[str, list[tuple[str, str]]]:
    """{GSTIN or "": [(the heading as printed, the field it means)]} - the
    same lessons as rules, applied to what the reader returned. A supplier's
    own lesson about a heading comes before every-supplier's and ARFA's."""
    rules: dict[str, list[tuple[str, str]]] = defaultdict(list)
    lessons = [l for l in active(tenant) if l.kind == LESSON_HEADING and (l.text or "").strip()
               and (l.example or {}).get("field") in HEADING_FIELDS]
    for lesson in sorted(lessons, key=lambda l: l.tenant_id is None):   # the business's own first
        rules[lesson.supplier_gstin or ""].append((lesson.text.strip(), lesson.example["field"]))
    return dict(rules)


def mark_used(ids: dict[str, list[int]], keys) -> None:
    lesson_ids = [i for k in keys or [] for i in ids.get(k, [])]
    if lesson_ids:
        ScanLesson.query.filter(ScanLesson.id.in_(lesson_ids)).update(
            {ScanLesson.uses: ScanLesson.uses + 1, ScanLesson.last_used_at: utcnow()}, synchronize_session=False)


# --- the Teach ARFA page ---------------------------------------------------------------------------

def add_note(tenant_id: int | None, user, text: str, gstin: str = "", name: str = "") -> ScanLesson:
    text = (text or "").strip()
    gstin = (gstin or "").strip().upper()
    if not text:
        raise LessonError("Write what ARFA should know.")
    if len(text) > NOTE_MAX:
        raise LessonError(f"Keep a note to {NOTE_MAX} characters - write a second note for more.")
    if gstin and not GSTIN.match(gstin):
        raise LessonError("That GSTIN isn't 15 characters in the GSTIN format.")
    lesson = ScanLesson(tenant_id=tenant_id, kind=LESSON_NOTE, supplier_gstin=gstin or None,
                        supplier_name=(name or "").strip()[:255] or None, text=text, created_by_id=user.id)
    db.session.add(lesson)
    return lesson


def add_heading(tenant_id: int | None, user, heading: str, field: str, gstin: str = "", name: str = "",
                source_doc_id: int | None = None) -> ScanLesson:
    """A heading means one thing for a supplier: taught again, the lesson
    is changed rather than doubled."""
    heading = re.sub(r"\s+", " ", (heading or "").strip()).strip(" :-")
    gstin = (gstin or "").strip().upper()
    if not heading:
        raise LessonError("Type the heading as it is printed on the bill.")
    if len(heading) > HEADING_MAX:
        raise LessonError(f"A heading is a few words - keep it to {HEADING_MAX} characters.")
    if not norm_heading(heading):
        raise LessonError("A heading needs letters or numbers in it.")
    if field not in HEADING_FIELDS:
        raise LessonError("Pick what the heading means.")
    if gstin and not GSTIN.match(gstin):
        raise LessonError("That GSTIN isn't 15 characters in the GSTIN format.")
    same = [l for l in ScanLesson.query.filter(ScanLesson.kind == LESSON_HEADING,
                                               ScanLesson.tenant_id == tenant_id if tenant_id is not None
                                               else ScanLesson.tenant_id.is_(None),
                                               ScanLesson.supplier_gstin == gstin if gstin
                                               else ScanLesson.supplier_gstin.is_(None)).all()
            if norm_heading(l.text) == norm_heading(heading)]
    if same:
        lesson = same[0]
        lesson.text, lesson.example, lesson.active = heading, {"field": field}, True
        return lesson
    lesson = ScanLesson(tenant_id=tenant_id, kind=LESSON_HEADING, supplier_gstin=gstin or None,
                        supplier_name=(name or "").strip()[:255] or None, text=heading, example={"field": field},
                        created_by_id=user.id, source_doc_id=source_doc_id)
    db.session.add(lesson)
    return lesson


def supplier_stats(tenant, days: int = 180) -> list[dict]:
    """Per supplier, the bills posted from scans lately, how many needed
    rows added, and the lessons ARFA has - the suppliers to teach about
    first at the top."""
    since = utcnow() - timedelta(days=days)
    docs = (ScanDocument.query.filter(ScanDocument.tenant_id == tenant.id, ScanDocument.status == POSTED,
                                      ScanDocument.posted_at >= since)
            .order_by(ScanDocument.id.desc()).limit(3000).all())
    from app.scan.services import party

    stats: dict[str, dict] = {}
    for doc in docs:
        name, gstin = party(doc)
        if not gstin:
            continue
        row = stats.setdefault(gstin, {"gstin": gstin, "name": name or gstin, "bills": 0, "corrected": 0,
                                       "fixed_by_reread": 0, "lessons": 0, "notes": 0, "last": None})
        row["bills"] += 1
        if (doc.engine or {}).get("rows_pass") == "fixed":
            row["fixed_by_reread"] += 1
    lessons = ScanLesson.query.filter(ScanLesson.tenant_id == tenant.id, ScanLesson.supplier_gstin.isnot(None)).all()
    for lesson in lessons:
        row = stats.setdefault(lesson.supplier_gstin, {"gstin": lesson.supplier_gstin,
                                                       "name": lesson.supplier_name or lesson.supplier_gstin,
                                                       "bills": 0, "corrected": 0, "fixed_by_reread": 0,
                                                       "lessons": 0, "notes": 0, "last": None})
        if lesson.kind == LESSON_CORRECTION:
            row["corrected"] += 1
            if lesson.created_at and (row["last"] is None or lesson.created_at > row["last"]):
                row["last"] = lesson.created_at
        elif lesson.kind == LESSON_HEADING:
            row["headings"] = row.get("headings", 0) + 1
        else:
            row["notes"] += 1
        if lesson.active:
            row["lessons"] += 1
    for row in stats.values():
        # Bills that were posted as ARFA read them (no rows had to be added).
        row["right_first_time"] = max(row["bills"] - row["corrected"], 0)
    return sorted(stats.values(), key=lambda r: (-r["corrected"], -r["bills"], r["name"].lower()))


def recent_bills(tenant, gstin: str, limit: int = 6) -> list[ScanDocument]:
    """A supplier's latest scanned bills - the ones to train on."""
    from app.scan.services import party

    docs = (ScanDocument.query.filter(ScanDocument.tenant_id == tenant.id, ScanDocument.extracted.isnot(None))
            .order_by(ScanDocument.id.desc()).limit(400).all())
    return [d for d in docs if party(d)[1] == gstin][:limit]
