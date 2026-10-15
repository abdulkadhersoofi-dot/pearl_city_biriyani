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
