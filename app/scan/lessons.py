"""Teach ARFA: what the bill reader learns from a business's corrections,
and what the owner (or the Ultra Admin, for every business) tells it.

  * A correction is saved when a scanned bill is posted with rows the
    reader had missed (record_correction): the rows that were added, the
    supplier, the invoice. The next bills from that supplier are read with
    them in mind ("last time you missed 'Freight charges', HSN 9965 ...").
  * A note is written on the Teach ARFA page (/scan/teach): about one
    supplier's bills (by GSTIN) or about every bill.

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
from app.models.scan import LESSON_CORRECTION, LESSON_NOTE, POSTED, ScanDocument, ScanLesson

CORRECTIONS_SHOWN = 3       # a supplier's latest corrections given to the reader
ROWS_PER_CORRECTION = 8
NOTE_MAX = 600
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
        where = "every bill" if not key else "bills from this supplier"
        for note in notes:
            lines.append(f"- About {where}: {note.text.strip()[:NOTE_MAX]}")
        for c in corrections:
            ex = c.example or {}
            rows = "; ".join(_row_text(r) for r in ex.get("rows", [])[:ROWS_PER_CORRECTION])
            invoice = f" (invoice {ex['invoice_number']})" if ex.get("invoice_number") else ""
            lines.append(f"- On an earlier bill from this supplier{invoice} you missed these rows, which the "
                         f"business had to add: {rows}. Look for rows like these on this bill and include them "
                         f"when it shows them.")
        if lines:
            texts[key] = "\n".join(lines)
            ids[key] = [l.id for l in notes + corrections]
    return texts, ids


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
        else:
            row["notes"] += 1
        if lesson.active:
            row["lessons"] += 1
    return sorted(stats.values(), key=lambda r: (-r["corrected"], -r["bills"], r["name"].lower()))
