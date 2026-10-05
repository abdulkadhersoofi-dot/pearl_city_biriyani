from flask import flash, redirect, render_template, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.extensions import db
from app.models.user import UserRole
from app.settings import settings_bp
from app.settings.forms import TenantSettingsForm
from app.utils.uploads import UploadError, delete_uploaded_image, save_uploaded_image


@settings_bp.route("/", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def index():
    tenant = current_user.tenant
    form = TenantSettingsForm(obj=tenant)
    if form.validate_on_submit():
        if form.logo.data and form.logo.data.filename:
            try:
                new_logo_path = save_uploaded_image(form.logo.data, "logos")
            except UploadError as exc:
                flash(exc.message, "error")
                return render_template("settings/form.html", form=form, tenant=tenant)
            delete_uploaded_image(tenant.logo_path)
            tenant.logo_path = new_logo_path

        tenant.primary_color = form.primary_color.data
        tenant.background_color = form.background_color.data
        tenant.font_family = form.font_family.data
        tenant.default_receipt_format = form.default_receipt_format.data
        db.session.commit()
        flash("Settings updated.", "success")
        return redirect(url_for("settings.index"))

    return render_template("settings/form.html", form=form, tenant=tenant)
