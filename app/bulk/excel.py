"""Workbook plumbing for bulk import/export - openpyxl only, no JS.

A template, an export and an upload all share one column layout per
ImportSpec, so "export, fix in Excel, import back" round-trips: import
updates the records it recognizes and creates the rest.

An upload may be an Excel workbook (.xlsx) or a CSV file - the same
headings, one row per record. A CSV saved by Excel on Windows is often not
UTF-8 (cp1252) and may use ; or tab between columns; both are handled.
"""

import csv
import io
import re
from decimal import Decimal
from io import BytesIO

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.bulk.specs import ImportContext, ImportSpec, RowResult

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000
# How far down the template pre-formats code columns as text.
_TEXT_FORMATTED_ROWS = 1000

_HEADER_FILL = PatternFill("solid", fgColor="1F7A4D")
_HEADER_FONT = Font(bold=True, color="FFFFFF")


class WorkbookError(ValueError):
    pass


def _write_header(ws, spec: ImportSpec) -> None:
    for i, col in enumerate(spec.columns, start=1):
        cell = ws.cell(row=1, column=i, value=col.header + (" *" if col.required else ""))
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(vertical="center")
        letter = get_column_letter(i)
        ws.column_dimensions[letter].width = max(14, len(col.header) + 4)
        if col.as_text:
            # Pre-format the whole column as text so a typed 0401 keeps
            # its leading zero and a long barcode isn't turned into 8E+12.
            for row in range(2, _TEXT_FORMATTED_ROWS + 2):
                ws.cell(row=row, column=i).number_format = "@"
    ws.freeze_panes = "A2"


def _write_help_sheet(wb: Workbook, spec: ImportSpec) -> None:
    ws = wb.create_sheet("How to fill")
    ws.append(["Column", "Required", "What to enter", "Example"])
    for cell in ws[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
    for col in spec.columns:
        ws.append([col.header, "Yes" if col.required else "", col.help, col.example])
    ws.append([])
    ws.append(["Fill one row per record on the first sheet. Don't rename or reorder the headings."])
    ws.append(["Uploading a row that matches an existing record updates it; anything else is added."])
    for letter, width in (("A", 24), ("B", 10), ("C", 70), ("D", 22)):
        ws.column_dimensions[letter].width = width


def build_template(spec: ImportSpec) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = spec.sheet_title
    _write_header(ws, spec)
    _write_help_sheet(wb, spec)
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def build_csv_template(spec: ImportSpec) -> BytesIO:
    """The headings as a CSV, UTF-8 with a byte-order mark so Excel opens
    it with the right characters (and keeps the ₹ sign)."""
    text = io.StringIO()
    csv.writer(text).writerow([col.header + (" *" if col.required else "") for col in spec.columns])
    return BytesIO(("\ufeff" + text.getvalue()).encode("utf-8"))


def build_export(spec: ImportSpec, tenant) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = spec.sheet_title
    _write_header(ws, spec)
    for r, record in enumerate(spec.export_rows(tenant), start=2):
        for c, col in enumerate(spec.columns, start=1):
            value = record.get(col.key)
            if isinstance(value, Decimal):
                value = float(value) if not col.as_text else str(value)
            ws.cell(row=r, column=c, value=value)
    _write_help_sheet(wb, spec)
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _normalize(header) -> str:
    return " ".join(str(header or "").replace("*", "").split()).strip().lower()


def _xlsx_rows(data: bytes):
    try:
        wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise WorkbookError("That Excel file can't be read. Download the template and use that.")
    return wb, wb.worksheets[0].iter_rows(values_only=True)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _csv_rows(data: bytes):
    text = _decode(data)
    if "\x00" in text:
        raise WorkbookError("That isn't a CSV or Excel (.xlsx) file this app can read. Download the template and use that.")
    first = text.split("\n", 1)[0]
    # The separator the file uses: Excel writes ; or tab in some regions.
    delimiter = max((",", ";", "\t"), key=first.count) if first else ","
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)

    def cleaned():
        for values in reader:
            yield tuple((v.strip() or None) for v in values)

    return cleaned()


def _kind_of(data: bytes, filename: str | None) -> str:
    name = (filename or "").lower()
    if data[:4] == b"PK\x03\x04":
        return "xlsx"
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" or name.endswith(".xls"):
        raise WorkbookError("That's an old Excel (.xls) file. In Excel use File > Save As > Excel Workbook (.xlsx) "
                            "or CSV, then upload that.")
    if name.endswith(".xlsx"):
        raise WorkbookError("That Excel file can't be read. Download the template and use that.")
    return "csv"


def read_rows(spec: ImportSpec, data: bytes, filename: str | None = None) -> list[tuple[int, dict]]:
    """[(sheet row number, {column key: raw cell value})] for every
    non-blank row of the first sheet (or of the CSV)."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise WorkbookError("That file is too big - 2 MB at most. Split it into smaller files.")
    wb = None
    if _kind_of(data, filename) == "xlsx":
        wb, rows = _xlsx_rows(data)
    else:
        rows = _csv_rows(data)
    try:
        header = next(rows)
    except StopIteration:
        raise WorkbookError("The file is empty.")

    by_header = {}
    for col in spec.columns:
        by_header[_normalize(col.header)] = col.key
        by_header[_normalize(col.key)] = col.key
    positions = {}
    for idx, title in enumerate(header):
        key = by_header.get(_normalize(title))
        if key and key not in positions:
            positions[key] = idx
    missing = [col.header for col in spec.columns if col.required and col.key not in positions]
    if missing:
        raise WorkbookError(
            "These columns are missing from the first row: " + ", ".join(missing) + ". Download the template and use that."
        )

    out = []
    for offset, values in enumerate(rows, start=2):
        if values is None or all(v is None or (isinstance(v, str) and not v.strip()) for v in values):
            continue
        raw = {key: (values[idx] if idx < len(values) else None) for key, idx in positions.items()}
        out.append((offset, raw))
        if len(out) > MAX_ROWS:
            raise WorkbookError(f"More than {MAX_ROWS} rows - split the file into smaller ones.")
    if wb is not None:
        wb.close()
    return out


# 8.90123E+12: what Excel writes into a CSV for a long number (a barcode)
# in a column not formatted as text - the digits are lost.
_SHORTENED = re.compile(r"^\d+(?:\.\d+)?[eE][+]?\d+$")


def preview(spec: ImportSpec, tenant, data: bytes, filename: str | None = None
            ) -> tuple[list[RowResult], ImportContext]:
    ctx = spec.make_context(tenant)
    text_columns = [col for col in spec.columns if col.as_text]
    results = []
    for row_number, raw in read_rows(spec, data, filename):
        row = RowResult(row_number=row_number, values={})
        spec.parse_row(raw, ctx, row)
        for col in text_columns:
            value = raw.get(col.key)
            if isinstance(value, str) and _SHORTENED.match(value.strip()):
                row.errors.append(f"{col.header}: '{value}' - Excel shortened this number and its digits are lost. "
                                  "Format the column as Text before typing, or use the Excel template.")
        results.append(row)
    return results, ctx
