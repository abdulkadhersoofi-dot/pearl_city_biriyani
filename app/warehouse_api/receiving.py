"""Receive mode and Count mode in the ARFA Warehouse app (app/warehouse/receiving.py
holds the rules). Same sign-in, same warehouse scoping, the same offline
retry rule: a unit or count scan sent twice with the same client id is one.

  GET  /receipts?location_id=                    open receipts at my warehouse
  GET  /receipts/<id>                            its lines, what's scanned
  POST /receipts/<id>/lines/<line_id>/units      {serial_no, method, client_unit_id}
  DELETE /receipts/<id>/units/<unit_id>
  POST /receipts/<id>/units/<unit_id>/photo
  GET  /counts?location_id=  POST /counts        open counts / start one
  POST /counts/<id>/scans                        {serial_no, client_scan_id}
  POST /counts/<id>/finish
"""

from __future__ import annotations

from decimal import Decimal

from flask import g, jsonify, request
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.media import UploadedImage
from app.models.mixins import utcnow
from app.models.purchase import PurchaseOrderLine
from app.models.warehouse import ReceiptStatus, ReceivedUnit, StockCount, StockCountScan, WarehouseReceipt
from app.utils.audit import record_audit
from app.warehouse import dispatch, receiving
from app.warehouse_api import warehouse_api_bp as api
from app.warehouse_api.routes import _ApiAbort, _body, _error, _iso, _jpeg, _location_id, _method, signed_in


@api.errorhandler(receiving.ReceiveError)
def _receive_error(exc: receiving.ReceiveError):
    db.session.rollback()
    return _error(exc.message, exc.status)


def _receipt(receipt_id: int) -> WarehouseReceipt:
    receipt = WarehouseReceipt.query.filter(WarehouseReceipt.id == receipt_id,
                                            WarehouseReceipt.tenant_id == g.wh_tenant.id,
                                            WarehouseReceipt.location_id.in_(g.wh_login.location_ids or [0])).first()
    if receipt is None:
        raise _ApiAbort("Receipt not found.", 404)
    return receipt


def _receipt_json(r: WarehouseReceipt, detail: bool = False) -> dict:
    counted = receiving.received_counts(r)
    order = r.purchase_order
    lines = []
    for l in receiving.serial_lines(order):
        row = {"id": l.id, "description": l.description, "qty": int(Decimal(l.qty)), "received": counted.get(l.id, 0)}
        if detail:
            p = l.product
            row["product"] = {"id": p.id, "name": p.name, "model_no": p.model_no or "", "barcode": p.barcode or ""}
            row["units"] = [{"id": u.id, "serial_no": u.serial_no, "by": u.login.name if u.login else None,
                             "at": _iso(u.created_at), "has_photo": bool(u.photo_id)}
                            for u in r.units if u.order_line_id == l.id]
        lines.append(row)
    return {"id": r.id, "order_number": order.number, "supplier": order.supplier.name, "status": r.status.value,
            "location_id": r.location_id, "created_at": _iso(r.created_at), "lines": lines,
            "units": sum(x["qty"] for x in lines), "received": sum(x["received"] for x in lines)}


@api.route("/receipts")
@signed_in
def receipts():
    location_id = _location_id()
    rows = (WarehouseReceipt.query.filter_by(tenant_id=g.wh_tenant.id, location_id=location_id,
                                             status=ReceiptStatus.OPEN)
            .order_by(WarehouseReceipt.id).limit(100).all())
    return jsonify({"receipts": [_receipt_json(r) for r in rows]})


@api.route("/receipts/<int:receipt_id>")
@signed_in
def receipt(receipt_id):
    return jsonify(_receipt_json(_receipt(receipt_id), detail=True))


@api.route("/receipts/<int:receipt_id>/lines/<int:line_id>/units", methods=["POST"])
@signed_in
def receive_unit(receipt_id, line_id):
    body = _body()
    r = (WarehouseReceipt.query.filter_by(id=_receipt(receipt_id).id).populate_existing().with_for_update().one())
    line = db.session.get(PurchaseOrderLine, line_id)
    if line is None or line.order_id != r.purchase_order_id:
        raise _ApiAbort("That line isn't on this order.", 404)
    unit = receiving.receive_unit(r, line, g.wh_login, str(body.get("serial_no") or ""), _method(body.get("method")),
                                  str(body.get("client_unit_id") or ""), rack=str(body.get("rack") or ""))
    db.session.commit()
    counted = receiving.received_counts(r)
    return jsonify({"unit": {"id": unit.id, "serial_no": unit.serial_no, "line_id": unit.order_line_id},
                    "line": {"id": line.id, "qty": int(Decimal(line.qty)), "received": counted.get(line.id, 0)},
                    "message": f"{unit.serial_no} received."}), 201


@api.route("/receipts/<int:receipt_id>/units/<int:unit_id>", methods=["DELETE"])
@signed_in
def remove_unit(receipt_id, unit_id):
    r = _receipt(receipt_id)
    unit = next((u for u in r.units if u.id == unit_id), None)
    if unit is None:
        raise _ApiAbort("Not found.", 404)
    if r.status != ReceiptStatus.OPEN:
        raise _ApiAbort("This receipt is closed.", 409)
    db.session.delete(unit)
    db.session.commit()
    return jsonify({"ok": True})


@api.route("/receipts/<int:receipt_id>/units/<int:unit_id>/photo", methods=["POST"])
@signed_in
def unit_photo(receipt_id, unit_id):
    r = _receipt(receipt_id)
    unit = next((u for u in r.units if u.id == unit_id), None)
    if unit is None:
        raise _ApiAbort("Not found.", 404)
    data = _jpeg()
    if unit.photo_id is None:
        image = UploadedImage(content_type="image/jpeg", data=data, is_private=True)
        db.session.add(image)
        db.session.flush()
        unit.photo_id = image.id
        db.session.commit()
    return jsonify({"ok": True, "photo_id": unit.photo_id})


# --- count mode ------------------------------------------------------------------------------------------

def _count(count_id: int) -> StockCount:
    c = StockCount.query.filter(StockCount.id == count_id, StockCount.tenant_id == g.wh_tenant.id,
                                StockCount.location_id.in_(g.wh_login.location_ids or [0])).first()
    if c is None:
        raise _ApiAbort("Count not found.", 404)
    return c


def _count_json(c: StockCount) -> dict:
    return {"id": c.id, "location_id": c.location_id, "started_by": c.started_by.name if c.started_by else None,
            "created_at": _iso(c.created_at), "finished": c.finished_at is not None, "scanned": len(c.scans)}


@api.route("/counts")
@signed_in
def counts():
    location_id = _location_id()
    rows = (StockCount.query.filter_by(tenant_id=g.wh_tenant.id, location_id=location_id, finished_at=None)
            .order_by(StockCount.id.desc()).limit(10).all())
    return jsonify({"counts": [_count_json(c) for c in rows]})


@api.route("/counts", methods=["POST"])
@signed_in
def start_count():
    location_id = _location_id()
    c = StockCount(tenant_id=g.wh_tenant.id, location_id=location_id, started_by_id=g.wh_login.id,
                   note=str(_body().get("note") or "")[:255] or None)
    db.session.add(c)
    db.session.flush()
    record_audit(None, "warehouse_count_started", tenant_id=g.wh_tenant.id, entity_type="stock_count", entity_id=c.id,
                 details={"by": g.wh_login.name})
    db.session.commit()
    return jsonify(_count_json(c)), 201


@api.route("/counts/<int:count_id>/scans", methods=["POST"])
@signed_in
def count_scan(count_id):
    body = _body()
    c = _count(count_id)
    if c.finished_at is not None:
        raise _ApiAbort("This count is finished.", 409)
    client_id = str(body.get("client_scan_id") or "").strip()[:64] or None
    if client_id:
        seen = StockCountScan.query.filter_by(tenant_id=g.wh_tenant.id, client_scan_id=client_id).first()
        if seen is not None:
            return jsonify({"serial_no": seen.serial_no, "replayed": True, "scanned": len(c.scans)})
    serial = dispatch.normalise(str(body.get("serial_no") or ""))
    if not serial:
        raise _ApiAbort("Nothing was read - scan the label again.")
    unit = dispatch.find_serial(g.wh_tenant.id, serial, lock_row=False)
    known = unit is not None and unit.location_id == c.location_id
    if any(s.serial_no == serial for s in c.scans):
        return jsonify({"serial_no": serial, "duplicate": True, "known": known, "scanned": len(c.scans),
                        "message": f"{serial} is already counted."})
    try:
        with db.session.begin_nested():
            db.session.add(StockCountScan(tenant_id=g.wh_tenant.id, count_id=c.id, serial_no=serial,
                                          login_id=g.wh_login.id, client_scan_id=client_id))
            db.session.flush()
    except IntegrityError:
        pass
    db.session.commit()
    db.session.refresh(c)
    product = unit.product.name if unit is not None and unit.product else None
    return jsonify({"serial_no": serial, "known": known, "product": product, "scanned": len(c.scans),
                    "message": f"{serial} counted" + (f" - {product}" if product else " - not in ARFA here")}), 201


@api.route("/counts/<int:count_id>/finish", methods=["POST"])
@signed_in
def finish_count(count_id):
    c = _count(count_id)
    if c.finished_at is None:
        c.finished_at = utcnow()
        db.session.commit()
    result = receiving.count_result(c)
    return jsonify({**_count_json(c), "in_stock": result["in_stock"], "matched": result["matched"],
                    "missing": len(result["missing"]), "extra": len(result["extra"])})
