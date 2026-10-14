"""GSTR-2B: read the statement the GST portal generates (its JSON or its
Excel download), and match each supplier invoice in it against the
purchase bills the client entered.

Statuses per 2B row: MATCHED (same supplier GSTIN, same invoice number -
or one written slightly differently - and the same value within Rs. 1),
MISMATCH (same supplier and invoice, different value; or a near invoice
number with a different value), ONLY_IN_2B (no bill entered). "Only in
books" is the other side: posted bills of the month that no 2B row
matched. Reverse-charge rows are kept on their own list and only ever
matched to reverse-charge bills - never mixed into the regular register.
"""

import difflib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from openpyxl import load_workbook

from app.extensions import db
from app.models.purchase import Gstr2bEntry, Gstr2bImport, MatchStatus, PurchaseBill, PurchaseStatus

ZERO = Decimal("0")
VALUE_TOLERANCE = Decimal("1.00")
FUZZY_RATIO = 0.8
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class Gstr2bError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class Row:
    supplier_gstin: str
    supplier_name: str
    invoice_no: str
    invoice_date: date | None
    invoice_value: Decimal
    taxable_value: Decimal = ZERO
    igst: Decimal = ZERO
    cgst: Decimal = ZERO
    sgst: Decimal = ZERO
    cess: Decimal = ZERO
    is_rcm: bool = False
    itc_available: bool = True


def invoice_key(invoice_no: str) -> str:
    """'INV/0042', 'inv-42', 'INV 42', 'INV0042' all compare equal: upper case,
    letters and digits only, leading zeros of each number dropped."""
    tokens = re.findall(r"[A-Z]+|\d+", (invoice_no or "").upper())
    return "".join((t.lstrip("0") or "0") if t.isdigit() else t for t in tokens)


def _money(value) -> Decimal:
    if value in (None, ""):
        return ZERO
    try:
        return Decimal(str(value).replace(",", "").strip()).quantize(Decimal("0.01"))
    except InvalidOperation:
        return ZERO


def _date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _yes(value) -> bool:
    return str(value or "").strip().upper() in ("Y", "YES", "TRUE", "1")


# --- reading the file ---------------------------------------------------------------

def parse_json(data: bytes) -> list[Row]:
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Gstr2bError("That file isn't valid GSTR-2B JSON.")
    root = doc.get("data", doc) if isinstance(doc, dict) else {}
    docdata = root.get("docdata") if isinstance(root, dict) else None
    if not isinstance(docdata, dict):
        raise Gstr2bError("No invoice data found - is this the GSTR-2B JSON from the GST portal?")
    rows = []
    for supplier in docdata.get("b2b", []) or []:
        gstin = (supplier.get("ctin") or "").strip().upper()
        name = supplier.get("trdnm") or ""
        for inv in supplier.get("inv", []) or []:
            items = inv.get("items", []) or []
            rows.append(Row(
                supplier_gstin=gstin, supplier_name=name, invoice_no=str(inv.get("inum", "")).strip(),
                invoice_date=_date(inv.get("dt")), invoice_value=_money(inv.get("val")),
                taxable_value=sum((_money(i.get("txval")) for i in items), ZERO),
                igst=sum((_money(i.get("igst")) for i in items), ZERO),
                cgst=sum((_money(i.get("cgst")) for i in items), ZERO),
                sgst=sum((_money(i.get("sgst")) for i in items), ZERO),
                cess=sum((_money(i.get("cess")) for i in items), ZERO),
                is_rcm=_yes(inv.get("rev")), itc_available=str(inv.get("itcavl", "Y")).upper() != "N",
            ))
    return rows


_EXCEL_COLUMNS = {
    "gstin": ("gstin of supplier",),
    "name": ("trade/legal name", "trade name", "legal name"),
    "invoice_no": ("invoice number",),
    "invoice_date": ("invoice date",),
    "invoice_value": ("invoice value",),
    "rcm": ("reverse charge",),
    "taxable": ("taxable value",),
    "igst": ("integrated tax",),
    "cgst": ("central tax",),
    "sgst": ("state/ut tax", "state tax"),
    "cess": ("cess",),
    "itc": ("itc availability",),
}


def parse_excel(data: bytes) -> list[Row]:
    try:
        wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise Gstr2bError("That file isn't an Excel workbook (.xlsx).")
    sheet = next((wb[name] for name in wb.sheetnames if name.strip().upper().startswith("B2B")
                  and "CDN" not in name.upper() and "(A)" not in name.upper()), None)
    if sheet is None:
        raise Gstr2bError("No B2B sheet found - is this the GSTR-2B Excel from the GST portal?")
    rows = list(sheet.iter_rows(values_only=True))
    header_at = next((i for i, r in enumerate(rows[:15])
                      if any("gstin of supplier" in str(c or "").lower() for c in r)), None)
    if header_at is None:
        raise Gstr2bError("Couldn't find the B2B column headings (GSTIN of supplier, Invoice number...).")
    width = max(len(r) for r in rows[header_at:header_at + 2])
    labels = []
    for col in range(width):
        parts = [str(rows[r][col] or "") if col < len(rows[r]) else "" for r in (header_at, header_at + 1) if r < len(rows)]
        labels.append(" ".join(parts).lower())
    cols = {}
    for key, needles in _EXCEL_COLUMNS.items():
        for idx, label in enumerate(labels):
            if any(n in label for n in needles):
                cols.setdefault(key, idx)
                break
    for required in ("gstin", "invoice_no", "invoice_value"):
        if required not in cols:
            raise Gstr2bError("The B2B sheet is missing the GSTIN, invoice number or invoice value column.")
    start = header_at + 1
    if start < len(rows) and not re.match(r"^\d{2}[A-Z0-9]{13}$", str(rows[start][cols["gstin"]] or "").strip().upper()):
        start += 1  # a second heading row (tax-amount sub-headings)
    merged: dict[tuple, Row] = {}
    for r in rows[start:]:
        cell = lambda k: r[cols[k]] if k in cols and cols[k] < len(r) else None
        gstin = str(cell("gstin") or "").strip().upper()
        if not re.match(r"^\d{2}[A-Z0-9]{13}$", gstin):
            continue
        invoice_no = str(cell("invoice_no") or "").strip()
        key = (gstin, invoice_no)
        row = merged.get(key)
        if row is None:
            row = merged[key] = Row(
                supplier_gstin=gstin, supplier_name=str(cell("name") or ""), invoice_no=invoice_no,
                invoice_date=_date(cell("invoice_date")), invoice_value=_money(cell("invoice_value")),
                is_rcm=_yes(cell("rcm")), itc_available=str(cell("itc") or "Yes").strip().upper() not in ("NO", "N"),
            )
        # One row per tax rate on an invoice: values add up, invoice value doesn't.
        row.taxable_value += _money(cell("taxable"))
        row.igst += _money(cell("igst"))
        row.cgst += _money(cell("cgst"))
        row.sgst += _money(cell("sgst"))
        row.cess += _money(cell("cess"))
    return list(merged.values())


def parse(filename: str, data: bytes) -> tuple[str, list[Row]]:
    if len(data) > MAX_UPLOAD_BYTES:
        raise Gstr2bError("That file is too large (10 MB max).")
    name = (filename or "").lower()
    if name.endswith(".json"):
        return "json", parse_json(data)
    if name.endswith(".xlsx"):
        return "excel", parse_excel(data)
    raise Gstr2bError("Upload the GSTR-2B as .json or .xlsx.")


# --- matching -----------------------------------------------------------------------

def save_import(tenant, gstin, period: str, filename: str, source: str, rows: list[Row], user) -> Gstr2bImport:
    if not rows:
        raise Gstr2bError("No supplier invoices found in that file.")
    imp = Gstr2bImport(tenant_id=tenant.id, gstin_id=gstin.id, period=period, filename=(filename or "")[:255],
                       source=source, uploaded_by_id=user.id)
    db.session.add(imp)
    db.session.flush()
    for row in rows:
        db.session.add(Gstr2bEntry(
            tenant_id=tenant.id, import_id=imp.id, supplier_gstin=row.supplier_gstin,
            supplier_name=(row.supplier_name or "")[:255], invoice_no=row.invoice_no[:50],
            invoice_date=row.invoice_date, invoice_value=row.invoice_value, taxable_value=row.taxable_value,
            igst=row.igst, cgst=row.cgst, sgst=row.sgst, cess=row.cess, is_rcm=row.is_rcm,
            itc_available=row.itc_available,
        ))
    db.session.flush()
    auto_match(imp)
    return imp


def _candidates(imp: Gstr2bImport) -> list[PurchaseBill]:
    return PurchaseBill.query.filter(
        PurchaseBill.tenant_id == imp.tenant_id,
        PurchaseBill.gstin_id == imp.gstin_id,
        PurchaseBill.status == PurchaseStatus.POSTED,
    ).all()


def _bill_gstin(bill: PurchaseBill) -> str:
    return ((bill.supplier_snapshot or {}).get("gstin") or "").strip().upper()


def _values_agree(entry: Gstr2bEntry, bill: PurchaseBill) -> bool:
    bill_value = bill.total_taxable_value + (ZERO if bill.is_rcm else bill.total_tax) + bill.other_charges
    return (abs(entry.invoice_value - bill_value) <= VALUE_TOLERANCE
            or abs(entry.invoice_value - bill.grand_total) <= VALUE_TOLERANCE
            or abs(entry.taxable_value - bill.total_taxable_value) <= VALUE_TOLERANCE)


def auto_match(imp: Gstr2bImport) -> None:
    """Matches every not-manually-matched row of the import. Manual
    matches are left exactly as the user set them."""
    bills = _candidates(imp)
    taken = {e.matched_bill_id for e in imp.entries if e.manually_matched and e.matched_bill_id}
    for entry in imp.entries:
        if entry.manually_matched:
            continue
        entry.matched_bill_id, entry.status, entry.match_note = None, MatchStatus.ONLY_IN_2B, None
        pool = [b for b in bills if b.id not in taken and _bill_gstin(b) == entry.supplier_gstin and b.is_rcm == entry.is_rcm]
        key = invoice_key(entry.invoice_no)
        exact = [b for b in pool if invoice_key(b.supplier_invoice_no) == key]
        if exact:
            bill = next((b for b in exact if _values_agree(entry, b)), exact[0])
            _link(entry, bill, taken)
            if _values_agree(entry, bill):
                entry.status = MatchStatus.MATCHED
            else:
                entry.status = MatchStatus.MISMATCH
                entry.match_note = f"Value differs: books Rs. {bill.grand_total:.2f}, 2B Rs. {entry.invoice_value:.2f}"
            continue
        near = [(difflib.SequenceMatcher(None, key, invoice_key(b.supplier_invoice_no)).ratio(), b) for b in pool]
        near = [(r, b) for r, b in near if r >= FUZZY_RATIO]
        if near:
            near.sort(key=lambda rb: (-rb[0], not _values_agree(entry, rb[1])))
            bill = near[0][1]
            _link(entry, bill, taken)
            if _values_agree(entry, bill):
                entry.status = MatchStatus.MATCHED
                entry.match_note = f"Invoice no. written differently in books: {bill.supplier_invoice_no}"
            else:
                entry.status = MatchStatus.MISMATCH
                entry.match_note = (f"Invoice no. {bill.supplier_invoice_no} in books and value differs "
                                    f"(books Rs. {bill.grand_total:.2f}, 2B Rs. {entry.invoice_value:.2f})")
    db.session.flush()


def _link(entry, bill, taken):
    entry.matched_bill_id = bill.id
    taken.add(bill.id)


def manual_match(entry: Gstr2bEntry, bill: PurchaseBill) -> None:
    if bill.tenant_id != entry.tenant_id or bill.gstin_id != entry.gstr2b_import.gstin_id:
        raise Gstr2bError("That bill isn't from this registration.")
    if bill.status != PurchaseStatus.POSTED:
        raise Gstr2bError("That bill is voided.")
    other = Gstr2bEntry.query.filter(Gstr2bEntry.import_id == entry.import_id, Gstr2bEntry.matched_bill_id == bill.id,
                                     Gstr2bEntry.id != entry.id).first()
    if other is not None:
        other.matched_bill_id, other.status, other.manually_matched = None, MatchStatus.ONLY_IN_2B, False
        other.match_note = f"Unlinked - bill {bill.number} was matched to another row by hand"
    entry.matched_bill_id = bill.id
    entry.manually_matched = True
    entry.status = MatchStatus.MATCHED
    entry.match_note = f"Matched by hand to {bill.number}"


def unmatch(entry: Gstr2bEntry) -> None:
    entry.matched_bill_id = None
    entry.manually_matched = False
    entry.status = MatchStatus.ONLY_IN_2B
    entry.match_note = None


def only_in_books(imp: Gstr2bImport) -> list[PurchaseBill]:
    """Posted bills of the import's month (by supplier invoice date) that
    no row of this 2B matched - not yet filed by the supplier, or a typo."""
    from app.reports.gstr import month_bounds

    start, end = month_bounds(imp.period)
    matched = {e.matched_bill_id for e in imp.entries if e.matched_bill_id}
    bills = PurchaseBill.query.filter(
        PurchaseBill.tenant_id == imp.tenant_id, PurchaseBill.gstin_id == imp.gstin_id,
        PurchaseBill.status == PurchaseStatus.POSTED,
        PurchaseBill.supplier_invoice_date >= start, PurchaseBill.supplier_invoice_date <= end,
    ).order_by(PurchaseBill.supplier_invoice_date).all()
    return [b for b in bills if b.id not in matched]
