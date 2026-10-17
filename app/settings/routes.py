from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.extensions import db
from app.models.tenant import BusinessType
from app.models.user import UserRole
from app.settings import settings_bp
from app.settings.forms import (
    PAYMENT_MODE_CHOICES,
    AppearanceForm,
    BusinessForm,
    CounterForm,
    DocumentsForm,
    ModulesForm,
    TenantSettingsForm,
    feature_field_name,
)
from app.settings.sections import module_groups, section_info, section_menu
from app.utils.audit import record_audit
from app.utils.entitlements import SETTINGS, client_may_switch, locked_for_client
from app.utils.features import AVAILABLE_FEATURES, BUSINESS_TYPE_LABELS, FEATURES, apply_business_type_preset
from app.utils.uploads import UploadError, delete_uploaded_image, save_uploaded_image


# --- saving, shared by the sections and the all-in-one form -----------------------------------
# Each returns an error message (nothing saved by the caller) or None.

def _save_appearance(tenant, form):
    if form.logo.data and getattr(form.logo.data, "filename", None):
        try:
            new_logo_id = save_uploaded_image(form.logo.data)
        except UploadError as exc:
            return exc.message
        # Point the tenant at the new image (and flush it) before deleting
        # the old row - the other way around trips the foreign key, since
        # tenant.logo_image_id still references it.
        old_logo_id = tenant.logo_image_id
        tenant.logo_image_id = new_logo_id
        db.session.flush()
        delete_uploaded_image(old_logo_id)
    tenant.primary_color = form.primary_color.data
    tenant.background_color = form.background_color.data
    tenant.text_color = form.text_color.data
    tenant.font_family = form.font_family.data
    return None


def _save_counter(tenant, form):
    modes = [m for m, _ in PAYMENT_MODE_CHOICES if m in (form.pos_payment_modes.data or [])]
    if not modes:
        return "Keep at least one payment mode switched on."
    tenant.pos_payment_modes = modes
    tenant.upi_vpa = form.upi_vpa.data
    tenant.upi_payee_name = form.upi_payee_name.data
    return None


def _submitted_business_type(form, tenant):
    # The submitted value, not form.business_type.data: when the field is
    # absent, Optional() lets the obj-populated enum (stringified) through
    # as "data". Absent means "keep the current type".
    submitted = (form.business_type.raw_data or [""])[0]
    return BusinessType(submitted) if submitted else tenant.business_type


def _apply_locks(tenant, form, business_type):
    """True when the post tried to change a locked business setting."""
    # A locked field is shown disabled, so it only arrives if someone
    # posted it by hand.
    sent = request.form
    tried = _locked("business_type") and (
        ("business_type" in sent and business_type != tenant.business_type) or form.apply_business_preset.data)
    tried = tried or (_locked("aato") and "aato" in sent and form.aato.data != tenant.aato)
    tried = tried or (_locked("pricing") and "default_price_includes_tax" in sent
                      and form.default_price_includes_tax.data != tenant.default_price_includes_tax)
    return bool(tried)


def _save_features(tenant, form):
    # Only the modules the client's package leaves to them; Included and
    # Off modules are the Ultra Admin's to change (app/utils/entitlements.py).
    for feature in AVAILABLE_FEATURES:
        if client_may_switch(tenant, feature.key):
            tenant.set_feature(feature.key, getattr(form, feature_field_name(feature.key)).data)


def _finish(tenant, before_features, section=None):
    """Ticks the first-run "business profile" step, audit-logs a change of
    modules (and which section was saved), and commits."""
    if not (tenant.first_run or {}).get("profile_saved"):
        from app.onboarding import mark

        mark(tenant, "profile_saved")
    if tenant.features != before_features:
        record_audit(
            current_user,
            "tenant_features_changed",
            tenant_id=tenant.id,
            entity_type="tenant",
            entity_id=tenant.id,
            details={"before": before_features, "after": tenant.features},
        )
    if section:
        record_audit(current_user, "settings_saved", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id,
                     details={"section": section})
    db.session.commit()


def _section(template, key, **context):
    tenant = current_user.tenant
    return render_template(template, section=section_info(key), sections=section_menu(key), tenant=tenant,
                           locked={k: locked_for_client(tenant, k) for k in SETTINGS}, **context)


def _locked(key):
    """Is this setting the Ultra Admin's for the signed-in client's package?
    (Not while the Ultra Admin is acting as them.)"""
    return locked_for_client(current_user.tenant, key)


def _kept_locked():
    flash("Some settings are set by ARFA for your package and were not changed.", "warning")


# --- Business (and the all-in-one form, at the same address) ------------------------------------

@settings_bp.route("/", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def index():
    tenant = current_user.tenant
    if request.method == "POST" and not request.form.get("section"):
        return _save_everything(tenant)
    form = BusinessForm(obj=tenant)
    if request.method == "GET":
        form.business_type.data = tenant.business_type.value
    if form.validate_on_submit():
        business_type = _submitted_business_type(form, tenant)
        before = dict(tenant.features or {})
        refused = _apply_locks(tenant, form, business_type)
        if refused:
            business_type = tenant.business_type
        if not _locked("aato"):
            tenant.aato = form.aato.data
        if form.apply_business_preset.data and not _locked("business_type"):
            kept_pricing = tenant.default_price_includes_tax
            apply_business_type_preset(tenant, business_type)
            if _locked("pricing"):
                tenant.default_price_includes_tax = kept_pricing
            message = (f"Saved - modules switched to the recommended set for "
                       f"{BUSINESS_TYPE_LABELS[business_type]}.")
        else:
            tenant.business_type = business_type
            if not _locked("pricing"):
                tenant.default_price_includes_tax = form.default_price_includes_tax.data
            message = "Business details saved."
        _finish(tenant, before, "business")
        if refused:
            _kept_locked()
        flash(message, "success")
        return redirect(url_for("settings.index"))
    return _section("settings/business.html", "business", form=form)


def _save_everything(tenant):
    """Every section from one post - how the single Settings form saved
    before the sections existed. Kept so nothing that posts it breaks."""
    form = TenantSettingsForm(obj=tenant)
    if not form.validate_on_submit():
        return _rejected(*[e for errors in form.errors.values() for e in errors])
    if not _locked("branding"):
        error = _save_appearance(tenant, form)
        if error:
            return _rejected(error)
    if not _locked("receipt"):
        tenant.default_receipt_format = form.default_receipt_format.data
    if not _locked("aato"):
        tenant.aato = form.aato.data
    # Only when the counter card was on the submitted page - an older
    # client/test posting without it keeps the modes it has.
    if request.form.get("pos_settings_shown") and not _locked("counter"):
        error = _save_counter(tenant, form)
        if error:
            return _rejected(error)
    # Only when its card was on the page (the margin_scheme module was on).
    if request.form.get("margin_settings_shown") and not _locked("margin_declaration"):
        tenant.margin_scheme_declaration = form.margin_scheme_declaration.data
    business_type = _submitted_business_type(form, tenant)
    if _locked("business_type"):
        business_type = tenant.business_type
    before = dict(tenant.features or {})
    if form.apply_business_preset.data and not _locked("business_type"):
        apply_business_type_preset(tenant, business_type)
        message = f"Settings updated - modules reset to the recommended set for {BUSINESS_TYPE_LABELS[business_type]}."
    else:
        tenant.business_type = business_type
        if not _locked("pricing"):
            tenant.default_price_includes_tax = form.default_price_includes_tax.data
        _save_features(tenant, form)
        message = "Settings updated."
    _finish(tenant, before)
    flash(message, "success")
    return redirect(url_for("settings.index"))


def _rejected(*errors):
    """Nothing saved: say why and show Settings again (as the single form did)."""
    for error in errors:
        flash(error, "error")
    return _section("settings/business.html", "business", form=BusinessForm(obj=current_user.tenant))


# --- Appearance ----------------------------------------------------------------------------------

@settings_bp.route("/appearance", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def appearance():
    tenant = current_user.tenant
    form = AppearanceForm(obj=tenant)
    if request.method == "POST" and _locked("branding"):
        abort(403)
    if form.validate_on_submit():
        error = _save_appearance(tenant, form)
        if error:
            flash(error, "error")
        else:
            _finish(tenant, dict(tenant.features or {}), "appearance")
            flash("Appearance saved.", "success")
            return redirect(url_for("settings.appearance"))
    return _section("settings/appearance.html", "appearance", form=form)


# --- Modules (the switches) ----------------------------------------------------------------------

@settings_bp.route("/features", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def features():
    tenant = current_user.tenant
    form = ModulesForm()
    if request.method == "GET":
        for feature in AVAILABLE_FEATURES:
            getattr(form, feature_field_name(feature.key)).data = tenant.has_feature(feature.key)
    if form.validate_on_submit():
        before = dict(tenant.features or {})
        _save_features(tenant, form)
        changed = sorted(k for k in set(before) | set(tenant.features or {})
                         if bool(before.get(k)) != tenant.has_feature(k))
        _finish(tenant, before, "modules")
        flash(f"Modules saved - {len(changed)} changed." if changed else "Modules saved - nothing changed.",
              "success")
        return redirect(url_for("settings.features"))
    from app.models.package import ModuleRequest

    pending = {r.feature_key: r for r in ModuleRequest.query.filter_by(tenant_id=tenant.id,
                                                                     status=ModuleRequest.PENDING)}
    return _section("settings/features.html", "modules", form=form, groups=module_groups(tenant),
                    field_name=feature_field_name, requests=pending)


@settings_bp.route("/features/ask/<key>", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def request_module(key):
    """"Ask ARFA to add" a module that isn't in the client's package."""
    from app.utils.entitlements import OFF, module_state, request_module as ask

    tenant = current_user.tenant
    if key not in FEATURES or module_state(tenant, key) != OFF:
        abort(404)
    ask(tenant, key, current_user)
    db.session.commit()
    flash(f"Asked ARFA to add {FEATURES[key].label}. It shows here once it's added.", "success")
    return redirect(url_for("settings.features"))


# --- Counter -------------------------------------------------------------------------------------

@settings_bp.route("/counter", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def counter():
    tenant = current_user.tenant
    form = CounterForm(obj=tenant)
    if request.method == "GET":
        form.pos_payment_modes.data = list(tenant.pos_payment_modes or [])
    if request.method == "POST" and _locked("counter"):
        abort(403)
    if form.validate_on_submit():
        error = _save_counter(tenant, form)
        if error:
            flash(error, "error")
        else:
            _finish(tenant, dict(tenant.features or {}), "counter")
            flash("Counter settings saved.", "success")
            return redirect(url_for("settings.counter"))
    return _section("settings/counter.html", "counter", form=form)


# --- Documents & printing ------------------------------------------------------------------------

@settings_bp.route("/documents", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def documents():
    from app.utils.margin import DEFAULT_DECLARATION

    tenant = current_user.tenant
    form = DocumentsForm(obj=tenant)
    if form.validate_on_submit():
        refused = ((_locked("receipt") and form.default_receipt_format.data != tenant.default_receipt_format)
                   or (_locked("margin_declaration") and tenant.has_feature("margin_scheme")
                       and (form.margin_scheme_declaration.data or None) != (tenant.margin_scheme_declaration or None)))
        if not _locked("receipt"):
            tenant.default_receipt_format = form.default_receipt_format.data
        if tenant.has_feature("margin_scheme") and not _locked("margin_declaration"):
            tenant.margin_scheme_declaration = form.margin_scheme_declaration.data
        _finish(tenant, dict(tenant.features or {}), "documents")
        if refused:
            _kept_locked()
        flash("Printing settings saved.", "success")
        return redirect(url_for("settings.documents"))
    return _section("settings/documents.html", "documents", form=form, default_margin_declaration=DEFAULT_DECLARATION)


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
    return _section("settings/modules.html", "modules", weighing=weighing, loyalty=module_settings(tenant, "loyalty"),
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
    if request.method == "POST" and _locked("security"):
        abort(403)
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
    return _section("settings/security.html", "security", idle=module_settings(tenant, "security")["idle_minutes"],
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
