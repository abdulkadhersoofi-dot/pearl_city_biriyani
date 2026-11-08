"""EMI (finance) sales - warehouse dispatch, stage 5B - the website side:

  /warehouse/financiers                       financiers (Bajaj Finserv, HDB ...)
  /warehouse/financiers/<id>/settlement       its open finance invoices, what it paid
  /warehouse/financiers/<id>/payouts          import its payout statement (Excel),
                                              matched by loan ref / invoice / amount
  /warehouse/racks                            rack labels to print (putaway, EMI READY)

A financier's share of a financed invoice is due on its own customer account
(app/accounts/receivables.py), so its payouts are ordinary receipt vouchers
allocated against those invoices - no new ledger.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from flask import current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user
from weasyprint import HTML

from app.auth.decorators import roles_required
from app.extensions import db
from app.models.customer import Customer
from app.models.invoice import Invoice
from app.models.user import UserRole
from app.models.warehouse import EMI_READY, RACK_PREFIX, Financier
from app.utils.audit import record_audit
from app.utils.features import feature_required
from app.utils.tenant_scope import tenant_query
from app.warehouse import dispatch, warehouse_bp

ADMIN = UserRole.CLIENT_ADMIN
SUGGESTED = (("Bajaj Finserv", "BFL"), ("HDB Financial Services", "HDB"))
ZERO = Decimal("0.00")


def _audit(action, entity_type=None, entity_id=None, **details):
    record_audit(current_user, action, tenant_id=current_user.tenant_id, entity_type=entity_type,
                 entity_id=entity_id, details=details)


def active_financiers(tenant_id: int) -> list[Financier]:
    return Financier.query.filter_by(tenant_id=tenant_id, is_active=True).order_by(Financier.name).all()


def add_financier(tenant, name: str, short_code: str = "") -> Financier:
    name = (name or "").strip()[:120]
    if not name:
        raise ValueError("Give the financier's name.")
    if Financier.query.filter(Financier.tenant_id == tenant.id, db.func.lower(Financier.name) == name.lower()).first():
        raise ValueError(f"{name} is already a financier.")
    gstin = tenant.gstins.filter_by(is_primary=True).first() or tenant.gstins.first()
    customer = Customer.query.filter(Customer.tenant_id == tenant.id, db.func.lower(Customer.name) == name.lower()).first()
    if customer is None:
        # Its account: what it owes on financed invoices (a customer like any other).
        customer = Customer(tenant_id=tenant.id, name=name, state_code=gstin.state_code if gstin else None,
                            state_name=gstin.state_name if gstin else None)
        db.session.add(customer)
        db.session.flush()
    financier = Financier(tenant_id=tenant.id, name=name, short_code=(short_code or "").strip().upper()[:20] or None,
                          customer_id=customer.id)
    db.session.add(financier)
    db.session.flush()
    return financier


@warehouse_bp.route("/financiers", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("warehouse_dispatch")
def financiers():
    tenant = current_user.tenant
    if request.method == "POST":
        try:
            f = add_financier(tenant, request.form.get("name"), request.form.get("short_code", ""))
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("warehouse.financiers"))
        _audit("financier_added", "financier", f.id, name=f.name)
        db.session.commit()
        flash(f"{f.name} added - its share of each EMI invoice is due on its own account.", "success")
        return redirect(url_for("warehouse.financiers"))
    from app.accounts.receivables import customer_account

    rows = tenant_query(Financier).order_by(Financier.is_active.desc(), Financier.name).all()
    due = {f.id: customer_account(tenant, f.customer, with_entries=False).balance for f in rows}
    have = {f.name.lower() for f in rows}
    return render_template("warehouse/financiers.html", rows=rows, due=due,
                           suggestions=[s for s in SUGGESTED if s[0].lower() not in have])


@warehouse_bp.route("/financiers/<int:financier_id>/toggle", methods=["POST"])
@roles_required(ADMIN)
@feature_required("warehouse_dispatch")
def toggle_financier(financier_id):
    f = tenant_query(Financier).filter_by(id=financier_id).first_or_404()
    f.is_active = not f.is_active
    _audit("financier_" + ("activated" if f.is_active else "deactivated"), "financier", f.id, name=f.name)
    db.session.commit()
    return redirect(url_for("warehouse.financiers"))


# --- settlement -----------------------------------------------------------------------------------------

def open_items(tenant, financier: Financier) -> list[dict]:
    """The financier's financed invoices with what is still due on each."""
    from app.accounts.receivables import customer_account

    account = customer_account(tenant, financier.customer, with_entries=False)
    by_id = {i.id: i for i in account.items if i.kind == "invoice"}
    invoices = (Invoice.query.filter(Invoice.tenant_id == tenant.id, Invoice.id.in_(list(by_id) or [0]))
                .order_by(Invoice.invoice_date, Invoice.id).all())
    return [{"invoice": inv, "due": by_id[inv.id].amount, "received": by_id[inv.id].settled,
             "outstanding": by_id[inv.id].outstanding} for inv in invoices]


@warehouse_bp.route("/financiers/<int:financier_id>/settlement")
@roles_required(ADMIN)
@feature_required("warehouse_dispatch")
def settlement(financier_id):
    f = tenant_query(Financier).filter_by(id=financier_id).first_or_404()
    items = open_items(current_user.tenant, f)
    return render_template("warehouse/settlement.html", f=f, items=items, preview=None,
                           totals={k: sum((i[k] for i in items), ZERO) for k in ("due", "received", "outstanding")})


_HEADERS = {
    "loan_ref": ("loan", "agreement", "lan", "deal", "do no", "do number", "application", "ref"),
    "invoice": ("invoice", "inv no", "bill"),
    "amount": ("net", "disburs", "payout", "paid", "amount", "credit"),
    "charges": ("subvention", "processing", "charge", "deduction", "mdr", "fee"),
    "utr": ("utr", "neft", "rtgs", "transaction", "txn", "reference no"),
    "date": ("date",),
}


def _money(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("₹", "").replace("Rs.", "").strip()).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _when(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return date.today().isoformat()


def read_payouts(data: bytes) -> list[dict]:
    """Rows of a financier's payout statement (.xlsx): the header row is
    found by its words (loan / agreement no, invoice, net amount, UTR...)."""
    from openpyxl import load_workbook

    try:
        ws = load_workbook(BytesIO(data), read_only=True, data_only=True).active
    except Exception as exc:  # noqa: BLE001 - any unreadable file
        raise ValueError("That file couldn't be read - upload the statement as an Excel (.xlsx) file.") from exc
    rows = list(ws.iter_rows(values_only=True))
    for h, header in enumerate(rows[:15]):
        cells = [str(c or "").strip().lower() for c in header]
        cols = {}
        for key, words in _HEADERS.items():
            for i, cell in enumerate(cells):
                if i not in cols.values() and cell and any(w in cell for w in words):
                    cols[key] = i
                    break
        if "amount" in cols and ("loan_ref" in cols or "invoice" in cols):
            break
    else:
        raise ValueError("No header row with a loan / invoice number and an amount was found.")
    out = []
    for row in rows[h + 1:]:
        get = lambda key: row[cols[key]] if key in cols and cols[key] < len(row) else None  # noqa: E731
        amount = _money(get("amount"))
        if not amount or amount <= 0:
            continue
        out.append({"loan_ref": str(get("loan_ref") or "").strip(), "invoice": str(get("invoice") or "").strip(),
                    "amount": str(amount), "charges": str(_money(get("charges")) or ""),
                    "utr": str(get("utr") or "").strip()[:100], "date": _when(get("date"))})
    return out


def match(rows: list[dict], items: list[dict]) -> list[dict]:
    """Each payout row against the open invoices: loan ref, then invoice
    number, then an amount only one invoice has outstanding."""
    def norm(text):
        return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())

    open_ = [i for i in items if i["outstanding"] > 0]
    taken = set()
    for row in rows:
        row["invoice_id"], row["how"] = None, ""
        amount = Decimal(row["amount"])
        for item in open_:
            inv = item["invoice"]
            if inv.id in taken:
                continue
            if row["loan_ref"] and inv.loan_ref and norm(row["loan_ref"]) == norm(inv.loan_ref):
                row["invoice_id"], row["how"] = inv.id, "loan ref"
                break
            if row["invoice"] and norm(row["invoice"]) == norm(inv.invoice_number):
                row["invoice_id"], row["how"] = inv.id, "invoice no."
                break
        if row["invoice_id"] is None:
            same = [i for i in open_ if i["invoice"].id not in taken and i["outstanding"] == amount]
            if len(same) == 1:
                row["invoice_id"], row["how"] = same[0]["invoice"].id, "amount"
        if row["invoice_id"]:
            taken.add(row["invoice_id"])
    return rows


@warehouse_bp.route("/financiers/<int:financier_id>/payouts", methods=["POST"])
@roles_required(ADMIN)
@feature_required("warehouse_dispatch")
def payouts(financier_id):
    """Upload: show the matches to check. Confirm: a receipt voucher for each
    matched row, set against its invoice."""
    from app.accounts import services as accounts
    from app.models.accounts import MoneyMode

    tenant = current_user.tenant
    f = tenant_query(Financier).filter_by(id=financier_id).first_or_404()
    items = open_items(tenant, f)
    if request.form.get("step") != "confirm":
        file = request.files.get("statement")
        try:
            rows = match(read_payouts(file.read() if file else b""), items)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("warehouse.settlement", financier_id=f.id))
        return render_template("warehouse/settlement.html", f=f, items=items, preview=rows,
                               rows_json=json.dumps(rows), totals={k: sum((i[k] for i in items), ZERO)
                                                                   for k in ("due", "received", "outstanding")})
    rows = json.loads(request.form.get("rows") or "[]")
    outstanding = {i["invoice"].id: i["outstanding"] for i in items}
    made = 0
    try:
        for n, row in enumerate(rows):
            choice = request.form.get(f"invoice_{n}") or ""
            if not choice.isdigit() or int(choice) not in outstanding:
                continue
            invoice_id = int(choice)
            amount = Decimal(row["amount"])
            settle = min(amount, outstanding[invoice_id])
            receipt = accounts.create_receipt(tenant, current_user, accounts.VoucherInput(
                party_id=f.customer_id, voucher_date=date.fromisoformat(row["date"]), amount=amount,
                mode=MoneyMode.BANK, reference=row.get("utr") or None,
                notes=f"{f.name} payout" + (f", loan {row['loan_ref']}" if row.get("loan_ref") else ""),
                allocations=[accounts.Allocation("invoice", invoice_id, settle)] if settle > 0 else []))
            outstanding[invoice_id] -= settle
            _audit("financier_payout_recorded", "customer_receipt", receipt.id, financier=f.name,
                   amount=str(amount), invoice_id=invoice_id)
            made += 1
    except accounts.AccountsError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        return redirect(url_for("warehouse.settlement", financier_id=f.id))
    db.session.commit()
    charges = sum((Decimal(r["charges"]) for r in rows if r.get("charges")), ZERO)
    note = f" The statement shows Rs. {charges:.2f} of charges deducted - record them as an expense." if charges else ""
    flash(f"{made} payout{'s' if made != 1 else ''} recorded against their invoices.{note}", "success")
    return redirect(url_for("warehouse.settlement", financier_id=f.id))


# --- rack labels ------------------------------------------------------------------------------------------

def rack_codes(text: str) -> list[str]:
    """"A1-A5, C3, EMI-READY" -> each rack. A range repeats the letters with
    the numbers between."""
    out = []
    for part in re.split(r"[,\n]+", (text or "").upper()):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"([A-Z]*)(\d+)\s*-\s*\1?(\d+)", part)
        if m and int(m.group(3)) >= int(m.group(2)) and int(m.group(3)) - int(m.group(2)) < 200:
            out += [f"{m.group(1)}{n}" for n in range(int(m.group(2)), int(m.group(3)) + 1)]
        else:
            out.append(dispatch.clean_bin(part))
    seen = []
    for code in out:
        if code not in seen:
            seen.append(code)
    return seen[:500]


@warehouse_bp.route("/racks", methods=["GET", "POST"])
@roles_required(ADMIN, UserRole.STAFF)
@feature_required("warehouse_dispatch")
def racks():
    from app.warehouse.routes import _scope

    _scope()
    if request.method == "POST":
        from app.inventory.labels import barcode_svg

        try:
            codes = rack_codes(request.form.get("codes", ""))
        except dispatch.DispatchError as exc:
            flash(exc.message, "error")
            return redirect(url_for("warehouse.racks"))
        if request.form.get("emi_ready") and EMI_READY not in codes:
            codes.insert(0, EMI_READY)
        if not codes:
            flash("Type the racks to print, e.g. A1-A10, C3.", "error")
            return redirect(url_for("warehouse.racks"))
        labels = [{"code": c, "svg": barcode_svg(RACK_PREFIX + c)} for c in codes]
        html = render_template("warehouse/racks_pdf.html", labels=labels, emi_ready=EMI_READY)
        pdf = HTML(string=html, base_url=current_app.root_path).write_pdf()
        return send_file(BytesIO(pdf), mimetype="application/pdf", as_attachment=False, download_name="rack-labels.pdf")
    used = (db.session.query(dispatch.ProductSerial.bin_code, db.func.count())
            .filter(dispatch.ProductSerial.tenant_id == current_user.tenant_id,
                    dispatch.ProductSerial.bin_code.isnot(None),
                    dispatch.ProductSerial.status.in_((dispatch.SerialStatus.IN_STOCK, dispatch.SerialStatus.PICKED)))
            .group_by(dispatch.ProductSerial.bin_code).order_by(dispatch.ProductSerial.bin_code).all())
    return render_template("warehouse/racks.html", used=used, emi_ready=EMI_READY)
