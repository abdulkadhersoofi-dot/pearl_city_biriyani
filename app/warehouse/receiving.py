"""Serials in at the warehouse, not typed (Receive mode), and counting a
warehouse's floor (Count mode).

Receive: a purchase order for serial-tracked items is sent to a warehouse
("Receive at warehouse"); its pickers open it in the ARFA Warehouse app and
scan every unit's label. Each serial is checked against the batch and every
serial the business already has. The purchase bill is then made from the
order with the scanned serials filled in and that warehouse as the place
stock arrives - and posts as it always does (serials land IN_STOCK there).

Count: every serial on the floor is scanned; the report lists serials ARFA
has in stock there that weren't scanned, and serials scanned that ARFA
doesn't have there - run once before go-live to clean up old mismatches.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO

from flask import abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from app.auth.decorators import roles_required
from app.extensions import db
from app.models.inventory import ProductSerial, SerialStatus
from app.models.mixins import utcnow
from app.models.purchase import PurchaseOrder, PurchaseOrderStatus
from app.models.user import UserRole
from app.models.warehouse import (
    PickMethod, ReceiptStatus, ReceivedUnit, StockCount, StockCountScan, WarehouseReceipt,
)
from app.utils.audit import record_audit
from app.utils.features import feature_required
from app.utils.tenant_scope import tenant_query
from app.warehouse import dispatch, notify, warehouse_bp

ADMIN, STAFF = UserRole.CLIENT_ADMIN, UserRole.STAFF


class ReceiveError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# --- the service ------------------------------------------------------------------------------------

def serial_lines(order: PurchaseOrder) -> list:
    return [l for l in order.lines if l.product is not None and l.product.track_serial]


def start_receipt(tenant, user, order: PurchaseOrder, location) -> WarehouseReceipt:
    if order.status != PurchaseOrderStatus.OPEN:
        raise ReceiveError("Only an open purchase order can be received.")
    if not serial_lines(order):
        raise ReceiveError("This order has no serial-tracked items to scan in.")
    open_one = WarehouseReceipt.query.filter_by(tenant_id=tenant.id, purchase_order_id=order.id,
                                                status=ReceiptStatus.OPEN).first()
    if open_one is not None:
        raise ReceiveError(f"{order.number} is already being received at {open_one.location.name}.")
    receipt = WarehouseReceipt(tenant_id=tenant.id, purchase_order_id=order.id, location_id=location.id,
                               created_by_id=user.id)
    db.session.add(receipt)
    db.session.flush()
    units = sum(int(Decimal(l.qty)) for l in serial_lines(order))
    notify.receipt(tenant.id, location.id, f"Receive {order.number}",
                   f"{units} unit{'s' if units != 1 else ''} from {order.supplier.name} - scan them in")
    return receipt


def received_counts(receipt: WarehouseReceipt) -> dict[int, int]:
    out: dict[int, int] = {}
    for unit in receipt.units:
        out[unit.order_line_id] = out.get(unit.order_line_id, 0) + 1
    return out


def receive_unit(receipt: WarehouseReceipt, line, login, text: str, method: PickMethod,
                 client_unit_id: str | None = None) -> ReceivedUnit:
    client_unit_id = (client_unit_id or "").strip()[:64] or None
    if client_unit_id:
        seen = ReceivedUnit.query.filter_by(tenant_id=receipt.tenant_id, client_unit_id=client_unit_id).first()
        if seen is not None:
            return seen
    if receipt.status != ReceiptStatus.OPEN:
        raise ReceiveError("This receipt is closed.", 409)
    if line.order_id != receipt.purchase_order_id or line.product is None or not line.product.track_serial:
        raise ReceiveError("That line isn't on this order (or has no serials).", 404)
    serial = dispatch.normalise(text)
    if not serial:
        raise ReceiveError("Nothing was read - scan the label again.")
    if dispatch._is_this_product(line.product, serial):
        raise ReceiveError(f"That's the model number of {line.product.name} - scan the serial number.", 422)
    if any(u.serial_no == serial for u in receipt.units):
        raise ReceiveError(f"{serial} is already scanned on this receipt.", 409)
    if dispatch.find_serial(receipt.tenant_id, serial, lock_row=False) is not None:
        raise ReceiveError(f"{serial} is already in your records - check the label.", 409)
    elsewhere = (ReceivedUnit.query.join(WarehouseReceipt)
                 .filter(ReceivedUnit.tenant_id == receipt.tenant_id, ReceivedUnit.serial_no == serial,
                         WarehouseReceipt.status == ReceiptStatus.OPEN).first())
    if elsewhere is not None:
        raise ReceiveError(f"{serial} is already scanned on {elsewhere.receipt.purchase_order.number}.", 409)
    if received_counts(receipt).get(line.id, 0) >= int(Decimal(line.qty)):
        raise ReceiveError(f"All {int(Decimal(line.qty))} of {line.product.name} are scanned.", 409)
    unit = ReceivedUnit(tenant_id=receipt.tenant_id, receipt_id=receipt.id, order_line_id=line.id,
                        product_id=line.product_id, serial_no=serial, raw_text=(text or "")[:200], method=method,
                        login_id=login.id if login else None, client_unit_id=client_unit_id)
    try:
        with db.session.begin_nested():
            db.session.add(unit)
            db.session.flush()
    except IntegrityError:
        raise ReceiveError(f"{serial} is already scanned on this receipt.", 409)
    receipt.updated_at = utcnow()
    return unit


def bill_rows(receipt: WarehouseReceipt) -> list[dict]:
    """The purchase bill form's lines: the order's, with the scanned serials
    (and, for a serial line, the quantity actually scanned)."""
    by_line: dict[int, list[str]] = {}
    for unit in receipt.units:
        by_line.setdefault(unit.order_line_id, []).append(unit.serial_no)
    rows = []
    for l in receipt.purchase_order.lines:
        serials = by_line.get(l.id, [])
        tracked = l.product is not None and l.product.track_serial
        qty = Decimal(len(serials)) if tracked and serials else Decimal(l.qty)
        rows.append({"product_id": str(l.product_id or ""), "description": l.description,
                     "hsn": l.hsn_or_sac_code or "", "qty": f"{qty.normalize():f}", "unit": l.unit,
                     "rate": f"{l.rate:.2f}", "discount": "", "gst_rate": f"{l.gst_rate.normalize():f}",
                     "itc": "eligible", "serials": "\n".join(serials)})
    return rows


def mark_billed(receipt: WarehouseReceipt, bill) -> None:
    receipt.status = ReceiptStatus.DONE
    receipt.purchase_bill_id = bill.id


def count_result(count: StockCount) -> dict:
    scanned = {s.serial_no for s in count.scans}
    in_stock = ProductSerial.query.filter(ProductSerial.tenant_id == count.tenant_id,
                                          ProductSerial.location_id == count.location_id,
                                          ProductSerial.status.in_((SerialStatus.IN_STOCK, SerialStatus.PICKED))).all()
    in_stock_nos = {u.serial_no.upper(): u for u in in_stock}
    missing = sorted((u for u in in_stock if u.serial_no.upper() not in scanned),
                     key=lambda u: (u.product.name if u.product else "", u.serial_no))
    extra = []
    for no in sorted(scanned - set(in_stock_nos)):
        unit = dispatch.find_serial(count.tenant_id, no, lock_row=False)
        if unit is None:
            why = "Not in ARFA"
        elif unit.status not in (SerialStatus.IN_STOCK, SerialStatus.PICKED):
            why = f"ARFA has it {unit.status.value.replace('_', ' ')}"
        else:
            why = f"ARFA has it at {unit.location.name if unit.location else 'no location'}"
        extra.append({"serial_no": no, "unit": unit, "why": why})
    return {"scanned": len(scanned), "in_stock": len(in_stock), "matched": len(scanned & set(in_stock_nos)),
            "missing": missing, "extra": extra}


# --- the website -------------------------------------------------------------------------------------

def _scope():
    from app.warehouse.routes import _scope as scope

    return scope()


def _receipt(receipt_id) -> WarehouseReceipt:
    receipt = tenant_query(WarehouseReceipt).filter_by(id=receipt_id).first_or_404()
    location = _scope()
    if location is not None and receipt.location_id != location.id:
        abort(404)
    return receipt


@warehouse_bp.route("/receipts")
@roles_required(ADMIN, STAFF)
@feature_required("warehouse_dispatch")
def receipts():
    location = _scope()
    query = tenant_query(WarehouseReceipt)
    if location is not None:
        query = query.filter_by(location_id=location.id)
    rows = query.order_by(WarehouseReceipt.status, WarehouseReceipt.id.desc()).limit(100).all()
    counts_q = tenant_query(StockCount)
    if location is not None:
        counts_q = counts_q.filter_by(location_id=location.id)
    counts = counts_q.order_by(StockCount.id.desc()).limit(20).all()
    open_orders = []
    if current_user.is_client_admin:
        open_orders = [o for o in tenant_query(PurchaseOrder).filter_by(status=PurchaseOrderStatus.OPEN)
                       .order_by(PurchaseOrder.id.desc()).limit(100) if serial_lines(o)]
    return render_template("warehouse/receipts.html", rows=rows, counts=counts, location=location,
                           counted={r.id: received_counts(r) for r in rows}, open_orders=open_orders,
                           warehouses=dispatch.warehouses(current_user.tenant_id), serial_lines=serial_lines)


@warehouse_bp.route("/receipts/new", methods=["POST"])
@roles_required(ADMIN)
@feature_required("warehouse_dispatch")
def new_receipt():
    order = tenant_query(PurchaseOrder).filter_by(id=request.form.get("purchase_order_id", type=int)).first_or_404()
    try:
        location = dispatch.resolve_warehouse(current_user.tenant, request.form.get("location_id"))
        receipt = start_receipt(current_user.tenant, current_user, order, location)
    except (ReceiveError, dispatch.DispatchError) as exc:
        db.session.rollback()
        flash(exc.message, "error")
        return redirect(request.referrer or url_for("warehouse.receipts"))
    record_audit(current_user, "warehouse_receipt_started", tenant_id=order.tenant_id, entity_type="purchase_order",
                 entity_id=order.id, details={"number": order.number, "warehouse": location.name})
    notify.commit_and_push()
    flash(f"{order.number} sent to {location.name} - its pickers scan the units in on the ARFA Warehouse app.",
          "success")
    return redirect(url_for("warehouse.receipt", receipt_id=receipt.id))


@warehouse_bp.route("/receipts/<int:receipt_id>")
@roles_required(ADMIN, STAFF)
@feature_required("warehouse_dispatch")
def receipt(receipt_id):
    r = _receipt(receipt_id)
    return render_template("warehouse/receipt.html", r=r, counted=received_counts(r), serial_lines=serial_lines(r.purchase_order))


@warehouse_bp.route("/receipts/<int:receipt_id>/units/<int:unit_id>/delete", methods=["POST"])
@roles_required(ADMIN, STAFF)
@feature_required("warehouse_dispatch")
def delete_unit(receipt_id, unit_id):
    r = _receipt(receipt_id)
    unit = next((u for u in r.units if u.id == unit_id), None)
    if unit is None or r.status != ReceiptStatus.OPEN:
        abort(404)
    db.session.delete(unit)
    record_audit(current_user, "warehouse_receipt_unit_removed", tenant_id=r.tenant_id, entity_type="warehouse_receipt",
                 entity_id=r.id, details={"serial": unit.serial_no})
    db.session.commit()
    flash(f"{unit.serial_no} taken off.", "success")
    return redirect(url_for("warehouse.receipt", receipt_id=r.id))


@warehouse_bp.route("/receipts/<int:receipt_id>/cancel", methods=["POST"])
@roles_required(ADMIN)
@feature_required("warehouse_dispatch")
def cancel_receipt(receipt_id):
    r = _receipt(receipt_id)
    if r.status == ReceiptStatus.OPEN:
        r.status = ReceiptStatus.CANCELLED
        record_audit(current_user, "warehouse_receipt_cancelled", tenant_id=r.tenant_id,
                     entity_type="warehouse_receipt", entity_id=r.id, details={"order": r.purchase_order.number})
        db.session.commit()
        flash("Receiving stopped - the scans are kept on this page only.", "success")
    return redirect(url_for("warehouse.receipt", receipt_id=r.id))


@warehouse_bp.route("/counts/<int:count_id>")
@roles_required(ADMIN, STAFF)
@feature_required("warehouse_dispatch")
def count(count_id):
    c = tenant_query(StockCount).filter_by(id=count_id).first_or_404()
    location = _scope()
    if location is not None and c.location_id != location.id:
        abort(404)
    result = count_result(c)
    if request.args.get("format") == "xlsx":
        return _count_xlsx(c, result)
    return render_template("warehouse/count.html", c=c, result=result)


def _count_xlsx(c: StockCount, result: dict):
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Not scanned"
    ws.append(["In ARFA's stock at " + c.location.name + " but not scanned"])
    ws.append(["Serial", "Item", "Model no.", "Status"])
    for row in (1, 2):
        ws[f"A{row}"].font = Font(bold=True)
    for u in result["missing"]:
        ws.append([u.serial_no, u.product.name if u.product else "", u.product.model_no if u.product else "",
                   u.status.value])
    ws2 = wb.create_sheet("Not in ARFA here")
    ws2.append(["Scanned on the floor but not in ARFA's stock here"])
    ws2.append(["Serial", "Why"])
    for row in (1, 2):
        ws2[f"A{row}"].font = Font(bold=True)
    for e in result["extra"]:
        ws2.append([e["serial_no"], e["why"]])
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=f"count-{c.location.name}-{c.created_at:%Y%m%d}.xlsx")
