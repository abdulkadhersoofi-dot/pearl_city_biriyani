import re
from datetime import date
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from flask import abort, flash, redirect, render_template, request, send_file, session, url_for
from flask_login import current_user, login_user

from app.auth.decorators import admin_or_auditor_required, roles_required
from app.auth.forms import SetPasswordForm
from app.extensions import db
from app.invoicing.pdf import render_invoice_pdf
from app.models.audit_log import AuditLog
from app.models.invoice import Invoice
from app.models.invoice_series import DOCUMENT_TYPE_LABELS
from app.models.note import CreditDebitNote
from app.models.pos_bill import POSBill
from app.models.tenant import BillingCycle, BusinessType, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.reports.gstr import ReportPeriodError
from app.tenants import tenants_bp
from app.tenants.forms import (
    AddGstinForm,
    BillingCycleForm,
    CreateAuditorForm,
    OnboardClientForm,
)
from app.utils.audit import record_audit
from app.utils.auditor_scope import (
    assert_auditor_owns_auditor,
    assert_auditor_owns_tenant,
    auditor_tenant_ids,
    visible_auditors,
)
from app.utils.billing import (
    auditor_access_status,
    mark_period_paid,
    next_due_after,
    tenant_access_status,
)
from app.utils.features import apply_business_type_preset
from app.models.package import Package
from app.tenants.packages import client_package_context
from app.tenants.powers import auditor_powers_context, may_set_powers
from app.utils.changes import auditor_changes, client_changes
from app.utils.entitlements import (
    BUSINESS_TYPE_PACKAGE, allowed_packages, assign_package, branch_login_context, client_room,
    ensure_starting_packages, grant_of, has_room, limit_meter, limit_of, may_add, package_price_for, power_required,
    sub_auditor_room,
)
from app.utils.indian_states import STATE_NAME_BY_CODE
from app.utils.uploads import UploadError, delete_uploaded_image, save_uploaded_image


def _make_login_slug(legal_name: str, tenant_id: int) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", legal_name.lower()).strip("-") or "client"
    return f"{base}-{tenant_id}"


def _scoped_tenant_query():
    ids = auditor_tenant_ids(current_user)
    query = Tenant.query
    if ids is not None:
        query = query.filter(Tenant.id.in_(ids)) if ids else query.filter(db.false())
    return query


@tenants_bp.route("/clients")
@admin_or_auditor_required
def directory():
    """Every client in scope, narrowed by one row of filter chips (console
    cleanup, stage 2): all, active, renewal due, can't sign in, waiting for
    verification and - for the Ultra Admin - no auditor. `?show=` opens it
    on one of them (the Today page links here that way)."""
    tenants = _scoped_tenant_query().order_by(Tenant.legal_name).all()
    access_statuses = {t.id: tenant_access_status(t) for t in tenants}
    filters = ["all", "active", "renewal", "blocked", "pending"] + (["no_auditor"] if current_user.is_super_admin else [])
    show = request.args.get("show")
    return render_template("tenants/directory.html", tenants=tenants, access_statuses=access_statuses,
                           filters=filters, show=show if show in filters else "all")


@tenants_bp.route("/clients/new", methods=["GET", "POST"])
@admin_or_auditor_required
@power_required("onboard_clients")
def onboard():
    form = OnboardClientForm()
    if not has_room(client_room(current_user)):
        count, limit = client_room(current_user)
        flash(f"You have {count} clients - your limit is {limit}. Ask ARFA to raise it.", "error")
        return redirect(url_for("tenants.directory"))
    if form.validate_on_submit():
        existing = User.query.filter_by(email=form.admin_email.data.strip().lower()).first()
        if existing:
            flash("A user with that email already exists.", "error")
            return render_template("tenants/onboard.html", form=form, **_onboarding_packages(form))

        tenant = Tenant(
            legal_name=form.legal_name.data.strip(),
            trade_name=(form.trade_name.data or "").strip() or None,
            registration_type=RegistrationType(form.registration_type.data),
            onboarded_by_id=current_user.id,
            # Allocated straight to whichever Auditor/Sub-Auditor created
            # it; null (unallocated) when the Ultra Admin creates it
            # directly. Billing cycle is deliberately never set here -
            # every new client starts pending verification regardless of
            # who created it (see app.utils.billing.tenant_access_status)
            # and only the Ultra Admin's set_billing_cycle sets it.
            auditor_id=current_user.id if current_user.is_admin_hierarchy and not current_user.is_super_admin else None,
        )
        # The submitted value, not .data - an absent field would otherwise
        # fall back to the form's RETAIL default and silently switch
        # modules on; absent means OTHER (nothing pre-switched).
        submitted_type = (form.business_type.raw_data or [""])[0]
        apply_business_type_preset(tenant, BusinessType(submitted_type or BusinessType.OTHER.value))
        tenant.aato = form.aato.data
        db.session.add(tenant)
        db.session.flush()

        tenant.login_slug = _make_login_slug(tenant.legal_name, tenant.id)
        try:
            tenant.logo_image_id = save_uploaded_image(form.logo.data)
        except UploadError as exc:
            flash(exc.message, "error")
            return render_template("tenants/onboard.html", form=form, **_onboarding_packages(form))

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

        # The client's package: any for the Ultra Admin; for an auditor,
        # one of the packages allowed to them (if they may choose
        # packages at all) - otherwise the client starts as before packages.
        chosen = request.form.get("package_id", type=int)
        package = next((p for p in allowed_packages(current_user) if p.id == chosen), None)
        if package is not None:
            assign_package(tenant, package, current_user)
        record_audit(
            current_user,
            "client_onboarded",
            tenant_id=tenant.id,
            entity_type="tenant",
            entity_id=tenant.id,
            details={"pending_verification": True},
        )
        db.session.commit()

        flash(
            f"{tenant.legal_name} onboarded and awaiting verification by Ultra Admin before it can be used. "
            f"Share the password you set with {admin_user.email} once verified - "
            "they'll be asked to change it on first sign-in.",
            "success",
        )
        return redirect(url_for("tenants.directory"))

    return render_template("tenants/onboard.html", form=form, **_onboarding_packages(form))


def _onboarding_packages(form):
    """The package picker on the onboarding form - for the Ultra Admin, and
    for an auditor allowed to choose packages (their allowed ones only) -
    and the trade packs each business type brings."""
    from app.utils.catalogue import business_types

    ensure_starting_packages()
    db.session.commit()
    type_packs = {b["value"]: b["packs"] for b in business_types()}
    packages = allowed_packages(current_user)
    if not packages:
        return {"packages": [], "type_packs": type_packs}
    by_key = {p.key: p.id for p in packages if p.key}
    suggest = {bt: by_key[key] for bt, key in BUSINESS_TYPE_PACKAGE.items() if key in by_key}
    chosen = request.form.get("package_id", type=int) or suggest.get(form.business_type.data or "retail")
    return {"packages": packages, "package_suggest": suggest, "package_default": chosen, "type_packs": type_packs}


@tenants_bp.route("/clients/<int:tenant_id>")
@admin_or_auditor_required
def detail(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
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
    billing_form = BillingCycleForm(billing_cycle=tenant.billing_cycle.value if tenant.billing_cycle else None)
    auditors = visible_auditors(current_user) if current_user.is_super_admin else []
    return render_template(
        "tenants/detail.html",
        tenant=tenant,
        invoices=invoices,
        pos_bills=pos_bills,
        notes=notes,
        users=users,
        gstin_form=gstin_form,
        billing_form=billing_form,
        auditors=auditors,
        access_status=tenant_access_status(tenant),
        default_period=_current_period(),
        pkg=client_package_context(tenant),
        gstin_meter=limit_meter(tenant, "gstins"),
        login_switches={u.id: branch_login_context(u) for u in users if u.is_staff} if current_user.is_super_admin else {},
        changes=client_changes(tenant, limit=5),
    )


@tenants_bp.route("/clients/<int:tenant_id>/invoices/<int:invoice_id>")
@admin_or_auditor_required
def admin_view_invoice(tenant_id, invoice_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
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
@admin_or_auditor_required
def admin_invoice_pdf(tenant_id, invoice_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
    invoice = Invoice.query.filter_by(id=invoice_id, tenant_id=tenant_id).first_or_404()
    pdf_bytes = render_invoice_pdf(invoice)
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"{invoice.invoice_number.replace('/', '-')}.pdf",
    )


@tenants_bp.route("/clients/<int:tenant_id>/pos-bills/<int:bill_id>")
@admin_or_auditor_required
def admin_view_pos_bill(tenant_id, bill_id):
    # Same read-only drill-down as an invoice (admin_view_invoice): scoped
    # to a client this login may see, audit-logged.
    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
    bill = POSBill.query.filter_by(id=bill_id, tenant_id=tenant_id).filter(POSBill.bill_number.isnot(None)).first_or_404()
    record_audit(current_user, "view_client_pos_bill", tenant_id=tenant_id, entity_type="pos_bill", entity_id=bill.id)
    db.session.commit()
    return render_template("tenants/view_pos_bill.html", tenant=tenant, bill=bill)


@tenants_bp.route("/clients/<int:tenant_id>/pos-bills/<int:bill_id>/pdf")
@admin_or_auditor_required
def admin_pos_bill_pdf(tenant_id, bill_id):
    from app.pos.receipt import render_receipt_pdf

    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
    bill = POSBill.query.filter_by(id=bill_id, tenant_id=tenant_id).filter(POSBill.bill_number.isnot(None)).first_or_404()
    # A copy for the firm - never the client's original.
    pdf_bytes = render_receipt_pdf(bill, "a4", copy_label="DUPLICATE COPY")
    return send_file(BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=False,
                     download_name=f"{bill.bill_number.replace('/', '-')}.pdf")


@tenants_bp.route("/clients/<int:tenant_id>/notes/<int:note_id>")
@admin_or_auditor_required
def admin_view_note(tenant_id, note_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
    note = CreditDebitNote.query.filter_by(id=note_id, tenant_id=tenant_id).first_or_404()
    record_audit(current_user, "view_client_note", tenant_id=tenant_id, entity_type="credit_debit_note", entity_id=note.id)
    db.session.commit()
    return render_template("tenants/view_note.html", tenant=tenant, note=note,
                           document_label=DOCUMENT_TYPE_LABELS[note.document_type])


@tenants_bp.route("/clients/<int:tenant_id>/notes/<int:note_id>/pdf")
@admin_or_auditor_required
def admin_note_pdf(tenant_id, note_id):
    from app.notes.pdf import render_note_pdf

    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
    note = CreditDebitNote.query.filter_by(id=note_id, tenant_id=tenant_id).first_or_404()
    return send_file(BytesIO(render_note_pdf(note)), mimetype="application/pdf", as_attachment=False,
                     download_name=f"{note.note_number.replace('/', '-')}.pdf")


@tenants_bp.route("/clients/<int:tenant_id>/billing/set-cycle", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def set_billing_cycle(tenant_id):
    # Sets a pending client's billing cycle for the first time (which is
    # what lifts the pending-verification gate and lets it go live), or
    # changes an already-active client's cycle later - e.g. Monthly to
    # Yearly. Either way this selection always stays with the Ultra
    # Admin, even for a client an Auditor onboarded.
    tenant = Tenant.query.get_or_404(tenant_id)
    form = BillingCycleForm()
    if form.validate_on_submit():
        was_pending = tenant.billing_cycle is None
        tenant.billing_cycle = BillingCycle(form.billing_cycle.data)
        # A client on a priced package gets that price as their fee, unless
        # the Ultra Admin already set one by hand.
        grant = grant_of(tenant)
        package_fee = package_price_for(grant.package, tenant.billing_cycle) if grant is not None else None
        fee_from_package = tenant.subscription_fee is None and package_fee is not None
        if fee_from_package:
            tenant.subscription_fee = package_fee
            record_audit(current_user, "subscription_fee_set", tenant_id=tenant.id, entity_type="tenant",
                         entity_id=tenant.id, details={"fee": str(package_fee), "from_package": grant.package.name})
        # cycle_anchor_date is never touched here - the recurring cycle
        # only starts on the Client Admin's first successful login after
        # changing their initial password (see app.auth.routes.
        # change_password). But if the cycle has already started
        # (anchor set) and this is a change, not the first-time pick,
        # next_billing_due must be recomputed against the *new* cycle
        # length right now - otherwise switching Monthly -> Yearly left
        # the old monthly due date sitting there unchanged, same bug as
        # resync_billing_cycle exists to fix, just triggered differently.
        due_date_changed = not was_pending and tenant.cycle_anchor_date is not None
        if due_date_changed:
            tenant.next_billing_due = next_due_after(tenant.cycle_anchor_date, tenant.billing_cycle, date.today())
        record_audit(
            current_user,
            "client_verified" if was_pending else "client_billing_cycle_changed",
            tenant_id=tenant.id,
            entity_type="tenant",
            entity_id=tenant.id,
            details={
                "billing_cycle": tenant.billing_cycle.value,
                "next_billing_due": tenant.next_billing_due.isoformat() if tenant.next_billing_due else None,
            },
        )
        db.session.commit()
        message = f"{tenant.legal_name} is now on the {tenant.billing_cycle.value} cycle"
        if was_pending:
            message += " and active."
        elif due_date_changed:
            message += f" - next renewal is now {tenant.next_billing_due.strftime('%d %b %Y')}."
        else:
            message += "."
        if fee_from_package:
            message += f" Fee set to Rs. {package_fee} from the {grant.package.name} package."
        flash(message, "success")
    else:
        flash("Pick a billing cycle.", "error")
    return redirect(url_for("tenants.detail", tenant_id=tenant.id))


@tenants_bp.route("/clients/<int:tenant_id>/reassign-auditor", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def reassign_auditor(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    raw = request.form.get("auditor_id", "").strip()
    new_auditor = None
    if raw:
        new_auditor = User.query.filter(
            User.id == int(raw), User.role.in_([UserRole.AUDITOR, UserRole.SUB_AUDITOR])
        ).first()
        if not new_auditor:
            flash("Pick a valid auditor.", "error")
            return redirect(url_for("tenants.detail", tenant_id=tenant.id))

    tenant.auditor_id = new_auditor.id if new_auditor else None
    record_audit(
        current_user,
        "client_auditor_reassigned",
        tenant_id=tenant.id,
        entity_type="tenant",
        entity_id=tenant.id,
        details={"auditor_id": tenant.auditor_id},
    )
    db.session.commit()
    flash(
        f"{tenant.legal_name} {'allotted to ' + new_auditor.name if new_auditor else 'unallocated'}.",
        "success",
    )
    return redirect(url_for("tenants.detail", tenant_id=tenant.id))


@tenants_bp.route("/clients/<int:tenant_id>/logo", methods=["POST"])
@admin_or_auditor_required
@power_required("edit_client_details")
def update_logo(tenant_id):
    # Lets the admin console set/replace a client's logo on their behalf -
    # clients onboarded before this feature existed otherwise have no way
    # to get one in without a Client Admin visiting Settings themselves.
    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
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
@admin_or_auditor_required
@power_required("edit_client_details")
def add_gstin(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
    if not may_add(tenant, "gstins", current_user):
        flash(f"{tenant.display_name}'s package allows {limit_of(tenant, 'gstins')} GSTIN(s) - all in use. "
              "Ask ARFA to raise the limit.", "error")
        return redirect(url_for("tenants.detail", tenant_id=tenant.id, _anchor="sec-locations"))
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
@admin_or_auditor_required
@power_required("pause_clients")
def deactivate(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
    tenant.is_active = False
    record_audit(
        current_user, "client_deactivated", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id
    )
    db.session.commit()
    flash(f"{tenant.legal_name} deactivated.", "success")
    return redirect(url_for("tenants.detail", tenant_id=tenant.id))


@tenants_bp.route("/clients/<int:tenant_id>/activate", methods=["POST"])
@admin_or_auditor_required
@power_required("pause_clients")
def activate(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
    tenant.is_active = True
    record_audit(
        current_user, "client_activated", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id
    )
    db.session.commit()
    flash(f"{tenant.legal_name} reactivated.", "success")
    return redirect(url_for("tenants.detail", tenant_id=tenant.id))


@tenants_bp.route("/clients/<int:tenant_id>/billing/mark-paid", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def mark_billing_paid(tenant_id):
    # The button is never hidden or disabled; it's always safe to click -
    # app.utils.billing.mark_period_paid says why.
    tenant = Tenant.query.get_or_404(tenant_id)
    result = mark_period_paid(tenant, date.today())
    if result == "paid_today":
        flash(f"{tenant.legal_name} was already marked paid today. Nothing more to do.", "info")
        return redirect(url_for("tenants.detail", tenant_id=tenant.id))
    if result == "paid_ahead":
        flash(
            f"{tenant.legal_name} is already paid up - next renewal isn't due until "
            f"{tenant.next_billing_due.strftime('%d %b %Y')}. Nothing to do.",
            "info",
        )
        return redirect(url_for("tenants.detail", tenant_id=tenant.id))
    record_audit(
        current_user, "client_billing_marked_paid", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id,
        details={"next_billing_due": tenant.next_billing_due.isoformat()},
    )
    db.session.commit()
    flash(f"Marked paid - {tenant.legal_name}'s next renewal is due {tenant.next_billing_due.strftime('%d %b %Y')}.", "success")
    return redirect(url_for("tenants.detail", tenant_id=tenant.id))


@tenants_bp.route("/clients/<int:tenant_id>/billing/resync", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def resync_billing_cycle(tenant_id):
    # An explicit repair action for a tenant whose next_billing_due has
    # already drifted - from the old mark-paid bug, from a hand edit, or
    # anything else - back onto the live calendar. Unlike mark_billing_paid
    # (which always advances relative to the tenant's own current due
    # date), this recomputes from the one date that never drifts -
    # cycle_anchor_date - so it's a true resync rather than another
    # relative nudge that could compound an existing mistake.
    tenant = Tenant.query.get_or_404(tenant_id)
    if tenant.billing_cycle is None:
        flash(f"Set a billing cycle for {tenant.legal_name} first.", "error")
        return redirect(url_for("tenants.detail", tenant_id=tenant.id))
    if tenant.cycle_anchor_date is None:
        flash(
            f"{tenant.legal_name}'s cycle hasn't started yet (no login since verification) - nothing to resync.",
            "error",
        )
        return redirect(url_for("tenants.detail", tenant_id=tenant.id))

    today = date.today()
    old_due = tenant.next_billing_due
    tenant.next_billing_due = next_due_after(tenant.cycle_anchor_date, tenant.billing_cycle, today)
    tenant.last_paid_on = today
    record_audit(
        current_user,
        "client_billing_cycle_resynced",
        tenant_id=tenant.id,
        entity_type="tenant",
        entity_id=tenant.id,
        details={
            "old_next_billing_due": old_due.isoformat() if old_due else None,
            "new_next_billing_due": tenant.next_billing_due.isoformat(),
        },
    )
    db.session.commit()
    flash(
        f"{tenant.legal_name}'s billing cycle resynced to the live calendar - "
        f"next renewal is {tenant.next_billing_due.strftime('%d %b %Y')}.",
        "success",
    )
    return redirect(url_for("tenants.detail", tenant_id=tenant.id))


@tenants_bp.route("/clients/<int:tenant_id>/billing/trigger-alarm", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def trigger_alarm(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    tenant.manual_alarm_active = True
    record_audit(
        current_user, "billing_alarm_triggered", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id
    )
    db.session.commit()
    flash(f"Billing reminder turned on for {tenant.legal_name}.", "success")
    return redirect(url_for("tenants.detail", tenant_id=tenant.id))


@tenants_bp.route("/clients/<int:tenant_id>/billing/mute-alarm", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def mute_alarm(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    tenant.manual_alarm_active = False
    record_audit(
        current_user, "billing_alarm_muted", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id
    )
    db.session.commit()
    flash(f"Billing reminder turned off for {tenant.legal_name}.", "success")
    return redirect(url_for("tenants.detail", tenant_id=tenant.id))


@tenants_bp.route("/clients/<int:tenant_id>/users/<int:user_id>/act-as", methods=["POST"])
@admin_or_auditor_required
@power_required("act_as")
def act_as(tenant_id, user_id):
    # Full operational access to a client's own screens - billing a POS
    # sale under a specific branch, issuing an invoice as that client, or
    # (for an Auditor) creating a new branch for a company they manage -
    # without a second, parallel set of admin-only forms to keep in sync
    # with the real ones. current_user really becomes `target` for the
    # duration, so every existing tenant-scoped route just works
    # unmodified; auth.stop_impersonating switches back.
    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
    target = User.query.filter_by(id=user_id, tenant_id=tenant_id, is_active=True).first_or_404()
    if target.role not in (UserRole.CLIENT_ADMIN, UserRole.STAFF):
        abort(400)

    admin_id = current_user.id
    record_audit(
        current_user, "impersonation_started", tenant_id=tenant_id, entity_type="user", entity_id=target.id,
    )
    db.session.commit()

    login_user(target, remember=False)
    session["impersonator_id"] = admin_id

    # Never routes through change-password here even if must_change_password
    # is set - this is the admin operating the client's screens, not the
    # client managing their own account, and silently changing a password
    # the client doesn't know about would lock them out of their own login.
    if target.is_client_admin:
        return redirect(url_for("invoicing.list_invoices"))
    return redirect(url_for("pos.terminal"))


@tenants_bp.route("/clients/<int:tenant_id>/users/<int:user_id>/deactivate", methods=["POST"])
@admin_or_auditor_required
@power_required("reset_logins")
def deactivate_user(tenant_id, user_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
    user = User.query.filter_by(id=user_id, tenant_id=tenant_id).first_or_404()
    user.is_active = False
    record_audit(
        current_user, "client_user_deactivated_by_firm", tenant_id=tenant_id, entity_type="user", entity_id=user.id,
    )
    db.session.commit()
    flash(f"{user.name} deactivated.", "success")
    return redirect(url_for("tenants.detail", tenant_id=tenant_id))


@tenants_bp.route("/clients/<int:tenant_id>/users/<int:user_id>/activate", methods=["POST"])
@admin_or_auditor_required
@power_required("reset_logins")
def activate_user(tenant_id, user_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
    user = User.query.filter_by(id=user_id, tenant_id=tenant_id).first_or_404()
    if not user.is_active:
        for key in (("branches", "logins") if user.is_staff else ("logins",)):
            if not may_add(tenant, key, current_user):
                flash(f"{tenant.display_name}'s package has no room for another login - ask ARFA to raise it.",
                      "error")
                return redirect(url_for("tenants.detail", tenant_id=tenant_id, _anchor="sec-users"))
    user.is_active = True
    record_audit(
        current_user, "client_user_activated_by_firm", tenant_id=tenant_id, entity_type="user", entity_id=user.id,
    )
    db.session.commit()
    flash(f"{user.name} reactivated.", "success")
    return redirect(url_for("tenants.detail", tenant_id=tenant_id))


@tenants_bp.route("/clients/<int:tenant_id>/users/<int:user_id>/reset-password", methods=["GET", "POST"])
@admin_or_auditor_required
@power_required("reset_logins")
def reset_client_password(tenant_id, user_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
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


@tenants_bp.route("/clients/<int:tenant_id>/users/<int:user_id>/reset-two-factor", methods=["POST"])
@admin_or_auditor_required
@power_required("reset_logins")
def reset_client_two_factor(tenant_id, user_id):
    # A Client Admin who lost the phone with their authenticator app (and
    # their recovery codes): the firm switches two-step sign-in off; they
    # turn it on again from Account security.
    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
    user = User.query.filter_by(id=user_id, tenant_id=tenant_id).first_or_404()
    from app.auth.security import clear_failures, disable_two_factor

    disable_two_factor(user)
    clear_failures(user)
    record_audit(current_user, "two_factor_reset_by_firm", tenant_id=tenant_id, entity_type="user", entity_id=user.id)
    db.session.commit()
    flash(f"Two-step sign-in is off for {user.email} - they can turn it on again from Account security.", "success")
    return redirect(url_for("tenants.detail", tenant_id=tenant_id))


@tenants_bp.route("/auditors/<int:auditor_id>/reset-two-factor", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def reset_auditor_two_factor(auditor_id):
    # An Auditor / Sub-Auditor must have two-step sign-in, so this sends
    # them through setting it up again at their next sign-in.
    auditor = User.query.filter(User.id == auditor_id, User.role.in_([UserRole.AUDITOR, UserRole.SUB_AUDITOR])).first_or_404()
    from app.auth.security import clear_failures, disable_two_factor

    disable_two_factor(auditor)
    clear_failures(auditor)
    record_audit(current_user, "two_factor_reset_by_ultra_admin", entity_type="user", entity_id=auditor.id)
    db.session.commit()
    flash(f"{auditor.email} will set up two-step sign-in again at their next sign-in.", "success")
    return redirect(url_for("tenants.auditor_detail", auditor_id=auditor.id))


@tenants_bp.route("/auditors")
@admin_or_auditor_required
def auditors_directory():
    # Every Auditor in the firm for the Ultra Admin; an Auditor's own
    # Sub-Auditors for them - "within his office". A plain list with filter
    # chips (console cleanup, stage 2): verifying moved to the Today page and
    # clients with no auditor to the Clients list.
    if current_user.is_sub_auditor:
        abort(403)
    auditors = visible_auditors(current_user)
    access_statuses = {a.id: auditor_access_status(a) for a in auditors}
    filters = ["all", "active", "renewal", "blocked", "pending"]
    show = request.args.get("show")
    return render_template(
        "tenants/auditors_directory.html",
        auditors=auditors,
        access_statuses=access_statuses,
        filters=filters,
        show=show if show in filters else "all",
    )


@tenants_bp.route("/auditors/new", methods=["GET", "POST"])
@admin_or_auditor_required
@power_required("create_sub_auditors")
def new_auditor():
    if current_user.is_sub_auditor:
        abort(403)
    if current_user.is_auditor and not has_room(sub_auditor_room(current_user)):
        count, limit = sub_auditor_room(current_user)
        flash(f"You have {count} Sub-Auditors - your limit is {limit}. Ask ARFA to raise it.", "error")
        return redirect(url_for("tenants.auditors_directory"))

    # Only the Ultra Admin can create a top-level Auditor; an Auditor may
    # only ever create a Sub-Auditor within their own office (forced
    # parent = themselves, never a value from the form).
    requested_role = request.form.get("role", "sub_auditor" if current_user.is_auditor else "auditor")
    if current_user.is_auditor:
        role = UserRole.SUB_AUDITOR
        parent_auditor_id = current_user.id
    elif requested_role == "sub_auditor":
        role = UserRole.SUB_AUDITOR
        parent_auditor_id = request.form.get("parent_auditor_id", type=int)
    else:
        role = UserRole.AUDITOR
        parent_auditor_id = None

    form = CreateAuditorForm()
    all_auditors = User.query.filter_by(role=UserRole.AUDITOR).order_by(User.name).all() if current_user.is_super_admin else []

    if form.validate_on_submit():
        if role == UserRole.SUB_AUDITOR and current_user.is_super_admin:
            parent = User.query.filter_by(id=parent_auditor_id, role=UserRole.AUDITOR).first()
            if not parent:
                flash("Pick a valid Auditor to own this Sub-Auditor.", "error")
                return render_template("tenants/new_auditor.html", form=form, all_auditors=all_auditors)
            parent_auditor_id = parent.id

        existing = User.query.filter_by(email=form.email.data.strip().lower()).first()
        if existing:
            flash("A user with that email already exists.", "error")
            return render_template("tenants/new_auditor.html", form=form, all_auditors=all_auditors)

        new_user = User(
            name=form.name.data.strip(),
            email=form.email.data.strip().lower(),
            phone=form.phone.data,
            role=role,
            parent_auditor_id=parent_auditor_id if role == UserRole.SUB_AUDITOR else None,
            must_change_password=True,
        )
        new_user.set_password(form.password.data)
        db.session.add(new_user)
        db.session.flush()
        record_audit(
            current_user,
            "auditor_created" if role == UserRole.AUDITOR else "sub_auditor_created",
            entity_type="user",
            entity_id=new_user.id,
            details={"role": role.value, "parent_auditor_id": new_user.parent_auditor_id},
        )
        db.session.commit()
        flash(f"{new_user.role_label} '{new_user.name}' created.", "success")
        return redirect(url_for("tenants.auditors_directory"))

    return render_template("tenants/new_auditor.html", form=form, all_auditors=all_auditors, default_role=requested_role)


@tenants_bp.route("/auditors/<int:auditor_id>")
@admin_or_auditor_required
def auditor_detail(auditor_id):
    if current_user.is_sub_auditor:
        abort(403)
    auditor = User.query.filter(User.id == auditor_id, User.role.in_([UserRole.AUDITOR, UserRole.SUB_AUDITOR])).first_or_404()
    assert_auditor_owns_auditor(current_user, auditor)

    clients = Tenant.query.filter_by(auditor_id=auditor.id).order_by(Tenant.legal_name).all()
    access_statuses = {t.id: tenant_access_status(t) for t in clients}
    sub_auditors = list(auditor.sub_auditors) if auditor.is_auditor else []
    billing_form = BillingCycleForm(billing_cycle=auditor.billing_cycle.value if auditor.billing_cycle else None)
    return render_template(
        "tenants/auditor_detail.html",
        auditor=auditor,
        clients=clients,
        access_statuses=access_statuses,
        sub_auditors=sub_auditors,
        billing_form=billing_form,
        auditor_access_status=auditor_access_status(auditor),
        powers=auditor_powers_context(current_user, auditor),
        power_changes=auditor_changes(auditor, limit=10) if may_set_powers(current_user, auditor) else None,
    )


@tenants_bp.route("/auditors/<int:auditor_id>/billing/set-cycle", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def set_auditor_billing_cycle(auditor_id):
    # Same system as a client's (tenants.set_billing_cycle) - sets a
    # pending Auditor/Sub-Auditor's cycle for the first time (lifting the
    # pending-verification gate) or changes an already-active one's
    # cycle later.
    auditor = User.query.filter(User.id == auditor_id, User.role.in_([UserRole.AUDITOR, UserRole.SUB_AUDITOR])).first_or_404()
    form = BillingCycleForm()
    if form.validate_on_submit():
        was_pending = auditor.billing_cycle is None
        auditor.billing_cycle = BillingCycle(form.billing_cycle.data)
        # Same recompute as tenants.set_billing_cycle - see there for why.
        due_date_changed = not was_pending and auditor.cycle_anchor_date is not None
        if due_date_changed:
            auditor.next_billing_due = next_due_after(auditor.cycle_anchor_date, auditor.billing_cycle, date.today())
        record_audit(
            current_user,
            "auditor_verified" if was_pending else "auditor_billing_cycle_changed",
            entity_type="user",
            entity_id=auditor.id,
            details={
                "billing_cycle": auditor.billing_cycle.value,
                "next_billing_due": auditor.next_billing_due.isoformat() if auditor.next_billing_due else None,
            },
        )
        db.session.commit()
        message = f"{auditor.name} is now on the {auditor.billing_cycle.value} cycle"
        if was_pending:
            message += " and active."
        elif due_date_changed:
            message += f" - next renewal is now {auditor.next_billing_due.strftime('%d %b %Y')}."
        else:
            message += "."
        flash(message, "success")
    else:
        flash("Pick a billing cycle.", "error")
    return redirect(url_for("tenants.auditor_detail", auditor_id=auditor.id))


@tenants_bp.route("/auditors/<int:auditor_id>/billing/mark-paid", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def mark_auditor_billing_paid(auditor_id):
    auditor = User.query.filter(User.id == auditor_id, User.role.in_([UserRole.AUDITOR, UserRole.SUB_AUDITOR])).first_or_404()
    # Same idempotency guards as a client's - app.utils.billing.mark_period_paid.
    result = mark_period_paid(auditor, date.today())
    if result == "paid_today":
        flash(f"{auditor.name} was already marked paid today. Nothing more to do.", "info")
        return redirect(url_for("tenants.auditor_detail", auditor_id=auditor.id))
    if result == "paid_ahead":
        flash(
            f"{auditor.name} is already paid up - next renewal isn't due until "
            f"{auditor.next_billing_due.strftime('%d %b %Y')}. Nothing to do.",
            "info",
        )
        return redirect(url_for("tenants.auditor_detail", auditor_id=auditor.id))
    record_audit(
        current_user, "auditor_billing_marked_paid", entity_type="user", entity_id=auditor.id,
        details={"next_billing_due": auditor.next_billing_due.isoformat()},
    )
    db.session.commit()
    flash(f"Marked paid - {auditor.name}'s next renewal is due {auditor.next_billing_due.strftime('%d %b %Y')}.", "success")
    return redirect(url_for("tenants.auditor_detail", auditor_id=auditor.id))


@tenants_bp.route("/auditors/<int:auditor_id>/billing/resync", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def resync_auditor_billing_cycle(auditor_id):
    # Same repair action as tenants.resync_billing_cycle - see there for why.
    auditor = User.query.filter(User.id == auditor_id, User.role.in_([UserRole.AUDITOR, UserRole.SUB_AUDITOR])).first_or_404()
    if auditor.billing_cycle is None:
        flash(f"Set a billing cycle for {auditor.name} first.", "error")
        return redirect(url_for("tenants.auditor_detail", auditor_id=auditor.id))
    if auditor.cycle_anchor_date is None:
        flash(
            f"{auditor.name}'s cycle hasn't started yet (no login since verification) - nothing to resync.",
            "error",
        )
        return redirect(url_for("tenants.auditor_detail", auditor_id=auditor.id))

    today = date.today()
    old_due = auditor.next_billing_due
    auditor.next_billing_due = next_due_after(auditor.cycle_anchor_date, auditor.billing_cycle, today)
    auditor.last_paid_on = today
    record_audit(
        current_user,
        "auditor_billing_cycle_resynced",
        entity_type="user",
        entity_id=auditor.id,
        details={
            "old_next_billing_due": old_due.isoformat() if old_due else None,
            "new_next_billing_due": auditor.next_billing_due.isoformat(),
        },
    )
    db.session.commit()
    flash(
        f"{auditor.name}'s billing cycle resynced to the live calendar - "
        f"next renewal is {auditor.next_billing_due.strftime('%d %b %Y')}.",
        "success",
    )
    return redirect(url_for("tenants.auditor_detail", auditor_id=auditor.id))


@tenants_bp.route("/auditors/<int:auditor_id>/billing/trigger-alarm", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def trigger_auditor_alarm(auditor_id):
    auditor = User.query.filter(User.id == auditor_id, User.role.in_([UserRole.AUDITOR, UserRole.SUB_AUDITOR])).first_or_404()
    auditor.manual_alarm_active = True
    record_audit(current_user, "auditor_billing_alarm_triggered", entity_type="user", entity_id=auditor.id)
    db.session.commit()
    flash(f"Billing reminder turned on for {auditor.name}.", "success")
    return redirect(url_for("tenants.auditor_detail", auditor_id=auditor.id))


@tenants_bp.route("/auditors/<int:auditor_id>/billing/mute-alarm", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def mute_auditor_alarm(auditor_id):
    auditor = User.query.filter(User.id == auditor_id, User.role.in_([UserRole.AUDITOR, UserRole.SUB_AUDITOR])).first_or_404()
    auditor.manual_alarm_active = False
    record_audit(current_user, "auditor_billing_alarm_muted", entity_type="user", entity_id=auditor.id)
    db.session.commit()
    flash(f"Billing reminder turned off for {auditor.name}.", "success")
    return redirect(url_for("tenants.auditor_detail", auditor_id=auditor.id))


@tenants_bp.route("/auditors/<int:auditor_id>/act-as", methods=["POST"])
@admin_or_auditor_required
@power_required("act_as")
def act_as_auditor(auditor_id):
    if current_user.is_sub_auditor:
        abort(403)
    target = User.query.filter(
        User.id == auditor_id, User.role.in_([UserRole.AUDITOR, UserRole.SUB_AUDITOR]), User.is_active.is_(True)
    ).first_or_404()
    assert_auditor_owns_auditor(current_user, target)

    admin_id = current_user.id
    record_audit(
        current_user, "auditor_impersonation_started", entity_type="user", entity_id=target.id,
    )
    db.session.commit()

    login_user(target, remember=False)
    session["impersonator_id"] = admin_id
    return redirect(url_for("tenants.today"))


@tenants_bp.route("/auditors/<int:auditor_id>/deactivate", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def deactivate_auditor(auditor_id):
    auditor = User.query.filter(User.id == auditor_id, User.role.in_([UserRole.AUDITOR, UserRole.SUB_AUDITOR])).first_or_404()
    auditor.is_active = False
    record_audit(current_user, "auditor_deactivated", entity_type="user", entity_id=auditor.id)
    db.session.commit()
    flash(f"{auditor.name} deactivated.", "success")
    return redirect(url_for("tenants.auditor_detail", auditor_id=auditor.id))


@tenants_bp.route("/auditors/<int:auditor_id>/activate", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def activate_auditor(auditor_id):
    auditor = User.query.filter(User.id == auditor_id, User.role.in_([UserRole.AUDITOR, UserRole.SUB_AUDITOR])).first_or_404()
    auditor.is_active = True
    record_audit(current_user, "auditor_activated", entity_type="user", entity_id=auditor.id)
    db.session.commit()
    flash(f"{auditor.name} reactivated.", "success")
    return redirect(url_for("tenants.auditor_detail", auditor_id=auditor.id))


@tenants_bp.route("/auditors/<int:auditor_id>/reset-password", methods=["GET", "POST"])
@roles_required(UserRole.SUPER_ADMIN)
def reset_auditor_password(auditor_id):
    # Ultra-Admin-only, same as deactivate/activate_auditor - this isn't
    # scoped through assert_auditor_owns_auditor, so it covers every
    # Auditor and every one of their Sub-Auditors, not just the ones an
    # Auditor manages themselves.
    auditor = User.query.filter(User.id == auditor_id, User.role.in_([UserRole.AUDITOR, UserRole.SUB_AUDITOR])).first_or_404()
    form = SetPasswordForm()
    if form.validate_on_submit():
        auditor.set_password(form.new_password.data)
        auditor.must_change_password = True
        record_audit(
            current_user,
            "auditor_password_reset_by_ultra_admin",
            entity_type="user",
            entity_id=auditor.id,
        )
        db.session.commit()
        flash(f"Password updated for {auditor.email}. Share it with them directly.", "success")
        return redirect(url_for("tenants.auditor_detail", auditor_id=auditor.id))
    return render_template("tenants/reset_auditor_password.html", form=form, auditor=auditor)


@tenants_bp.route("/clients/<int:tenant_id>/allocate-sub-auditor", methods=["POST"])
@admin_or_auditor_required
@power_required("create_sub_auditors")
def allocate_client_to_sub_auditor(tenant_id):
    # An Auditor handing one of their own clients to one of their own
    # Sub-Auditors, "within his office" - never outside it.
    if not current_user.is_auditor:
        abort(403)
    tenant = Tenant.query.filter_by(id=tenant_id, auditor_id=current_user.id).first_or_404()
    sub_auditor_id = request.form.get("sub_auditor_id", type=int)
    sub_auditor = User.query.filter_by(
        id=sub_auditor_id, role=UserRole.SUB_AUDITOR, parent_auditor_id=current_user.id
    ).first()
    if not sub_auditor:
        flash("Pick one of your own Sub-Auditors.", "error")
        return redirect(url_for("tenants.detail", tenant_id=tenant.id))

    tenant.auditor_id = sub_auditor.id
    record_audit(
        current_user,
        "client_allocated_to_sub_auditor",
        tenant_id=tenant.id,
        entity_type="tenant",
        entity_id=tenant.id,
        details={"sub_auditor_id": sub_auditor.id},
    )
    db.session.commit()
    flash(f"{tenant.legal_name} allotted to {sub_auditor.name}.", "success")
    return redirect(url_for("tenants.detail", tenant_id=tenant.id))


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

    # Each client's full monthly pack (GSTR-1/3B in Excel, PDF and JSON per
    # GSTIN, HSN summary, purchase / expense registers, P&L, Tally files).
    from app.finance.filing import monthly_pack_zip, period_bounds

    try:
        period_bounds(period)
        buf = monthly_pack_zip(selected, period)
    except (ReportPeriodError, ValueError):
        flash("Pick a valid month.", "error")
        return redirect(url_for("tenants.gst_exports"))

    record_audit(
        current_user,
        "bulk_gst_export",
        details={"period": period, "tenant_count": len(selected), "tenant_ids": sorted(t.id for t in selected)},
    )
    db.session.commit()

    return send_file(
        buf, as_attachment=True, download_name=f"GST_exports_{period}.zip", mimetype="application/zip"
    )


# --- Phase 7: filing tracker, monthly pack, Tally export ---------------------------

def _scoped_tenants():
    ids = auditor_tenant_ids(current_user)
    tenants = _exportable_tenants()
    return [t for t in tenants if ids is None or t.id in ids]


def _period_arg(value=None) -> str:
    from app.finance.filing import period_bounds

    period = value or request.values.get("period") or _current_period()
    try:
        period_bounds(period)
    except (ValueError, TypeError):
        abort(404)
    return period


@tenants_bp.route("/filing")
@admin_or_auditor_required
@power_required("month_end")
def filing_tracker():
    from app.finance.filing import tracker_rows

    period = _period_arg()
    rows = tracker_rows(_scoped_tenants(), period)
    return render_template("tenants/filing.html", rows=rows, period=period, today=date.today())


@tenants_bp.route("/clients/<int:tenant_id>/filing", methods=["POST"])
@admin_or_auditor_required
@power_required("month_end")
def filing_update(tenant_id):
    from app.finance.filing import mark_filed, mark_ready
    from app.models.filing import ReturnType

    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
    gstin = tenant.gstins.filter_by(id=request.form.get("gstin_id", type=int)).first_or_404()
    period = _period_arg(request.form.get("period"))
    action = request.form.get("action")
    if action in ("ready", "not_ready"):
        mark_ready(tenant.id, gstin.id, period, current_user, action == "ready")
        details = {"period": period, "gstin": gstin.gstin, "data_ready": action == "ready"}
    else:
        try:
            rt = ReturnType(request.form.get("return_type", ""))
        except ValueError:
            abort(400)
        filed_on = None
        if request.form.get("filed_on"):
            try:
                filed_on = date.fromisoformat(request.form["filed_on"])
            except ValueError:
                filed_on = None
        mark_filed(tenant.id, gstin.id, period, rt, current_user, action == "filed", filed_on, request.form.get("arn"))
        details = {"period": period, "gstin": gstin.gstin, "return": rt.value, "filed": action == "filed",
                   "arn": request.form.get("arn") or None}
    record_audit(current_user, "filing_status_updated", tenant_id=tenant.id, entity_type="tenant",
                 entity_id=tenant.id, details=details)
    db.session.commit()
    flash(f"{tenant.legal_name} - {period} updated.", "success")
    return redirect(url_for("tenants.filing_tracker", period=period))


@tenants_bp.route("/clients/<int:tenant_id>/monthly-pack.zip")
@admin_or_auditor_required
@power_required("month_end")
def monthly_pack(tenant_id):
    from app.finance.filing import pack_files, safe_name

    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
    period = _period_arg()
    buf = BytesIO()
    with ZipFile(buf, "w", ZIP_DEFLATED) as zf:
        for name, data in pack_files(tenant, period):
            zf.writestr(name, data)
    buf.seek(0)
    record_audit(current_user, "monthly_pack_export", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id,
                 details={"period": period})
    db.session.commit()
    return send_file(buf, as_attachment=True, mimetype="application/zip",
                     download_name=f"{safe_name(tenant)}_{period}_pack.zip")


@tenants_bp.route("/clients/<int:tenant_id>/tally.zip")
@admin_or_auditor_required
@power_required("month_end")
def tally_export(tenant_id):
    from app.finance.filing import period_bounds, safe_name
    from app.finance.tally import tally_zip

    tenant = Tenant.query.get_or_404(tenant_id)
    assert_auditor_owns_tenant(current_user, tenant)
    period = _period_arg()
    start, end = period_bounds(period)
    record_audit(current_user, "tally_export", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id,
                 details={"period": period})
    db.session.commit()
    return send_file(tally_zip(tenant, start, end), as_attachment=True, mimetype="application/zip",
                     download_name=f"{safe_name(tenant)}_{period}_tally.zip")
