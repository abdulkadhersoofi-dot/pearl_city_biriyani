import re
from datetime import date
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from flask import abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.auth.forms import SetPasswordForm
from app.extensions import db
from app.invoicing.pdf import render_invoice_pdf
from app.models.audit_log import AuditLog
from app.models.invoice import Invoice
from app.models.invoice_series import DOCUMENT_TYPE_LABELS
from app.models.note import CreditDebitNote
from app.models.pos_bill import POSBill
from app.models.tenant import Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.reports.export import build_gstr1_workbook, build_gstr3b_workbook
from app.reports.gstr import ReportPeriodError, gstr1_data, gstr3b_data
from app.tenants import tenants_bp
from app.tenants.forms import AddGstinForm, OnboardClientForm
from app.utils.audit import record_audit
from app.utils.indian_states import STATE_NAME_BY_CODE
from app.utils.uploads import UploadError, delete_uploaded_image, save_uploaded_image


def _make_login_slug(legal_name: str, tenant_id: int) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", legal_name.lower()).strip("-") or "client"
    return f"{base}-{tenant_id}"


@tenants_bp.route("/clients")
@roles_required(UserRole.SUPER_ADMIN)
def directory():
    tenants = Tenant.query.order_by(Tenant.legal_name).all()
    return render_template("tenants/directory.html", tenants=tenants)


@tenants_bp.route("/clients/new", methods=["GET", "POST"])
@roles_required(UserRole.SUPER_ADMIN)
def onboard():
    form = OnboardClientForm()
    if form.validate_on_submit():
        existing = User.query.filter_by(email=form.admin_email.data.strip().lower()).first()
        if existing:
            flash("A user with that email already exists.", "error")
            return render_template("tenants/onboard.html", form=form)

        tenant = Tenant(
            legal_name=form.legal_name.data.strip(),
            trade_name=(form.trade_name.data or "").strip() or None,
            registration_type=RegistrationType(form.registration_type.data),
            onboarded_by_id=current_user.id,
        )
        db.session.add(tenant)
        db.session.flush()

        tenant.login_slug = _make_login_slug(tenant.legal_name, tenant.id)
        try:
            tenant.logo_image_id = save_uploaded_image(form.logo.data)
        except UploadError as exc:
            flash(exc.message, "error")
            return render_template("tenants/onboard.html", form=form)

        # Always create a business-location row, GSTIN or not - an
        # unregistered client still has a state/address, it just has no
        # GSTIN number. Without this row, POS checkout and invoicing have
        # nothing to bill from for an unregistered client.
        gstin = Gstin(
            tenant_id=tenant.id,
            gstin=form.gstin.data,  # already normalized/validated by the form field
            state_code=form.state_code.data,
            state_name=STATE_NAME_BY_CODE.get(form.state_code.data, ""),
            registered_address=form.registered_address.data,
            is_primary=True,
        )
        db.session.add(gstin)

        admin_user = User(
            tenant_id=tenant.id,
            name=form.admin_name.data.strip(),
            email=form.admin_email.data.strip().lower(),
            phone=form.admin_phone.data,
            role=UserRole.CLIENT_ADMIN,
            must_change_password=True,
        )
        admin_user.set_password(form.admin_password.data)
        db.session.add(admin_user)
        db.session.flush()

        record_audit(
            current_user,
            "client_onboarded",
            tenant_id=tenant.id,
            entity_type="tenant",
            entity_id=tenant.id,
        )
        db.session.commit()

        flash(
            f"{tenant.legal_name} onboarded. Share the password you set with "
            f"{admin_user.email} - they'll be asked to change it on first sign-in.",
            "success",
        )
        return redirect(url_for("tenants.directory"))

    return render_template("tenants/onboard.html", form=form)


@tenants_bp.route("/clients/<int:tenant_id>")
@roles_required(UserRole.SUPER_ADMIN)
def detail(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    record_audit(
        current_user,
        "view_client_data",
        tenant_id=tenant.id,
        entity_type="tenant",
        entity_id=tenant.id,
    )
    db.session.commit()

    invoices = (
        Invoice.query.filter_by(tenant_id=tenant.id)
        .order_by(Invoice.invoice_date.desc())
        .limit(50)
        .all()
    )
    pos_bills = (
        POSBill.query.filter_by(tenant_id=tenant.id)
        .filter(POSBill.bill_number.isnot(None))
        .order_by(POSBill.completed_at.desc())
        .limit(50)
        .all()
    )
    notes = (
        CreditDebitNote.query.filter_by(tenant_id=tenant.id)
        .order_by(CreditDebitNote.note_date.desc())
        .limit(50)
        .all()
    )
    users = User.query.filter_by(tenant_id=tenant.id).all()
    gstin_form = AddGstinForm()
    return render_template(
        "tenants/detail.html",
        tenant=tenant,
        invoices=invoices,
        pos_bills=pos_bills,
        notes=notes,
        users=users,
        gstin_form=gstin_form,
    )


@tenants_bp.route("/clients/<int:tenant_id>/invoices/<int:invoice_id>")
@roles_required(UserRole.SUPER_ADMIN)
def admin_view_invoice(tenant_id, invoice_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    invoice = Invoice.query.filter_by(id=invoice_id, tenant_id=tenant_id).first_or_404()
    record_audit(
        current_user, "view_client_invoice", tenant_id=tenant_id, entity_type="invoice", entity_id=invoice.id
    )
    db.session.commit()
    return render_template(
        "tenants/view_invoice.html",
        tenant=tenant,
        invoice=invoice,
        document_label=DOCUMENT_TYPE_LABELS[invoice.document_type],
    )


@tenants_bp.route("/clients/<int:tenant_id>/invoices/<int:invoice_id>/pdf")
@roles_required(UserRole.SUPER_ADMIN)
def admin_invoice_pdf(tenant_id, invoice_id):
    invoice = Invoice.query.filter_by(id=invoice_id, tenant_id=tenant_id).first_or_404()
    pdf_bytes = render_invoice_pdf(invoice)
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"{invoice.invoice_number.replace('/', '-')}.pdf",
    )


@tenants_bp.route("/clients/<int:tenant_id>/logo", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def update_logo(tenant_id):
    # Lets the firm set/replace a client's logo on their behalf - clients
    # onboarded before this feature existed otherwise have no way to get
    # one in without a Client Admin visiting Settings themselves.
    tenant = Tenant.query.get_or_404(tenant_id)
    try:
        new_logo_id = save_uploaded_image(request.files.get("logo"))
    except UploadError as exc:
        flash(exc.message, "error")
        return redirect(url_for("tenants.detail", tenant_id=tenant.id))

    if new_logo_id:
        old_logo_id = tenant.logo_image_id
        tenant.logo_image_id = new_logo_id
        db.session.flush()
        delete_uploaded_image(old_logo_id)
        if not tenant.login_slug:
            tenant.login_slug = _make_login_slug(tenant.legal_name, tenant.id)
        record_audit(
            current_user, "tenant_logo_updated", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id
        )
        db.session.commit()
        flash(f"Logo updated for {tenant.legal_name}.", "success")
    else:
        flash("Choose an image file first.", "error")

    return redirect(url_for("tenants.detail", tenant_id=tenant.id))


@tenants_bp.route("/clients/<int:tenant_id>/gstins/new", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def add_gstin(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    form = AddGstinForm()
    if form.validate_on_submit():
        gstin = Gstin(
            tenant_id=tenant.id,
            gstin=form.gstin.data,  # already normalized/validated by the form field
            state_code=form.state_code.data,
            state_name=STATE_NAME_BY_CODE.get(form.state_code.data, ""),
            registered_address=form.registered_address.data,
            is_primary=tenant.gstins.count() == 0,
        )
        db.session.add(gstin)
        record_audit(
            current_user, "tenant_gstin_added", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id
        )
        db.session.commit()
        flash("Business location added.", "success")
    else:
        flash("Could not add that location - check the state and GSTIN format.", "error")
    return redirect(url_for("tenants.detail", tenant_id=tenant.id))


@tenants_bp.route("/clients/<int:tenant_id>/deactivate", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def deactivate(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    tenant.is_active = False
    record_audit(
        current_user, "client_deactivated", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id
    )
    db.session.commit()
    flash(f"{tenant.legal_name} deactivated.", "success")
    return redirect(url_for("tenants.detail", tenant_id=tenant.id))


@tenants_bp.route("/clients/<int:tenant_id>/activate", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def activate(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    tenant.is_active = True
    record_audit(
        current_user, "client_activated", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id
    )
    db.session.commit()
    flash(f"{tenant.legal_name} reactivated.", "success")
    return redirect(url_for("tenants.detail", tenant_id=tenant.id))


@tenants_bp.route("/clients/<int:tenant_id>/users/<int:user_id>/reset-password", methods=["GET", "POST"])
@roles_required(UserRole.SUPER_ADMIN)
def reset_client_password(tenant_id, user_id):
    user = User.query.filter_by(id=user_id, tenant_id=tenant_id).first_or_404()
    form = SetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.new_password.data)
        user.must_change_password = True
        record_audit(
            current_user,
            "client_password_reset_by_firm",
            tenant_id=tenant_id,
            entity_type="user",
            entity_id=user.id,
        )
        db.session.commit()
        flash(f"Password updated for {user.email}. Share it with them directly.", "success")
        return redirect(url_for("tenants.detail", tenant_id=tenant_id))
    return render_template("tenants/reset_password.html", form=form, tenant_user=user, tenant_id=tenant_id)


@tenants_bp.route("/audit-log")
@roles_required(UserRole.SUPER_ADMIN)
def audit_log():
    logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(200).all()
    return render_template("tenants/audit_log.html", logs=logs)


def _current_period() -> str:
    today = date.today()
    return f"{today.year:04d}-{today.month:02d}"


def _exportable_tenants():
    """Regular-scheme tenants with at least one active GSTIN - the only
    ones GSTR-1/3B applies to, and the only ones with anything to bill
    a report from."""
    return [
        t
        for t in Tenant.query.filter_by(registration_type=RegistrationType.REGULAR).order_by(Tenant.legal_name).all()
        if t.gstins.filter_by(is_active=True).count() > 0
    ]


@tenants_bp.route("/gst-exports")
@roles_required(UserRole.SUPER_ADMIN)
def gst_exports():
    return render_template(
        "tenants/gst_exports.html", tenants=_exportable_tenants(), period=_current_period()
    )


@tenants_bp.route("/gst-exports/download", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def gst_exports_download():
    period = request.form.get("period", _current_period())
    tenant_ids = {int(v) for v in request.form.getlist("tenant_ids") if v.isdigit()}
    eligible = {t.id: t for t in _exportable_tenants()}
    selected = [eligible[tid] for tid in tenant_ids if tid in eligible]

    if not selected:
        flash("Pick at least one client to export.", "error")
        return redirect(url_for("tenants.gst_exports"))

    buf = BytesIO()
    with ZipFile(buf, "w", ZIP_DEFLATED) as zf:
        for tenant in selected:
            gstin = tenant.gstins.filter_by(is_primary=True, is_active=True).first() or tenant.gstins.filter_by(is_active=True).first()
            safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in tenant.legal_name).strip() or f"tenant-{tenant.id}"
            try:
                g1 = gstr1_data(tenant, gstin, period)
                g3b = gstr3b_data(tenant, gstin, period)
            except ReportPeriodError as exc:
                flash(exc.message, "error")
                return redirect(url_for("tenants.gst_exports"))
            zf.writestr(f"{safe_name}/GSTR1_{period}.xlsx", build_gstr1_workbook(g1).getvalue())
            zf.writestr(f"{safe_name}/GSTR3B_{period}.xlsx", build_gstr3b_workbook(g3b).getvalue())
    buf.seek(0)

    record_audit(
        current_user,
        "bulk_gst_export",
        details={"period": period, "tenant_count": len(selected), "tenant_ids": sorted(t.id for t in selected)},
    )
    db.session.commit()

    return send_file(
        buf, as_attachment=True, download_name=f"GST_exports_{period}.zip", mimetype="application/zip"
    )
