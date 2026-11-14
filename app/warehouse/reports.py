"""Warehouse reports (owner):

  /warehouse/reports            serial movement (the warranty desk's search):
                                serial -> purchase bill -> picked by, when,
                                photo -> invoice -> customer; units added at
                                pick for checking
  /warehouse/reports/picking    orders waiting too long, failed scans by
                                reason and by picker
  /warehouse/reports/devices    every phone: last seen, app version, scans
                                on orders not yet confirmed
(The count result report is /warehouse/counts/<id>, with its Excel.)
"""

from __future__ import annotations

from datetime import timedelta

from flask import render_template, request
from flask_login import current_user
from sqlalchemy import func

from app.auth.decorators import roles_required
from app.extensions import db
from app.models.inventory import ProductSerial
from app.models.mixins import utcnow
from app.models.purchase import PurchaseBill
from app.models.sales import SalesDocument, SalesDocumentKind, SalesDocumentLine, SalesDocumentStatus
from app.models.user import UserRole
from app.models.warehouse import (
    PICK_RESULT_LABELS, DispatchStatus, PickResult, ReceivedUnit, SerialPick, WarehouseLogin, WarehouseSession,
)
from app.utils.features import feature_required
from app.utils.tenant_scope import tenant_query
from app.warehouse import dispatch, warehouse_bp

ADMIN = UserRole.CLIENT_ADMIN


def serial_story(tenant_id: int, serial: str) -> dict | None:
    unit = dispatch.find_serial(tenant_id, serial, lock_row=False)
    if unit is None:
        return None
    bill = db.session.get(PurchaseBill, unit.purchase_bill_id) if unit.purchase_bill_id else None
    received = ReceivedUnit.query.filter_by(tenant_id=tenant_id, serial_no=unit.serial_no).order_by(
        ReceivedUnit.id.desc()).first()
    picks = (SerialPick.query.filter_by(tenant_id=tenant_id, serial_id=unit.id).order_by(SerialPick.id).all())
    order = None
    if unit.picked_sales_line_id:
        line = db.session.get(SalesDocumentLine, unit.picked_sales_line_id)
        order = line.document if line is not None else None
    invoice = None
    if unit.sold_invoice_id:
        from app.models.invoice import Invoice

        invoice = db.session.get(Invoice, unit.sold_invoice_id)
    return {"unit": unit, "bill": bill, "received": received, "picks": picks, "order": order, "invoice": invoice}


@warehouse_bp.route("/reports")
@roles_required(ADMIN)
@feature_required("warehouse_dispatch")
def reports():
    serial = (request.args.get("serial") or "").strip()
    story = serial_story(current_user.tenant_id, serial) if serial else None
    added = (tenant_query(ProductSerial).filter_by(added_at_pick=True)
             .order_by(ProductSerial.picked_at.desc().nullslast()).limit(100).all())
    return render_template("warehouse/reports.html", serial=serial, story=story, added=added,
                           result_labels=PICK_RESULT_LABELS)


@warehouse_bp.route("/reports/picking")
@roles_required(ADMIN)
@feature_required("warehouse_dispatch")
def report_picking():
    hours = max(1, min(request.args.get("hours", 2, type=int), 240))
    days = max(1, min(request.args.get("days", 30, type=int), 365))
    cutoff = utcnow() - timedelta(hours=hours)
    late = (tenant_query(SalesDocument).filter(SalesDocument.kind == SalesDocumentKind.SALES_ORDER,
                                               SalesDocument.status == SalesDocumentStatus.OPEN,
                                               SalesDocument.dispatch_status.in_((DispatchStatus.TO_PICK,
                                                                                  DispatchStatus.PICKING)),
                                               SalesDocument.created_at < cutoff)
            .order_by(SalesDocument.created_at).all())
    since = utcnow() - timedelta(days=days)
    bad = [r for r in PickResult if r not in (PickResult.OK, PickResult.ADDED)]
    base = SerialPick.query.filter(SerialPick.tenant_id == current_user.tenant_id, SerialPick.created_at >= since)
    by_reason = dict(base.filter(SerialPick.result.in_(bad)).with_entities(SerialPick.result, func.count())
                     .group_by(SerialPick.result).all())
    rows = (base.with_entities(SerialPick.login_id, SerialPick.result, func.count())
            .group_by(SerialPick.login_id, SerialPick.result).all())
    logins = {l.id: l for l in tenant_query(WarehouseLogin).all()}
    by_picker: dict[int, dict] = {}
    for login_id, result, n in rows:
        row = by_picker.setdefault(login_id, {"login": logins.get(login_id), "good": 0, "failed": 0, "reasons": {}})
        if result in (PickResult.OK, PickResult.ADDED):
            row["good"] += n
        else:
            row["failed"] += n
            row["reasons"][result] = n
    orders_done = dict(tenant_query(SalesDocument).filter(SalesDocument.picked_confirmed_at >= since)
                       .with_entities(SalesDocument.picked_confirmed_by_id, func.count())
                       .group_by(SalesDocument.picked_confirmed_by_id).all())
    return render_template("warehouse/report_picking.html", late=late, hours=hours, days=days, by_reason=by_reason,
                           by_picker=sorted(by_picker.values(), key=lambda r: -r["failed"]), orders_done=orders_done,
                           labels=PICK_RESULT_LABELS, now=utcnow())


@warehouse_bp.route("/reports/devices")
@roles_required(ADMIN)
@feature_required("warehouse_dispatch")
def report_devices():
    sessions = (tenant_query(WarehouseSession).filter(WarehouseSession.revoked_at.is_(None))
                .order_by(WarehouseSession.last_seen_at.desc().nullslast()).all())
    open_picks = dict(
        SerialPick.query.join(SalesDocument, SerialPick.sales_document_id == SalesDocument.id)
        .filter(SerialPick.tenant_id == current_user.tenant_id, SerialPick.undone_at.is_(None),
                SerialPick.result.in_((PickResult.OK, PickResult.ADDED)),
                SalesDocument.dispatch_status == DispatchStatus.PICKING)
        .with_entities(SerialPick.session_id, func.count()).group_by(SerialPick.session_id).all())
    return render_template("warehouse/report_devices.html", sessions=sessions, open_picks=open_picks, now=utcnow())
