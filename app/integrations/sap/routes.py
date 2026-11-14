"""Setup > SAP link: the connection, Test, Fetch now, and the list of SAP
codes waiting to be matched to the business's own suppliers and items."""

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.extensions import db
from app.integrations.sap import sap_bp, sync
from app.models.product import Product
from app.models.purchase import PurchaseOrder, Supplier
from app.models.sap import ORDER_WAITING, SapCall, SapOrder
from app.models.tenant import Gstin
from app.models.user import UserRole
from app.utils.audit import record_audit
from app.utils.features import feature_required

ADMIN = UserRole.CLIENT_ADMIN


def _tenant():
    return current_user.tenant


def _page(form=None, status=200):
    tenant = _tenant()
    conn = sync.connection(tenant)
    waiting = sync.waiting(tenant)
    return render_template(
        "sap/index.html", conn=conn, form=form,
        gstins=tenant.gstins.filter(Gstin.is_active.is_(True)).order_by(Gstin.is_primary.desc(), Gstin.id).all(),
        to_map=len(waiting["suppliers"]) + len(waiting["materials"]),
        orders=PurchaseOrder.query.filter_by(tenant_id=tenant.id, source="sap").count(),
        held=SapOrder.query.filter_by(tenant_id=tenant.id, status=ORDER_WAITING).count(),
        calls=SapCall.query.filter_by(tenant_id=tenant.id).order_by(SapCall.id.desc()).limit(15).all()), status


@sap_bp.route("/", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("sap_link")
def index():
    if request.method == "POST":
        try:
            conn = sync.save_connection(_tenant(), current_user, request.form)
            db.session.flush()
            record_audit(current_user, "sap_connection_saved", tenant_id=current_user.tenant_id,
                         entity_type="sap_connection", entity_id=conn.id,
                         details={"base_url": conn.base_url, "api": conn.api, "auth": conn.auth,
                                  "username": conn.username, "enabled": conn.enabled,
                                  "password_changed": bool(request.form.get("secret"))})
            db.session.commit()
            flash("Saved. Press Test to check that SAP answers.", "success")
            return redirect(url_for("sap.index"))
        except sync.SyncError as exc:
            db.session.rollback()
            flash(exc.message, "error")
            return _page(request.form, 400)
    return _page()


@sap_bp.route("/test", methods=["POST"])
@roles_required(ADMIN)
@feature_required("sap_link")
def test():
    try:
        result = sync.test(_tenant(), current_user)
        if result["ok"]:
            flash("SAP answered, and its purchase order service has every field ARFA reads.", "success")
        else:
            flash("SAP answered, but its purchase order service lacks: " + ", ".join(result["missing"])
                  + ". Try the other service version, or ask the SAP team.", "error")
    except sync.SyncError as exc:
        flash(exc.message, "error")
    db.session.commit()                 # the call log
    return redirect(url_for("sap.index"))


@sap_bp.route("/fetch", methods=["POST"])
@roles_required(ADMIN)
@feature_required("sap_link")
def fetch():
    try:
        counts = sync.pull(_tenant(), current_user)
        record_audit(current_user, "sap_orders_fetched", tenant_id=current_user.tenant_id,
                     entity_type="sap_connection", details=counts)
        db.session.commit()
        flash(_said(counts), "success")
    except sync.SyncError as exc:
        flash(exc.message, "error")
    back = request.form.get("next")
    return redirect(back if back in (url_for("purchases.orders"), url_for("sap.mapping")) else url_for("sap.index"))


def _said(counts: dict) -> str:
    bits = [f"{counts['fetched']} order{'s' if counts['fetched'] != 1 else ''} read from SAP"]
    if counts.get("imported"):
        bits.append(f"{counts['imported']} new in ARFA")
    if counts.get("cancelled"):
        bits.append(f"{counts['cancelled']} cancelled")
    if counts.get("waiting"):
        bits.append(f"{counts['waiting']} waiting for a supplier to be matched")
    return ", ".join(bits) + "."


@sap_bp.route("/map", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("sap_link")
def mapping():
    tenant = _tenant()
    if request.method == "POST":
        try:
            row = sync.set_mapping(tenant, current_user, request.form.get("mapping_id", type=int),
                                   request.form.get("target_id", type=int))
            db.session.flush()
            record_audit(current_user, "sap_code_mapped", tenant_id=tenant.id, entity_type="sap_mapping",
                         entity_id=row.id, details={"kind": row.kind, "sap_code": row.sap_code,
                                                    "supplier_id": row.supplier_id, "product_id": row.product_id})
            counts = sync.apply(tenant, current_user)
            db.session.commit()
            done = f"SAP {row.kind} {row.sap_code} " + ("matched." if row.mapped else "cleared.")
            if counts.get("imported"):
                done += f" {counts['imported']} order{'s' if counts['imported'] != 1 else ''} came in."
            flash(done, "success")
        except sync.SyncError as exc:
            db.session.rollback()
            flash(exc.message, "error")
        return redirect(url_for("sap.mapping"))
    waiting = sync.waiting(tenant)
    return render_template(
        "sap/mapping.html", waiting=waiting,
        suppliers=Supplier.query.filter_by(tenant_id=tenant.id).order_by(Supplier.name).limit(2000).all(),
        products=Product.query.filter_by(tenant_id=tenant.id).order_by(Product.name).limit(5000).all())
