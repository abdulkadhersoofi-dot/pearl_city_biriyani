from datetime import date
from decimal import Decimal, InvalidOperation

from flask import Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.extensions import db
from app.inventory import ledger
from app.inventory.reports import reorder_report
from app.models.inventory import StockLocation
from app.models.media import UploadedImage
from app.models.product import Product
from app.models.purchase import (
    ITC_LABELS,
    ItcEligibility,
    MATCH_STATUS_LABELS,
    Gstr2bEntry,
    Gstr2bImport,
    MatchStatus,
    PurchaseBill,
    PurchaseOrder,
    PurchaseReturn,
    PurchaseStatus,
    Supplier,
)
from app.models.tenant import Gstin
from app.models.user import UserRole
from app.purchases import gstr2b, purchases_bp, services
from app.purchases.forms import SupplierForm
from app.utils.audit import record_audit
from app.utils.features import feature_required
from app.utils.gst_config import GST_RATE_SLABS
from app.utils.indian_states import STATE_NAME_BY_CODE
from app.utils.tenant_scope import assert_owns, tenant_query
from app.utils.uploads import UploadError, save_private_document

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


def _gstins():
    return tenant_query(Gstin).filter_by(is_active=True).order_by(Gstin.is_primary.desc(), Gstin.id).all()


def _suppliers():
    return tenant_query(Supplier).filter_by(is_active=True).order_by(Supplier.name).all()


def _locations():
    ledger.default_location(current_user.tenant_id)
    db.session.commit()
    return (tenant_query(StockLocation).filter_by(is_active=True)
            .order_by(StockLocation.is_default.desc(), StockLocation.name).all())


def _catalog_json():
    """Every item the client can buy (variant parents excluded - a
    variant is bought, not the design)."""
    products = (tenant_query(Product).filter_by(is_active=True).filter(Product.has_variants.is_(False))
                .order_by(Product.name).all())
    return [{"id": p.id, "name": p.name, "hsn": p.hsn_or_sac_code, "unit": p.unit, "gst_rate": float(p.gst_rate),
             "rate": float(p.purchase_rate or p.cost_price or 0), "batch": bool(p.track_batch),
             "serial": bool(p.track_serial)} for p in products]


# --- suppliers ---------------------------------------------------------------------------

@purchases_bp.route("/suppliers")
@roles_required(ADMIN)
@feature_required("purchases")
def suppliers():
    search = (request.args.get("q") or "").strip()
    query = tenant_query(Supplier).filter_by(is_active=True)
    if search:
        query = query.filter(db.or_(Supplier.name.ilike(f"%{search}%"), Supplier.gstin.ilike(f"%{search}%")))
    return render_template("purchases/suppliers.html", suppliers=query.order_by(Supplier.name).all(), search=search)


def _supplier_form(**kwargs) -> SupplierForm:
    form = SupplierForm(**kwargs)
    if not current_user.tenant.has_feature("payables"):
        del form.opening_balance_date
    return form


def _save_supplier(form, supplier):
    form.populate_obj(supplier)
    supplier.state_name = STATE_NAME_BY_CODE.get(supplier.state_code, "")
    supplier.opening_balance = form.opening_balance.data or Decimal("0")


@purchases_bp.route("/suppliers/new", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("purchases")
def new_supplier():
    form = _supplier_form()
    if form.validate_on_submit():
        supplier = Supplier(tenant_id=current_user.tenant_id)
        _save_supplier(form, supplier)
        db.session.add(supplier)
        db.session.flush()
        record_audit(current_user, "supplier_created", tenant_id=current_user.tenant_id, entity_type="supplier",
                     entity_id=supplier.id, details={"name": supplier.name})
        db.session.commit()
        flash(f"{supplier.name} added.", "success")
        return redirect(request.args.get("next") if (request.args.get("next") or "").startswith("/purchases/")
                        else url_for("purchases.suppliers"))
    return render_template("purchases/supplier_form.html", form=form, supplier=None)


@purchases_bp.route("/suppliers/<int:supplier_id>/edit", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("purchases")
def edit_supplier(supplier_id):
    supplier = tenant_query(Supplier).filter_by(id=supplier_id).first_or_404()
    assert_owns(supplier)
    form = _supplier_form(obj=supplier)
    if form.validate_on_submit():
        _save_supplier(form, supplier)
        record_audit(current_user, "supplier_updated", tenant_id=current_user.tenant_id, entity_type="supplier",
                     entity_id=supplier.id, details={"name": supplier.name})
        db.session.commit()
        flash(f"{supplier.name} updated.", "success")
        return redirect(url_for("purchases.suppliers"))
    bills = (tenant_query(PurchaseBill).filter_by(supplier_id=supplier.id)
             .order_by(PurchaseBill.bill_date.desc(), PurchaseBill.id.desc()).limit(50).all())
    return render_template("purchases/supplier_form.html", form=form, supplier=supplier, bills=bills)


@purchases_bp.route("/suppliers/<int:supplier_id>/deactivate", methods=["POST"])
@roles_required(ADMIN)
@feature_required("purchases")
def deactivate_supplier(supplier_id):
    supplier = tenant_query(Supplier).filter_by(id=supplier_id).first_or_404()
    assert_owns(supplier)
    supplier.is_active = False
    record_audit(current_user, "supplier_deactivated", tenant_id=current_user.tenant_id, entity_type="supplier",
                 entity_id=supplier.id, details={"name": supplier.name})
    db.session.commit()
    flash(f"{supplier.name} removed from the active list.", "success")
    return redirect(url_for("purchases.suppliers"))


# --- purchase bills ------------------------------------------------------------------------

@purchases_bp.route("/")
@roles_required(ADMIN)
@feature_required("purchases")
def bills():
    query = tenant_query(PurchaseBill)
    supplier_id = _int(request.args.get("supplier"))
    if supplier_id:
        query = query.filter_by(supplier_id=supplier_id)
    rcm = request.args.get("rcm")
    # Reverse-charge purchases are their own register - never mixed in.
    query = query.filter(PurchaseBill.is_rcm.is_(rcm == "1"))
    rows = query.order_by(PurchaseBill.bill_date.desc(), PurchaseBill.id.desc()).limit(300).all()
    return render_template("purchases/bills.html", bills=rows, suppliers=_suppliers(), supplier_id=supplier_id,
                           rcm=rcm == "1")


def _bill_form(form=None, rows=None, order=None, status=200):
    return render_template(
        "purchases/bill_form.html", form=form or {}, rows=rows or [], suppliers=_suppliers(), gstins=_gstins(),
        locations=_locations(), catalog=_catalog_json(), itc_labels=_itc_choices(),
        slabs=[float(s) for s in GST_RATE_SLABS], order=order, today=date.today(), claims_itc=services.can_claim_itc(_tenant()),
    ), status


def _itc_choices():
    if services.can_claim_itc(_tenant()):
        return ITC_LABELS
    return {ItcEligibility.INELIGIBLE: "No ITC"}


def _rows_from_form(form):
    keys = ("product_id", "description", "hsn", "qty", "unit", "rate", "discount", "gst_rate", "itc",
            "batch", "expiry", "mfg", "mrp", "serials")
    cols = {k: form.getlist(f"line_{k}[]") for k in keys}
    rows = []
    for i in range(len(cols["description"])):
        row = {k: (cols[k][i] if i < len(cols[k]) else "") for k in keys}
        if row["description"].strip():
            rows.append(row)
    return rows


def _rows_from_order(order):
    return [{"product_id": str(l.product_id or ""), "description": l.description, "hsn": l.hsn_or_sac_code or "",
             "qty": f"{l.qty.normalize():f}", "unit": l.unit, "rate": f"{l.rate:.2f}", "discount": "",
             "gst_rate": f"{l.gst_rate.normalize():f}", "itc": "eligible"} for l in order.lines]


@purchases_bp.route("/new", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("purchases")
def new_bill():
    order = None
    order_id = _int(request.values.get("order"))
    if order_id:
        order = tenant_query(PurchaseOrder).filter_by(id=order_id).first_or_404()
        assert_owns(order)
    if request.method == "POST":
        form = request.form
        try:
            other = Decimal((form.get("other_charges") or "0").strip() or "0")
        except InvalidOperation:
            flash("Other charges must be a number.", "error")
            return _bill_form(form, _rows_from_form(form), order, 400)
        try:
            lines = services.parse_purchase_lines(form)
            attachment_id = save_private_document(request.files.get("attachment"))
            data = services.PurchaseInput(
                supplier_id=_int(form.get("supplier_id")), gstin_id=_int(form.get("gstin_id")),
                supplier_invoice_no=form.get("supplier_invoice_no"),
                supplier_invoice_date=_date(form.get("supplier_invoice_date")),
                location_id=_int(form.get("location_id")), lines=lines, bill_date=_date(form.get("bill_date")),
                is_rcm=bool(form.get("is_rcm")), other_charges=other, other_charges_label=form.get("other_charges_label"),
                notes=form.get("notes"), purchase_order_id=order.id if order else None, attachment_id=attachment_id,
            )
            bill = services.create_purchase_bill(_tenant(), current_user, data)
        except (services.PurchaseError, UploadError) as e:
            db.session.rollback()
            flash(e.message, "error")
            return _bill_form(form, _rows_from_form(form), order, 400)
        record_audit(current_user, "purchase_bill_posted", tenant_id=current_user.tenant_id, entity_type="purchase_bill",
                     entity_id=bill.id, details={"number": bill.number, "supplier": bill.supplier_snapshot.get("name"),
                                                "supplier_invoice_no": bill.supplier_invoice_no,
                                                "grand_total": str(bill.grand_total)})
        db.session.commit()
        flash(f"Purchase bill {bill.number} posted - stock received.", "success")
        return redirect(url_for("purchases.view_bill", bill_id=bill.id))
    form = {}
    if order is not None:
        form = {"supplier_id": str(order.supplier_id), "gstin_id": str(order.gstin_id)}
    return _bill_form(form, _rows_from_order(order) if order else [], order)


@purchases_bp.route("/<int:bill_id>")
@roles_required(ADMIN)
@feature_required("purchases")
def view_bill(bill_id):
    bill = tenant_query(PurchaseBill).filter_by(id=bill_id).first_or_404()
    assert_owns(bill)
    return render_template("purchases/bill_view.html", bill=bill, itc_labels=ITC_LABELS)


@purchases_bp.route("/<int:bill_id>/attachment")
@roles_required(ADMIN)
@feature_required("purchases")
def bill_attachment(bill_id):
    """The supplier's bill - private, served only to its own client."""
    bill = tenant_query(PurchaseBill).filter_by(id=bill_id).first_or_404()
    assert_owns(bill)
    if not bill.attachment_id:
        abort(404)
    record = db.session.get(UploadedImage, bill.attachment_id)
    if record is None:
        abort(404)
    ext = {"application/pdf": "pdf", "image/png": "png"}.get(record.content_type, "jpg")
    return Response(record.data, mimetype=record.content_type, headers={
        "Content-Disposition": f'inline; filename="{bill.number.replace("/", "-")}-supplier-bill.{ext}"',
        "Cache-Control": "private, no-store",
    })


@purchases_bp.route("/<int:bill_id>/void", methods=["POST"])
@roles_required(ADMIN)
@feature_required("purchases")
def void_bill(bill_id):
    bill = tenant_query(PurchaseBill).filter_by(id=bill_id).first_or_404()
    assert_owns(bill)
    try:
        services.void_purchase_bill(bill, current_user, request.form.get("reason"))
    except services.PurchaseError as e:
        db.session.rollback()
        flash(e.message, "error")
        return redirect(url_for("purchases.view_bill", bill_id=bill.id))
    record_audit(current_user, "purchase_bill_voided", tenant_id=current_user.tenant_id, entity_type="purchase_bill",
                 entity_id=bill.id, details={"number": bill.number, "reason": bill.void_reason})
    db.session.commit()
    flash(f"{bill.number} voided - its goods were taken back out of stock and its ITC dropped.", "success")
    return redirect(url_for("purchases.view_bill", bill_id=bill.id))


# --- returns to supplier (debit notes) -------------------------------------------------------

@purchases_bp.route("/<int:bill_id>/return", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("purchases")
def new_return(bill_id):
    bill = tenant_query(PurchaseBill).filter_by(id=bill_id).first_or_404()
    assert_owns(bill)
    lines = services.returnable(bill)
    if request.method == "POST":
        quantities = {}
        try:
            for line, _, _ in lines:
                raw = (request.form.get(f"qty_{line.id}") or "").strip()
                if raw:
                    quantities[line.id] = Decimal(raw)
        except InvalidOperation:
            flash("Quantities must be numbers.", "error")
            return render_template("purchases/return_form.html", bill=bill, lines=lines, form=request.form), 400
        try:
            ret = services.create_purchase_return(bill, current_user, quantities, request.form.get("reason"),
                                                  _date(request.form.get("return_date")))
        except services.PurchaseError as e:
            db.session.rollback()
            flash(e.message, "error")
            return render_template("purchases/return_form.html", bill=bill, lines=services.returnable(bill),
                                   form=request.form), 400
        record_audit(current_user, "purchase_return_posted", tenant_id=current_user.tenant_id,
                     entity_type="purchase_return", entity_id=ret.id,
                     details={"number": ret.number, "bill": bill.number, "grand_total": str(ret.grand_total)})
        db.session.commit()
        flash(f"Debit note {ret.number} posted - goods taken out of stock and ITC reversed.", "success")
        return redirect(url_for("purchases.view_return", return_id=ret.id))
    return render_template("purchases/return_form.html", bill=bill, lines=lines, form={})


@purchases_bp.route("/returns")
@roles_required(ADMIN)
@feature_required("purchases")
def returns():
    rows = tenant_query(PurchaseReturn).order_by(PurchaseReturn.return_date.desc(), PurchaseReturn.id.desc()).limit(300).all()
    return render_template("purchases/returns.html", returns=rows)


@purchases_bp.route("/returns/<int:return_id>")
@roles_required(ADMIN)
@feature_required("purchases")
def view_return(return_id):
    ret = tenant_query(PurchaseReturn).filter_by(id=return_id).first_or_404()
    assert_owns(ret)
    return render_template("purchases/return_view.html", ret=ret)


@purchases_bp.route("/returns/<int:return_id>/void", methods=["POST"])
@roles_required(ADMIN)
@feature_required("purchases")
def void_return(return_id):
    ret = tenant_query(PurchaseReturn).filter_by(id=return_id).first_or_404()
    assert_owns(ret)
    try:
        services.void_purchase_return(ret, current_user, request.form.get("reason"))
    except services.PurchaseError as e:
        db.session.rollback()
        flash(e.message, "error")
        return redirect(url_for("purchases.view_return", return_id=ret.id))
    record_audit(current_user, "purchase_return_voided", tenant_id=current_user.tenant_id, entity_type="purchase_return",
                 entity_id=ret.id, details={"number": ret.number, "reason": ret.void_reason})
    db.session.commit()
    flash(f"{ret.number} voided - the goods are back in stock.", "success")
    return redirect(url_for("purchases.view_return", return_id=ret.id))


# --- purchase orders ---------------------------------------------------------------------------

@purchases_bp.route("/orders")
@roles_required(ADMIN)
@feature_required("purchases")
def orders():
    rows = tenant_query(PurchaseOrder).order_by(PurchaseOrder.order_date.desc(), PurchaseOrder.id.desc()).limit(300).all()
    return render_template("purchases/orders.html", orders=rows)


@purchases_bp.route("/orders/new", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("purchases")
def new_order():
    if request.method == "POST":
        form = request.form
        try:
            lines = services.parse_purchase_lines(form)
            order = services.create_purchase_order(
                _tenant(), current_user, _int(form.get("supplier_id")), _int(form.get("gstin_id")), lines,
                order_date=_date(form.get("order_date")), expected_date=_date(form.get("expected_date")),
                notes=form.get("notes"),
            )
        except services.PurchaseError as e:
            db.session.rollback()
            flash(e.message, "error")
            return _order_form(form, _rows_from_form(form), 400)
        record_audit(current_user, "purchase_order_created", tenant_id=current_user.tenant_id,
                     entity_type="purchase_order", entity_id=order.id, details={"number": order.number})
        db.session.commit()
        flash(f"Purchase order {order.number} saved.", "success")
        return redirect(url_for("purchases.view_order", order_id=order.id))
    rows = []
    if request.args.get("reorder") and current_user.tenant.has_feature("inventory"):
        rows = [{"product_id": str(r.product.id), "description": r.product.name, "hsn": r.product.hsn_or_sac_code,
                 "qty": f"{r.suggested.normalize():f}", "unit": r.product.unit,
                 "rate": f"{(r.last_rate or r.product.purchase_rate or 0):.2f}",
                 "gst_rate": f"{r.product.gst_rate.normalize():f}"} for r in reorder_report(current_user.tenant_id)]
    return _order_form({}, rows)


def _order_form(form, rows, status=200):
    return render_template("purchases/order_form.html", form=form, rows=rows, suppliers=_suppliers(), gstins=_gstins(),
                           catalog=_catalog_json(), slabs=[float(s) for s in GST_RATE_SLABS], today=date.today()), status


@purchases_bp.route("/orders/<int:order_id>")
@roles_required(ADMIN)
@feature_required("purchases")
def view_order(order_id):
    order = tenant_query(PurchaseOrder).filter_by(id=order_id).first_or_404()
    assert_owns(order)
    return render_template("purchases/order_view.html", order=order)


@purchases_bp.route("/orders/<int:order_id>/cancel", methods=["POST"])
@roles_required(ADMIN)
@feature_required("purchases")
def cancel_order(order_id):
    order = tenant_query(PurchaseOrder).filter_by(id=order_id).first_or_404()
    assert_owns(order)
    try:
        services.cancel_purchase_order(order, request.form.get("reason"))
    except services.PurchaseError as e:
        flash(e.message, "error")
        return redirect(url_for("purchases.view_order", order_id=order.id))
    record_audit(current_user, "purchase_order_cancelled", tenant_id=current_user.tenant_id,
                 entity_type="purchase_order", entity_id=order.id, details={"number": order.number,
                                                                            "reason": order.cancel_reason})
    db.session.commit()
    flash(f"{order.number} cancelled.", "success")
    return redirect(url_for("purchases.view_order", order_id=order.id))


# --- GSTR-2B ---------------------------------------------------------------------------------------

@purchases_bp.route("/gstr2b", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("purchases")
def gstr2b_imports():
    gstins = [g for g in _gstins() if g.gstin]
    if request.method == "POST":
        gstin = next((g for g in gstins if g.id == _int(request.form.get("gstin_id"))), None)
        period = (request.form.get("period") or "").strip()
        upload = request.files.get("file")
        if gstin is None:
            flash("Pick the GSTIN this GSTR-2B is for.", "error")
            return redirect(url_for("purchases.gstr2b_imports"))
        if len(period) != 7 or period[4] != "-" or not (period[:4] + period[5:]).isdigit():
            flash("Pick the month of the GSTR-2B.", "error")
            return redirect(url_for("purchases.gstr2b_imports"))
        if not upload or not upload.filename:
            flash("Choose the GSTR-2B file (.json or .xlsx) downloaded from the GST portal.", "error")
            return redirect(url_for("purchases.gstr2b_imports"))
        try:
            source, rows = gstr2b.parse(upload.filename, upload.read())
            imp = gstr2b.save_import(_tenant(), gstin, period, upload.filename, source, rows, current_user)
        except gstr2b.Gstr2bError as e:
            db.session.rollback()
            flash(e.message, "error")
            return redirect(url_for("purchases.gstr2b_imports"))
        record_audit(current_user, "gstr2b_imported", tenant_id=current_user.tenant_id, entity_type="gstr2b_import",
                     entity_id=imp.id, details={"period": period, "gstin": gstin.gstin, "rows": len(imp.entries)})
        db.session.commit()
        flash(f"GSTR-2B for {period} read: {len(imp.entries)} supplier invoice(s).", "success")
        return redirect(url_for("purchases.gstr2b_view", import_id=imp.id))
    imports = tenant_query(Gstr2bImport).order_by(Gstr2bImport.period.desc(), Gstr2bImport.id.desc()).limit(60).all()
    return render_template("purchases/gstr2b_imports.html", imports=imports, gstins=gstins,
                           default_period=date.today().strftime("%Y-%m"))


@purchases_bp.route("/gstr2b/<int:import_id>")
@roles_required(ADMIN)
@feature_required("purchases")
def gstr2b_view(import_id):
    imp = tenant_query(Gstr2bImport).filter_by(id=import_id).first_or_404()
    assert_owns(imp)
    regular = [e for e in imp.entries if not e.is_rcm]
    groups = {status: [e for e in regular if e.status == status] for status in MatchStatus}
    rcm = [e for e in imp.entries if e.is_rcm]
    books = gstr2b.only_in_books(imp)
    candidates = (tenant_query(PurchaseBill).filter_by(gstin_id=imp.gstin_id, status=PurchaseStatus.POSTED)
                  .order_by(PurchaseBill.supplier_invoice_date.desc()).limit(500).all())
    return render_template("purchases/gstr2b_view.html", imp=imp, groups=groups, rcm=rcm, books=books,
                           books_regular=[b for b in books if not b.is_rcm], books_rcm=[b for b in books if b.is_rcm],
                           labels=MATCH_STATUS_LABELS, candidates=candidates, MatchStatus=MatchStatus)


def _entry(entry_id):
    entry = tenant_query(Gstr2bEntry).filter_by(id=entry_id).first_or_404()
    assert_owns(entry)
    return entry


@purchases_bp.route("/gstr2b/entries/<int:entry_id>/match", methods=["POST"])
@roles_required(ADMIN)
@feature_required("purchases")
def gstr2b_match(entry_id):
    entry = _entry(entry_id)
    bill = tenant_query(PurchaseBill).filter_by(id=_int(request.form.get("bill_id"))).first()
    if bill is None:
        flash("Pick the purchase bill to match.", "error")
        return redirect(url_for("purchases.gstr2b_view", import_id=entry.import_id))
    try:
        gstr2b.manual_match(entry, bill)
    except gstr2b.Gstr2bError as e:
        flash(e.message, "error")
        return redirect(url_for("purchases.gstr2b_view", import_id=entry.import_id))
    record_audit(current_user, "gstr2b_manual_match", tenant_id=current_user.tenant_id, entity_type="gstr2b_entry",
                 entity_id=entry.id, details={"invoice_no": entry.invoice_no, "bill": bill.number})
    db.session.commit()
    flash(f"{entry.invoice_no} matched to {bill.number}.", "success")
    return redirect(url_for("purchases.gstr2b_view", import_id=entry.import_id))


@purchases_bp.route("/gstr2b/entries/<int:entry_id>/unmatch", methods=["POST"])
@roles_required(ADMIN)
@feature_required("purchases")
def gstr2b_unmatch(entry_id):
    entry = _entry(entry_id)
    gstr2b.unmatch(entry)
    record_audit(current_user, "gstr2b_unmatched", tenant_id=current_user.tenant_id, entity_type="gstr2b_entry",
                 entity_id=entry.id, details={"invoice_no": entry.invoice_no})
    db.session.commit()
    flash(f"{entry.invoice_no} unmatched.", "success")
    return redirect(url_for("purchases.gstr2b_view", import_id=entry.import_id))


@purchases_bp.route("/gstr2b/<int:import_id>/rematch", methods=["POST"])
@roles_required(ADMIN)
@feature_required("purchases")
def gstr2b_rematch(import_id):
    """Run auto-matching again - after entering bills that were missing."""
    imp = tenant_query(Gstr2bImport).filter_by(id=import_id).first_or_404()
    assert_owns(imp)
    gstr2b.auto_match(imp)
    db.session.commit()
    flash("Matched again against your purchase bills.", "success")
    return redirect(url_for("purchases.gstr2b_view", import_id=imp.id))
