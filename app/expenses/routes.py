from datetime import date
from io import BytesIO

from flask import Response, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.expenses import expenses_bp, services
from app.expenses.reports import build_expense_workbook, expense_report_data, render_expense_pdf
from app.extensions import db
from app.models.expense import (
    FREQUENCY_LABELS,
    PAYMENT_MODE_LABELS,
    Expense,
    ExpenseCategory,
    ExpensePaymentMode,
    ExpenseStatus,
    Frequency,
    RecurringExpense,
)
from app.models.media import UploadedImage
from app.models.purchase import ITC_LABELS, ItcEligibility, Supplier
from app.models.tenant import Gstin
from app.models.user import UserRole
from app.purchases.services import can_claim_itc
from app.reports.filters import branch_choices, parse_report_filters
from app.utils.audit import record_audit
from app.utils.features import feature_required
from app.utils.gst_config import GST_RATE_SLABS
from app.utils.tenant_scope import assert_owns, tenant_query
from app.utils.uploads import UploadError, save_private_document

ADMIN = UserRole.CLIENT_ADMIN
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


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


def _audit(action, expense, **details):
    record_audit(current_user, action, tenant_id=current_user.tenant_id, entity_type="expense", entity_id=expense.id,
                 details={"number": expense.number, "amount": str(expense.amount), **details})


def _expense(expense_id) -> Expense:
    expense = tenant_query(Expense).filter_by(id=expense_id).first_or_404()
    assert_owns(expense)
    return expense


def _categories():
    """The client's categories - seeded with the standard list on first
    use, and committed straight away so the ids on the page are real."""
    rows = services.ensure_categories(current_user.tenant_id)
    db.session.commit()
    return rows


def _form_context(form=None, expense=None):
    tenant = current_user.tenant
    return {
        "form": form or {},
        "expense": expense,
        "categories": _categories(),
        "suppliers": tenant_query(Supplier).filter_by(is_active=True).order_by(Supplier.name).all()
        if tenant.has_feature("purchases") else [],
        "branches": branch_choices(tenant),
        "gstins": tenant_query(Gstin).filter_by(is_active=True).order_by(Gstin.is_primary.desc(), Gstin.id).all(),
        "modes": PAYMENT_MODE_LABELS,
        "slabs": [f"{s.normalize():f}" for s in GST_RATE_SLABS],
        "itc_labels": ITC_LABELS if can_claim_itc(tenant) else {ItcEligibility.INELIGIBLE: "No ITC"},
        "claims_itc": can_claim_itc(tenant),
        "today": date.today(),
    }


def _form_from_expense(e: Expense) -> dict:
    """A draft's saved values in the shape the form posts them."""
    return {
        "expense_date": e.expense_date.isoformat(), "category_id": str(e.category_id),
        "payee_name": e.payee_name or "", "supplier_id": str(e.supplier_id or ""),
        "payment_mode": e.payment_mode.value, "reference": e.reference or "",
        "branch_user_id": str(e.branch_user_id or ""), "notes": e.notes or "",
        "amount": f"{e.amount:.2f}",  # the bill total, or under RCM the taxable value
        "is_gst_bill": "1" if e.is_gst_bill else "", "gstin_id": str(e.gstin_id or ""),
        "supplier_gstin": e.supplier_gstin or "", "bill_number": e.bill_number or "",
        "gst_rate": f"{e.gst_rate.normalize():f}", "amount_includes_gst": "1" if not e.is_rcm else "",
        "is_rcm": "1" if e.is_rcm else "", "itc_eligibility": e.itc_eligibility.value,
    }


def _input_from_form(form) -> services.ExpenseInput:
    try:
        mode = ExpensePaymentMode(form.get("payment_mode") or "cash")
        eligibility = ItcEligibility(form.get("itc_eligibility") or "eligible")
    except ValueError:
        raise services.ExpenseError("Pick how it was paid.")
    is_gst = bool(form.get("is_gst_bill"))
    return services.ExpenseInput(
        expense_date=_date(form.get("expense_date")),
        category_id=_int(form.get("category_id")),
        amount=services.money(form.get("amount") or "0", "Amount"),
        payment_mode=mode,
        payee_name=form.get("payee_name"),
        supplier_id=_int(form.get("supplier_id")),
        reference=form.get("reference"),
        branch_user_id=_int(form.get("branch_user_id")),
        notes=form.get("notes"),
        is_gst_bill=is_gst,
        gstin_id=_int(form.get("gstin_id")),
        supplier_gstin=form.get("supplier_gstin") if is_gst else None,
        bill_number=form.get("bill_number") if is_gst else None,
        gst_rate=services.money(form.get("gst_rate") or "0", "GST rate") if is_gst else 0,
        amount_includes_gst=bool(form.get("amount_includes_gst")),
        is_rcm=bool(form.get("is_rcm")) if is_gst else False,
        itc_eligibility=eligibility,
    )


# --- list --------------------------------------------------------------------------------

@expenses_bp.route("/")
@roles_required(ADMIN)
@feature_required("expenses")
def index():
    tenant = current_user.tenant
    made = services.generate_due_drafts(tenant)
    if made:
        for draft in made:
            record_audit(current_user, "expense_draft_generated", tenant_id=tenant.id, entity_type="expense",
                         entity_id=draft.id, details={"recurring": draft.recurring.name, "due": draft.due_date.isoformat()})
    categories = _categories()
    status = request.args.get("status") or "posted"
    if status not in ("posted", "draft", "voided"):
        status = "posted"
    query = tenant_query(Expense).filter_by(status=ExpenseStatus(status))
    category_id = _int(request.args.get("category"))
    if category_id:
        query = query.filter_by(category_id=category_id)
    q = (request.args.get("q") or "").strip()
    if q:
        query = query.filter(db.or_(Expense.payee_name.ilike(f"%{q}%"), Expense.number.ilike(f"%{q}%"),
                                    Expense.notes.ilike(f"%{q}%")))
    rows = query.order_by(Expense.expense_date.desc(), Expense.id.desc()).limit(500).all()
    drafts = tenant_query(Expense).filter_by(status=ExpenseStatus.DRAFT).count()
    this_month = date.today().replace(day=1)
    month_total = (tenant_query(Expense).with_entities(db.func.coalesce(db.func.sum(Expense.amount), 0))
                   .filter(Expense.status == ExpenseStatus.POSTED, Expense.expense_date >= this_month).scalar())
    return render_template("expenses/index.html", rows=rows, status=status, drafts=drafts, q=q,
                           category_id=category_id, categories=categories,
                           month_total=month_total, new_drafts=len(made))


# --- enter / view / post / void ------------------------------------------------------------

@expenses_bp.route("/new", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("expenses")
def new_expense():
    if request.method == "POST":
        form = request.form
        try:
            data = _input_from_form(form)
            data.attachment_id = save_private_document(request.files.get("attachment"))
            expense = services.create_expense(current_user.tenant, current_user, data,
                                              post=form.get("action") != "draft")
        except (services.ExpenseError, UploadError) as exc:
            db.session.rollback()
            flash(exc.message, "error")
            return render_template("expenses/form.html", **_form_context(form)), 400
        _audit("expense_posted" if expense.number else "expense_draft_saved", expense, category=expense.category.name)
        db.session.commit()
        flash(f"Expense {expense.number} saved." if expense.number else "Draft saved - post it when it's paid.", "success")
        return redirect(url_for("expenses.view_expense", expense_id=expense.id))
    return render_template("expenses/form.html", **_form_context({"expense_date": date.today().isoformat(),
                                                                  "amount_includes_gst": "1"}))


@expenses_bp.route("/<int:expense_id>")
@roles_required(ADMIN)
@feature_required("expenses")
def view_expense(expense_id):
    expense = _expense(expense_id)
    return render_template("expenses/view.html", expense=expense, modes=PAYMENT_MODE_LABELS, itc_labels=ITC_LABELS)


@expenses_bp.route("/<int:expense_id>/edit", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("expenses")
def edit_expense(expense_id):
    expense = _expense(expense_id)
    if expense.status != ExpenseStatus.DRAFT:
        flash("Only a draft can be changed - void a posted expense and enter it again.", "error")
        return redirect(url_for("expenses.view_expense", expense_id=expense.id))
    if request.method == "POST":
        form = request.form
        try:
            data = _input_from_form(form)
            data.attachment_id = save_private_document(request.files.get("attachment"))
            services.update_draft(expense, current_user.tenant, data)
            if form.get("action") != "draft":
                services.post_expense(expense, current_user)
        except (services.ExpenseError, UploadError) as exc:
            db.session.rollback()
            flash(exc.message, "error")
            return render_template("expenses/form.html", **_form_context(form, expense)), 400
        _audit("expense_posted" if expense.number else "expense_draft_saved", expense, category=expense.category.name)
        db.session.commit()
        flash(f"Expense {expense.number} posted." if expense.number else "Draft saved.", "success")
        return redirect(url_for("expenses.view_expense", expense_id=expense.id))
    return render_template("expenses/form.html", **_form_context(_form_from_expense(expense), expense))


@expenses_bp.route("/<int:expense_id>/post", methods=["POST"])
@roles_required(ADMIN)
@feature_required("expenses")
def post_expense(expense_id):
    expense = _expense(expense_id)
    try:
        services.post_expense(expense, current_user)
    except services.ExpenseError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        return redirect(url_for("expenses.view_expense", expense_id=expense.id))
    _audit("expense_posted", expense, category=expense.category.name)
    db.session.commit()
    flash(f"Expense {expense.number} posted.", "success")
    return redirect(url_for("expenses.view_expense", expense_id=expense.id))


@expenses_bp.route("/<int:expense_id>/void", methods=["POST"])
@roles_required(ADMIN)
@feature_required("expenses")
def void_expense(expense_id):
    expense = _expense(expense_id)
    was_draft = expense.status == ExpenseStatus.DRAFT
    try:
        services.void_expense(expense, current_user, request.form.get("reason"))
    except services.ExpenseError as exc:
        flash(exc.message, "error")
        return redirect(url_for("expenses.view_expense", expense_id=expense.id))
    _audit("expense_draft_skipped" if was_draft else "expense_voided", expense, reason=expense.void_reason)
    db.session.commit()
    flash("Draft skipped." if was_draft else f"{expense.number} voided.", "success")
    return redirect(url_for("expenses.view_expense", expense_id=expense.id))


@expenses_bp.route("/<int:expense_id>/attachment")
@roles_required(ADMIN)
@feature_required("expenses")
def expense_attachment(expense_id):
    """The bill photo / PDF - private, served only to its own client."""
    expense = _expense(expense_id)
    record = db.session.get(UploadedImage, expense.attachment_id) if expense.attachment_id else None
    if record is None:
        abort(404)
    ext = {"application/pdf": "pdf", "image/png": "png"}.get(record.content_type, "jpg")
    name = (expense.number or f"draft-{expense.id}").replace("/", "-")
    return Response(record.data, mimetype=record.content_type, headers={
        "Content-Disposition": f'inline; filename="{name}-bill.{ext}"', "Cache-Control": "private, no-store",
    })


# --- categories ----------------------------------------------------------------------------

@expenses_bp.route("/categories", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("expenses")
def categories():
    tenant_id = current_user.tenant_id
    if request.method == "POST":
        try:
            category = services.add_category(tenant_id, request.form.get("name"))
        except services.ExpenseError as exc:
            db.session.rollback()
            flash(exc.message, "error")
            return redirect(url_for("expenses.categories"))
        record_audit(current_user, "expense_category_added", tenant_id=tenant_id, entity_type="expense_category",
                     entity_id=category.id, details={"name": category.name})
        db.session.commit()
        flash(f"{category.name} added.", "success")
        return redirect(url_for("expenses.categories"))
    _categories()
    rows = tenant_query(ExpenseCategory).order_by(ExpenseCategory.is_active.desc(), ExpenseCategory.name).all()
    counts = dict(tenant_query(Expense).with_entities(Expense.category_id, db.func.count(Expense.id))
                  .filter(Expense.status == ExpenseStatus.POSTED).group_by(Expense.category_id).all())
    return render_template("expenses/categories.html", rows=rows, counts=counts)


@expenses_bp.route("/categories/<int:category_id>", methods=["POST"])
@roles_required(ADMIN)
@feature_required("expenses")
def update_category(category_id):
    category = tenant_query(ExpenseCategory).filter_by(id=category_id).first_or_404()
    assert_owns(category)
    action = request.form.get("action")
    try:
        if action == "rename":
            old = category.name
            services.rename_category(category, request.form.get("name"))
            details = {"from": old, "to": category.name}
        elif action == "off":
            if tenant_query(ExpenseCategory).filter_by(is_active=True).count() <= 1:
                raise services.ExpenseError("Keep at least one category.")
            category.is_active, details = False, {"name": category.name, "active": False}
        elif action == "on":
            category.is_active, details = True, {"name": category.name, "active": True}
        else:
            abort(400)
    except services.ExpenseError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        return redirect(url_for("expenses.categories"))
    record_audit(current_user, "expense_category_changed", tenant_id=current_user.tenant_id,
                 entity_type="expense_category", entity_id=category.id, details=details)
    db.session.commit()
    flash("Category updated.", "success")
    return redirect(url_for("expenses.categories"))


# --- recurring -----------------------------------------------------------------------------

def _recurring_input(form) -> services.RecurringInput:
    try:
        frequency = Frequency(form.get("frequency") or "monthly")
        mode = ExpensePaymentMode(form.get("payment_mode") or "bank")
    except ValueError:
        raise services.ExpenseError("Pick how often and how it's paid.")
    return services.RecurringInput(
        name=form.get("name"), category_id=_int(form.get("category_id")),
        amount=services.money(form.get("amount") or "0", "Amount"), frequency=frequency,
        start_date=_date(form.get("start_date")), end_date=_date(form.get("end_date")), payment_mode=mode,
        payee_name=form.get("payee_name"), supplier_id=_int(form.get("supplier_id")),
        branch_user_id=_int(form.get("branch_user_id")), notes=form.get("notes"),
    )


def _recurring_context(form, recurring=None):
    ctx = _form_context(form)
    ctx.update(recurring=recurring, frequencies=FREQUENCY_LABELS)
    return ctx


@expenses_bp.route("/recurring")
@roles_required(ADMIN)
@feature_required("expenses")
def recurring():
    rows = tenant_query(RecurringExpense).order_by(RecurringExpense.is_active.desc(), RecurringExpense.name).all()
    return render_template("expenses/recurring.html", rows=rows, frequencies=FREQUENCY_LABELS, modes=PAYMENT_MODE_LABELS)


@expenses_bp.route("/recurring/new", methods=["GET", "POST"])
@expenses_bp.route("/recurring/<int:recurring_id>", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("expenses")
def edit_recurring(recurring_id=None):
    rec = None
    if recurring_id is not None:
        rec = tenant_query(RecurringExpense).filter_by(id=recurring_id).first_or_404()
        assert_owns(rec)
    if request.method == "POST":
        try:
            rec = services.save_recurring(current_user.tenant, current_user, _recurring_input(request.form), rec)
        except services.ExpenseError as exc:
            db.session.rollback()
            flash(exc.message, "error")
            return render_template("expenses/recurring_form.html", **_recurring_context(request.form, rec)), 400
        record_audit(current_user, "recurring_expense_saved", tenant_id=current_user.tenant_id,
                     entity_type="recurring_expense", entity_id=rec.id,
                     details={"name": rec.name, "amount": str(rec.amount), "frequency": rec.frequency.value})
        db.session.commit()
        flash(f"{rec.name} saved. A draft appears under Expenses each time it falls due.", "success")
        return redirect(url_for("expenses.recurring"))
    form = {"start_date": date.today().isoformat(), "frequency": "monthly", "payment_mode": "bank"}
    if rec is not None:
        form = {"name": rec.name, "category_id": str(rec.category_id), "amount": f"{rec.amount:.2f}",
                "frequency": rec.frequency.value, "start_date": rec.start_date.isoformat(),
                "end_date": rec.end_date.isoformat() if rec.end_date else "", "payment_mode": rec.payment_mode.value,
                "payee_name": rec.payee_name or "", "supplier_id": str(rec.supplier_id or ""),
                "branch_user_id": str(rec.branch_user_id or ""), "notes": rec.notes or ""}
    return render_template("expenses/recurring_form.html", **_recurring_context(form, rec))


@expenses_bp.route("/recurring/<int:recurring_id>/toggle", methods=["POST"])
@roles_required(ADMIN)
@feature_required("expenses")
def toggle_recurring(recurring_id):
    rec = tenant_query(RecurringExpense).filter_by(id=recurring_id).first_or_404()
    assert_owns(rec)
    rec.is_active = not rec.is_active
    if rec.is_active and rec.next_due_date < date.today():
        # Restarting doesn't back-fill the periods it was stopped for.
        while rec.next_due_date < date.today():
            rec.next_due_date = services.next_due(rec.next_due_date, rec.frequency, rec.start_date.day)
    record_audit(current_user, "recurring_expense_toggled", tenant_id=current_user.tenant_id,
                 entity_type="recurring_expense", entity_id=rec.id, details={"name": rec.name, "active": rec.is_active})
    db.session.commit()
    flash(f"{rec.name} {'restarted' if rec.is_active else 'stopped'}.", "success")
    return redirect(url_for("expenses.recurring"))


# --- report ----------------------------------------------------------------------------------

def _report_data():
    filters = parse_report_filters(request.args)
    category_ids = [int(v) for v in request.args.getlist("category_id") if v.isdigit()] or None
    data = expense_report_data(current_user.tenant, filters.start, filters.end, filters.branch_ids, category_ids)
    return filters, category_ids, data


@expenses_bp.route("/report")
@roles_required(ADMIN)
@feature_required("expenses")
def report():
    filters, category_ids, data = _report_data()
    args = {k: v for k, v in request.args.to_dict(flat=False).items() if k != "category_id"}
    return render_template("expenses/report.html", data=data, filters=filters, category_ids=category_ids or [],
                           all_categories_url=url_for("expenses.report", **args),
                           branches=branch_choices(current_user.tenant),
                           categories=_categories())


@expenses_bp.route("/report.xlsx")
@roles_required(ADMIN)
@feature_required("expenses")
def report_xlsx():
    filters, _, data = _report_data()
    return send_file(build_expense_workbook(data), as_attachment=True, mimetype=XLSX,
                     download_name=f"Expenses_{filters.start.isoformat()}_to_{filters.end.isoformat()}.xlsx")


@expenses_bp.route("/report.pdf")
@roles_required(ADMIN)
@feature_required("expenses")
def report_pdf():
    filters, _, data = _report_data()
    return send_file(BytesIO(render_expense_pdf(data)), mimetype="application/pdf",
                     download_name=f"Expenses_{filters.start.isoformat()}_to_{filters.end.isoformat()}.pdf")
