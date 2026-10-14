from datetime import date
from decimal import Decimal

from flask import Response, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.extensions import db
from app.inventory import inventory_bp, ledger, reports
from app.inventory import labels as label_service
from app.inventory import services
from app.models.inventory import (
    ADJUSTMENT_REASON_LABELS,
    LOCATION_KIND_LABELS,
    AdjustmentReason,
    LocationKind,
    ProductSerial,
    SerialStatus,
    StockAdjustment,
    StockLocation,
    StockTransfer,
)
from app.models.product import Product
from app.models.user import User, UserRole
from app.utils.audit import record_audit
from app.utils.features import feature_required
from app.utils.tenant_scope import assert_owns, tenant_query

ADMIN = UserRole.CLIENT_ADMIN


def _tenant():
    return current_user.tenant


def _int(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _locations(active_only=True):
    """The client's locations, branch locations included (each branch
    login gets one the first time it's needed - made here so they can be
    picked before that branch has sold anything)."""
    tenant_id = current_user.tenant_id
    ledger.default_location(tenant_id)
    for user in tenant_query(User).filter_by(role=UserRole.STAFF, is_active=True):
        ledger.branch_location(tenant_id, user)
    db.session.commit()
    query = tenant_query(StockLocation)
    if active_only:
        query = query.filter_by(is_active=True)
    return query.order_by(StockLocation.is_default.desc(), StockLocation.kind, StockLocation.name).all()


def _stock_products():
    return reports.stocked_products(current_user.tenant_id)


# --- stock summary + item ledger --------------------------------------------------

@inventory_bp.route("/")
@roles_required(ADMIN)
@feature_required("inventory")
def index():
    locations = _locations()
    location_id = _int(request.args.get("location"))
    search = (request.args.get("q") or "").strip()
    low_only = bool(request.args.get("low"))
    rows, total_value = reports.stock_summary(current_user.tenant_id, location_id, search or None, low_only=low_only)
    low_count = len(reports.low_stock(current_user.tenant_id))
    return render_template("inventory/index.html", rows=rows, total_value=total_value, locations=locations,
                           location_id=location_id, search=search, low_only=low_only, low_count=low_count)


@inventory_bp.route("/items/<int:product_id>")
@roles_required(ADMIN)
@feature_required("inventory")
def item_ledger(product_id):
    product = tenant_query(Product).filter_by(id=product_id).first_or_404()
    assert_owns(product)
    locations = _locations(active_only=False)
    location_id = _int(request.args.get("location"))
    date_from, date_to = _date(request.args.get("from")), _date(request.args.get("to"))
    opening, rows, closing = reports.item_ledger(current_user.tenant_id, product.id, location_id, date_from, date_to)
    by_location = ledger.stock_by_location(current_user.tenant_id, [product.id])
    batches = ledger.batch_stock(current_user.tenant_id, product.id) if product.track_batch else []
    return render_template("inventory/item.html", product=product, locations=locations, location_id=location_id,
                           date_from=date_from, date_to=date_to, opening=opening, rows=rows, closing=closing,
                           by_location=by_location, batches=batches)


# --- locations ------------------------------------------------------------------------

@inventory_bp.route("/locations", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("inventory")
def locations():
    if request.method == "POST":
        try:
            kind = LocationKind(request.form.get("kind") or "godown")
            location = services.create_location(_tenant(), request.form.get("name"), kind, request.form.get("address"))
        except (ValueError, ledger.StockError) as e:
            db.session.rollback()
            flash(getattr(e, "message", "Pick a valid location type."), "error")
            return redirect(url_for("inventory.locations"))
        record_audit(current_user, "stock_location_created", tenant_id=current_user.tenant_id,
                     entity_type="stock_location", entity_id=location.id, details={"name": location.name})
        db.session.commit()
        flash(f"{location.name} added.", "success")
        return redirect(url_for("inventory.locations"))
    all_locations = _locations(active_only=False)
    on_hand = ledger.stock_by_location(current_user.tenant_id)
    counts = {}
    for (pid, lid), qty in on_hand.items():
        if qty != 0:
            counts[lid] = counts.get(lid, 0) + 1
    return render_template("inventory/locations.html", locations=all_locations, counts=counts,
                           kind_labels=LOCATION_KIND_LABELS)


@inventory_bp.route("/locations/<int:location_id>/deactivate", methods=["POST"])
@roles_required(ADMIN)
@feature_required("inventory")
def deactivate_location(location_id):
    location = tenant_query(StockLocation).filter_by(id=location_id).first_or_404()
    assert_owns(location)
    try:
        services.deactivate_location(location)
    except ledger.StockError as e:
        flash(e.message, "error")
        return redirect(url_for("inventory.locations"))
    record_audit(current_user, "stock_location_deactivated", tenant_id=current_user.tenant_id,
                 entity_type="stock_location", entity_id=location.id, details={"name": location.name})
    db.session.commit()
    flash(f"{location.name} switched off.", "success")
    return redirect(url_for("inventory.locations"))


# --- adjustments ------------------------------------------------------------------------

def _lines_from_form():
    return services.parse_stock_lines(
        request.form.getlist("line_product_id[]"), request.form.getlist("line_qty[]"),
        request.form.getlist("line_cost[]"), request.form.getlist("line_batch[]"),
        request.form.getlist("line_expiry[]"),
    )


def _rows_from_form(form):
    """The rows typed in, to put back on the form when it's refused."""
    keys = ("product_id", "qty", "cost", "batch", "expiry")
    cols = [form.getlist(f"line_{k}[]") for k in keys]
    rows = []
    for i in range(len(cols[0])):
        row = {k: (c[i] if i < len(c) else "") for k, c in zip(keys, cols)}
        if row["product_id"] or row["qty"]:
            rows.append(row)
    return rows


def _products_json(products):
    return [{"id": p.id, "name": p.name, "unit": p.unit, "batch": bool(p.track_batch),
             "cost": float(p.cost_price or 0)} for p in products]


@inventory_bp.route("/adjustments")
@roles_required(ADMIN)
@feature_required("inventory")
def adjustments():
    rows = tenant_query(StockAdjustment).order_by(StockAdjustment.adjusted_on.desc(), StockAdjustment.id.desc()).limit(300).all()
    return render_template("inventory/adjustments.html", adjustments=rows, reason_labels=ADJUSTMENT_REASON_LABELS)


@inventory_bp.route("/adjustments/new", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("inventory")
def new_adjustment():
    locations = _locations()
    products = _stock_products()
    if request.method == "POST":
        try:
            reason = AdjustmentReason(request.form.get("reason") or "")
        except ValueError:
            flash("Pick why stock is being adjusted.", "error")
            return redirect(url_for("inventory.new_adjustment"))
        try:
            lines = _lines_from_form()
            adjustment = services.create_adjustment(
                _tenant(), current_user, _int(request.form.get("location_id")), reason, lines,
                note=request.form.get("note"), adjusted_on=_date(request.form.get("adjusted_on")),
            )
        except ledger.StockError as e:
            db.session.rollback()
            flash(e.message, "error")
            return render_template("inventory/adjustment_form.html", locations=locations,
                                   products_json=_products_json(products), reason_labels=ADJUSTMENT_REASON_LABELS,
                                   form=request.form, rows=_rows_from_form(request.form), today=date.today()), 400
        record_audit(current_user, "stock_adjusted", tenant_id=current_user.tenant_id, entity_type="stock_adjustment",
                     entity_id=adjustment.id, details={"number": adjustment.number, "reason": reason.value,
                                                      "lines": len(adjustment.lines)})
        db.session.commit()
        flash(f"Stock adjustment {adjustment.number} saved.", "success")
        return redirect(url_for("inventory.view_adjustment", adjustment_id=adjustment.id))
    return render_template("inventory/adjustment_form.html", locations=locations, products_json=_products_json(products),
                           reason_labels=ADJUSTMENT_REASON_LABELS, form={}, today=date.today())


@inventory_bp.route("/adjustments/<int:adjustment_id>")
@roles_required(ADMIN)
@feature_required("inventory")
def view_adjustment(adjustment_id):
    adjustment = tenant_query(StockAdjustment).filter_by(id=adjustment_id).first_or_404()
    assert_owns(adjustment)
    return render_template("inventory/adjustment_view.html", adjustment=adjustment, reason_labels=ADJUSTMENT_REASON_LABELS)


@inventory_bp.route("/adjustments/<int:adjustment_id>/void", methods=["POST"])
@roles_required(ADMIN)
@feature_required("inventory")
def void_adjustment(adjustment_id):
    adjustment = tenant_query(StockAdjustment).filter_by(id=adjustment_id).first_or_404()
    assert_owns(adjustment)
    try:
        services.void_adjustment(adjustment, current_user, request.form.get("reason"))
    except ledger.StockError as e:
        flash(e.message, "error")
        return redirect(url_for("inventory.view_adjustment", adjustment_id=adjustment.id))
    record_audit(current_user, "stock_adjustment_voided", tenant_id=current_user.tenant_id, entity_type="stock_adjustment",
                 entity_id=adjustment.id, details={"number": adjustment.number, "reason": adjustment.void_reason})
    db.session.commit()
    flash(f"{adjustment.number} voided - its stock changes were reversed.", "success")
    return redirect(url_for("inventory.view_adjustment", adjustment_id=adjustment.id))


# --- transfers + delivery challan ---------------------------------------------------------

@inventory_bp.route("/transfers")
@roles_required(ADMIN)
@feature_required("inventory")
def transfers():
    rows = tenant_query(StockTransfer).order_by(StockTransfer.transfer_date.desc(), StockTransfer.id.desc()).limit(300).all()
    return render_template("inventory/transfers.html", transfers=rows)


@inventory_bp.route("/transfers/new", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("inventory")
def new_transfer():
    locations = _locations()
    products = _stock_products()
    if request.method == "POST":
        try:
            lines = _lines_from_form()
            transfer = services.create_transfer(
                _tenant(), current_user, _int(request.form.get("from_location_id")),
                _int(request.form.get("to_location_id")), lines,
                transfer_date=_date(request.form.get("transfer_date")), note=request.form.get("note"),
                vehicle_no=request.form.get("vehicle_no"),
            )
        except ledger.StockError as e:
            db.session.rollback()
            flash(e.message, "error")
            return render_template("inventory/transfer_form.html", locations=locations,
                                   products_json=_products_json(products), form=request.form,
                                   rows=_rows_from_form(request.form), today=date.today()), 400
        record_audit(current_user, "stock_transferred", tenant_id=current_user.tenant_id, entity_type="stock_transfer",
                     entity_id=transfer.id, details={"number": transfer.number, "from": transfer.from_location.name,
                                                    "to": transfer.to_location.name})
        db.session.commit()
        flash(f"Transfer {transfer.number} saved.", "success")
        return redirect(url_for("inventory.view_transfer", transfer_id=transfer.id))
    return render_template("inventory/transfer_form.html", locations=locations, products_json=_products_json(products),
                           form={}, today=date.today())


@inventory_bp.route("/transfers/<int:transfer_id>")
@roles_required(ADMIN)
@feature_required("inventory")
def view_transfer(transfer_id):
    transfer = tenant_query(StockTransfer).filter_by(id=transfer_id).first_or_404()
    assert_owns(transfer)
    return render_template("inventory/transfer_view.html", transfer=transfer)


@inventory_bp.route("/transfers/<int:transfer_id>/challan.pdf")
@roles_required(ADMIN)
@feature_required("inventory")
def transfer_challan(transfer_id):
    from flask import current_app
    from weasyprint import HTML

    from app.utils.uploads import image_data_uri

    transfer = tenant_query(StockTransfer).filter_by(id=transfer_id).first_or_404()
    assert_owns(transfer)
    tenant = _tenant()
    value = sum((Decimal(l.qty) * Decimal(l.unit_cost or 0) for l in transfer.lines), Decimal("0")).quantize(Decimal("0.01"))
    html = render_template("inventory/challan_pdf.html", transfer=transfer, firm_name=tenant.display_name,
                           logo_data_uri=image_data_uri(tenant.logo_image_id), total_value=value)
    pdf = HTML(string=html, base_url=current_app.root_path).write_pdf()
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{transfer.number.replace("/", "-")}.pdf"'})


@inventory_bp.route("/transfers/<int:transfer_id>/void", methods=["POST"])
@roles_required(ADMIN)
@feature_required("inventory")
def void_transfer(transfer_id):
    transfer = tenant_query(StockTransfer).filter_by(id=transfer_id).first_or_404()
    assert_owns(transfer)
    try:
        services.void_transfer(transfer, current_user, request.form.get("reason"))
    except ledger.StockError as e:
        flash(e.message, "error")
        return redirect(url_for("inventory.view_transfer", transfer_id=transfer.id))
    record_audit(current_user, "stock_transfer_voided", tenant_id=current_user.tenant_id, entity_type="stock_transfer",
                 entity_id=transfer.id, details={"number": transfer.number, "reason": transfer.void_reason})
    db.session.commit()
    flash(f"{transfer.number} voided - the goods are back where they came from.", "success")
    return redirect(url_for("inventory.view_transfer", transfer_id=transfer.id))


# --- reports --------------------------------------------------------------------------------

@inventory_bp.route("/reorder")
@roles_required(ADMIN)
@feature_required("inventory")
def reorder():
    return render_template("inventory/reorder.html", rows=reports.reorder_report(current_user.tenant_id))


@inventory_bp.route("/movement")
@roles_required(ADMIN)
@feature_required("inventory")
def movement():
    days = _int(request.args.get("days")) or 90
    days = min(max(days, 7), 365)
    rows = reports.movement_analysis(current_user.tenant_id, days)
    return render_template("inventory/movement.html", rows=rows, days=days)


@inventory_bp.route("/by-location")
@roles_required(ADMIN)
@feature_required("inventory")
def by_location():
    _locations()
    search = (request.args.get("q") or "").strip()
    locations, rows = reports.location_matrix(current_user.tenant_id, search or None)
    return render_template("inventory/by_location.html", locations=locations, rows=rows, search=search)


@inventory_bp.route("/expiry")
@roles_required(ADMIN)
@feature_required("inventory")
@feature_required("batch_expiry")
def expiry():
    within = _int(request.args.get("within")) or 30
    within = min(max(within, 1), 365)
    expired, soon = reports.expiry_report(current_user.tenant_id, within)
    return render_template("inventory/expiry.html", expired=expired, soon=soon, within=within)


@inventory_bp.route("/serials")
@roles_required(ADMIN)
@feature_required("inventory")
@feature_required("serial_imei")
def serials():
    """Where is serial/IMEI X? - and every unit in stock."""
    q = (request.args.get("q") or "").strip()
    query = tenant_query(ProductSerial)
    if q:
        query = query.filter(ProductSerial.serial_no.ilike(f"%{q}%"))
    else:
        query = query.filter(ProductSerial.status == SerialStatus.IN_STOCK)
    units = query.order_by(ProductSerial.serial_no).limit(500).all()
    return render_template("inventory/serials.html", units=units, q=q)


# --- barcode labels --------------------------------------------------------------------------

@inventory_bp.route("/labels", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("inventory")
def labels():
    products = tenant_query(Product).filter_by(is_active=True).filter(Product.has_variants.is_(False)).order_by(Product.name).all()
    if request.method == "POST":
        by_id = {p.id: p for p in products}
        items = []
        for p in products:
            copies = _int(request.form.get(f"copies_{p.id}")) or 0
            if copies > 0:
                items.append((by_id[p.id], min(copies, label_service.MAX_LABELS)))
        if not items:
            flash("Enter how many labels to print for at least one item.", "error")
            return redirect(url_for("inventory.labels"))
        layout = request.form.get("layout") if request.form.get("layout") in label_service.LAYOUTS else "a4"
        built = label_service.build_labels(items, show_price=bool(request.form.get("show_price")))
        pdf = label_service.render_labels_pdf(built, layout, _tenant().display_name)
        return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": 'inline; filename="labels.pdf"'})
    return render_template("inventory/labels.html", products=products, layouts=label_service.LAYOUTS,
                           code_for=label_service.label_code)


@inventory_bp.app_template_global()
def stock_low_items():
    """For the Live dashboard / anywhere: low-stock rows when the
    signed-in client has Inventory on, else nothing."""
    from app.utils.features import current_tenant_has

    if not current_tenant_has("inventory"):
        return []
    return reports.low_stock(current_user.tenant_id)
