from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.extensions import db
from app.models.tenant import BusinessType
from app.models.user import UserRole
from app.settings import settings_bp
from app.settings.forms import PAYMENT_MODE_CHOICES, TenantSettingsForm, feature_field_name
from app.utils.audit import record_audit
from app.utils.features import AVAILABLE_FEATURES, BUSINESS_TYPE_LABELS, apply_business_type_preset
from app.utils.uploads import UploadError, delete_uploaded_image, save_uploaded_image


@settings_bp.route("/", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def index():
    tenant = current_user.tenant
    form = TenantSettingsForm(obj=tenant)
    if request.method == "GET":
        form.business_type.data = tenant.business_type.value
        form.pos_payment_modes.data = list(tenant.pos_payment_modes or [])
        for feature in AVAILABLE_FEATURES:
            getattr(form, feature_field_name(feature.key)).data = tenant.has_feature(feature.key)
    if form.validate_on_submit():
        if form.logo.data and form.logo.data.filename:
            try:
                new_logo_id = save_uploaded_image(form.logo.data)
            except UploadError as exc:
                flash(exc.message, "error")
                return _render(form, tenant)
            # Point the tenant at the new image (and flush it) before
            # deleting the old row - the other way around trips the
            # foreign key, since tenant.logo_image_id still references it.
            old_logo_id = tenant.logo_image_id
            tenant.logo_image_id = new_logo_id
            db.session.flush()
            delete_uploaded_image(old_logo_id)

        tenant.primary_color = form.primary_color.data
        tenant.background_color = form.background_color.data
        tenant.text_color = form.text_color.data
        tenant.font_family = form.font_family.data
        tenant.default_receipt_format = form.default_receipt_format.data
        tenant.aato = form.aato.data
        # Only when the counter card was on the submitted page - an older
        # client/test posting without it keeps the modes it has.
        if request.form.get("pos_settings_shown"):
            modes = [m for m, _ in PAYMENT_MODE_CHOICES if m in (form.pos_payment_modes.data or [])]
            if not modes:
                flash("Keep at least one payment mode switched on.", "error")
                return _render(form, tenant)
            tenant.pos_payment_modes = modes
            tenant.upi_vpa = form.upi_vpa.data
            tenant.upi_payee_name = form.upi_payee_name.data
        # Only when its card was on the page (the margin_scheme module was on).
        if request.form.get("margin_settings_shown"):
            tenant.margin_scheme_declaration = form.margin_scheme_declaration.data

        # The submitted value, not form.business_type.data: when the field
        # is absent, Optional() lets the obj-populated enum (stringified)
        # through as "data". Absent means "keep the current type".
        submitted = (form.business_type.raw_data or [""])[0]
        business_type = BusinessType(submitted) if submitted else tenant.business_type
        before = dict(tenant.features or {})
        if form.apply_business_preset.data:
            apply_business_type_preset(tenant, business_type)
            message = f"Settings updated - modules reset to the recommended set for {BUSINESS_TYPE_LABELS[business_type]}."
        else:
            tenant.business_type = business_type
            tenant.default_price_includes_tax = form.default_price_includes_tax.data
            for feature in AVAILABLE_FEATURES:
                tenant.set_feature(feature.key, getattr(form, feature_field_name(feature.key)).data)
            message = "Settings updated."
        if not (tenant.first_run or {}).get("profile_saved"):
            from app.onboarding import mark

            mark(tenant, "profile_saved")
        if tenant.features != before:
            record_audit(
                current_user,
                "tenant_features_changed",
                tenant_id=tenant.id,
                entity_type="tenant",
                entity_id=tenant.id,
                details={"before": before, "after": tenant.features},
            )
        db.session.commit()
        flash(message, "success")
        return redirect(url_for("settings.index"))

    return _render(form, tenant)


def _render(form, tenant):
    from app.utils.margin import DEFAULT_DECLARATION

    return render_template(
        "settings/form.html", form=form, tenant=tenant, features=AVAILABLE_FEATURES, feature_field_name=feature_field_name,
        default_margin_declaration=DEFAULT_DECLARATION
    )


@settings_bp.route("/modules", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def modules():
    """Settings of the weighing-scale and loyalty modules (restaurant has its
    own setup page). 404 when neither is on."""
    from flask import abort

    from app.utils.module_settings import dec, module_settings, save_module_settings

    tenant = current_user.tenant
    if not (tenant.has_feature("weighing") or tenant.has_feature("loyalty")):
        abort(404)
    if request.method == "POST":
        f = request.form
        if f.get("module") == "weighing" and tenant.has_feature("weighing"):
            prefixes = {}
            for kind in ("weight", "price"):
                for p in (f.get(f"{kind}_prefixes") or "").replace(" ", "").split(","):
                    if len(p) == 2 and p.isdigit() and p[0] == "2":
                        prefixes[p] = kind
            digits = f.get("code_digits", type=int) or 5
            values = {"prefixes": prefixes or {"21": "weight"}, "code_digits": min(max(digits, 4), 6)}
            save_module_settings(tenant, "weighing", values)
        elif f.get("module") == "loyalty" and tenant.has_feature("loyalty"):
            values = {"points_per_100": str(max(dec(f.get("points_per_100")), dec(0))),
                      "point_value": str(max(dec(f.get("point_value"), "1"), dec("0.01"))),
                      "min_redeem": max(f.get("min_redeem", type=int) or 0, 0)}
            save_module_settings(tenant, "loyalty", values)
        else:
            abort(404)
        record_audit(current_user, "module_settings_saved", tenant_id=tenant.id, details={"module": f.get("module"), **values})
        db.session.commit()
        flash("Saved.", "success")
        return redirect(url_for("settings.modules"))
    weighing = module_settings(tenant, "weighing")
    return render_template("settings/modules.html", weighing=weighing, loyalty=module_settings(tenant, "loyalty"),
                           weight_prefixes=", ".join(k for k, v in weighing["prefixes"].items() if v == "weight"),
                           price_prefixes=", ".join(k for k, v in weighing["prefixes"].items() if v == "price"))


IDLE_CHOICES = [0, 15, 30, 60, 120, 240, 480]
LOCK_CHOICES = [0, 2, 5, 10, 15, 30]


@settings_bp.route("/security", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def security():
    """Idle sign-out for this client's logins, and the counter's auto-lock
    with cashier PINs. Both off unless set here."""
    from app.utils.module_settings import module_settings, save_module_settings

    tenant = current_user.tenant
    if request.method == "POST":
        idle = request.form.get("idle_minutes", type=int) or 0
        save_module_settings(tenant, "security", {"idle_minutes": idle if idle in IDLE_CHOICES else 0})
        details = {"idle_minutes": idle if idle in IDLE_CHOICES else 0}
        if tenant.has_feature("cashier_pin"):
            lock = request.form.get("lock_minutes", type=int) or 0
            save_module_settings(tenant, "cashier", {"lock_minutes": lock if lock in LOCK_CHOICES else 0})
            details["lock_minutes"] = lock if lock in LOCK_CHOICES else 0
        record_audit(current_user, "security_settings_saved", tenant_id=tenant.id, details=details)
        db.session.commit()
        flash("Saved.", "success")
        return redirect(url_for("settings.security"))
    return render_template("settings/security.html", idle=module_settings(tenant, "security")["idle_minutes"],
                           lock=module_settings(tenant, "cashier")["lock_minutes"], idle_choices=IDLE_CHOICES,
                           lock_choices=LOCK_CHOICES)


# --- first-run checklist ---------------------------------------------------------------------

@settings_bp.route("/first-run/dismiss", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def dismiss_first_run():
    from app.onboarding import mark

    tenant = current_user.tenant
    mark(tenant, "dismissed")
    record_audit(current_user, "first_run_dismissed", tenant_id=tenant.id)
    db.session.commit()
    return redirect(request.referrer or url_for("main.index"))


@settings_bp.route("/printer-test", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def printer_test():
    """A test receipt on the counter's paper size - the last first-run step."""
    tenant = current_user.tenant
    if request.method == "POST":
        from app.onboarding import mark

        mark(tenant, "printer_tested")
        record_audit(current_user, "printer_tested", tenant_id=tenant.id,
                     details={"format": request.form.get("format") or tenant.default_receipt_format})
        db.session.commit()
        flash("Printer checked.", "success")
        return redirect(url_for("main.index"))
    return render_template("settings/printer_test.html", tenant=tenant)


@settings_bp.route("/printer-test.pdf")
@roles_required(UserRole.CLIENT_ADMIN)
def printer_test_pdf():
    from io import BytesIO

    from flask import send_file

    from app.pos.receipt import THERMAL_WIDTHS_IN, _render_pdf

    tenant = current_user.tenant
    page_format = request.args.get("format") or tenant.default_receipt_format
    if page_format not in THERMAL_WIDTHS_IN and page_format != "a4":
        page_format = tenant.default_receipt_format
    gstin = tenant.gstins.filter_by(is_active=True).first()
    pdf = _render_pdf("settings/printer_test_print.html", tenant, {"gstin": gstin, "page_format": page_format}, 4,
                      page_format)
    return send_file(BytesIO(pdf), mimetype="application/pdf", as_attachment=False, download_name="printer-test.pdf")
