from decimal import Decimal

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.auth.forms import SetPasswordForm
from app.branches import branches_bp
from app.branches.forms import BranchForm, BranchPermissionsForm
from app.extensions import db
from app.models.price_list import PriceList
from app.models.user import BranchType, User, UserRole
from app.pos.stock import branch_stock_status
from app.utils.audit import record_audit
from app.utils.entitlements import (
    BRANCH_MODULES, branch_login_context, counter_permissions_allowed, limit_message, limit_meter, may_add,
)
from app.utils.features import feature_required
from app.utils.permissions import BRANCH_ROLES, PERMISSION_COLUMNS, role_of
from app.utils.tenant_scope import assert_owns, tenant_query


def _room_for_a_branch(tenant):
    """None if there's room for one more branch login, else why not."""
    for key in ("branches", "logins"):
        if not may_add(tenant, key):
            return limit_message(tenant, key)
    return None


@branches_bp.route("/")
@roles_required(UserRole.CLIENT_ADMIN)
def list_branches():
    tenant = current_user.tenant
    branches = tenant_query(User).filter_by(role=UserRole.STAFF).order_by(User.name).all()
    stock_by_branch = {b.id: branch_stock_status(current_user.tenant_id, b.id) for b in branches}
    from app.counterapp.pairing import app_devices

    return render_template("branches/list.html", branches=branches, stock_by_branch=stock_by_branch,
                           meters=[limit_meter(tenant, "branches"), limit_meter(tenant, "logins")],
                           roles=BRANCH_ROLES, devices=app_devices(tenant))


# --- the ARFA Counter app: pairing a phone (counter app, stage 1) ----------------------------

@branches_bp.route("/<int:user_id>/pair", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("pos")
def pair_phone(user_id):
    """A one-time code that signs a phone in as this branch. Shown once,
    here, never stored in the clear."""
    from app.counterapp import pairing

    branch = User.query.get_or_404(user_id)
    assert_owns(branch)
    try:
        record, code = pairing.new_pairing(current_user.tenant, branch, current_user)
    except pairing.PairingError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        return redirect(url_for("branches.list_branches"))
    record_audit(current_user, "counter_pairing_code_made", tenant_id=current_user.tenant_id, entity_type="user",
                 entity_id=branch.id, details={"branch": branch.name})
    db.session.commit()
    return render_template("branches/pair_code.html", branch=branch, code=code, expires_at=record.expires_at,
                           minutes=pairing.PAIRING_MINUTES)


@branches_bp.route("/devices/<int:device_id>/active", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def device_active(device_id):
    from app.models.platform import CounterDevice

    device = tenant_query(CounterDevice).filter_by(id=device_id).first_or_404()
    device.is_active = request.form.get("active") == "1"
    record_audit(current_user, "counter_device_on" if device.is_active else "counter_device_off",
                 tenant_id=current_user.tenant_id, entity_type="counter_device", entity_id=device.id,
                 details={"code": device.code, "name": device.name})
    db.session.commit()
    flash(f"{device.name} ({device.code}) is {'on again' if device.is_active else 'switched off - it stops billing now'}.",
          "success")
    return redirect(url_for("branches.list_branches", _anchor="sec-devices"))


@branches_bp.route("/new", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def new_branch():
    tenant = current_user.tenant
    full = _room_for_a_branch(tenant)
    if full:
        flash(full, "error")
        return redirect(url_for("branches.list_branches"))
    form = BranchForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        if User.query.filter_by(email=email).first():
            flash("A user with that email already exists.", "error")
            return render_template("branches/form.html", form=form)

        user = User(
            tenant_id=current_user.tenant_id,
            name=form.name.data.strip(),
            email=email,
            phone=form.phone.data,
            role=UserRole.STAFF,
            branch_type=BranchType(form.branch_type.data),
            must_change_password=True,
        )
        _apply_role(user, form.role.data)
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.flush()
        record_audit(current_user, "branch_created", tenant_id=current_user.tenant_id, entity_type="user",
                     entity_id=user.id, details={"name": user.name, "role_label": BRANCH_ROLES[user.branch_role]["label"]})
        db.session.commit()
        flash(f"{user.name} added. Share the password you set with them.", "success")
        return redirect(url_for("branches.list_branches"))
    return render_template("branches/form.html", form=form, roles=BRANCH_ROLES,
                           meters=[limit_meter(tenant, "branches"), limit_meter(tenant, "logins")])


def _apply_role(user, role_key):
    role = BRANCH_ROLES[role_key]
    for key, value in role["permissions"].items():
        setattr(user, PERMISSION_COLUMNS[key], value)
    user.max_discount_percent = role["max_discount"]
    user.branch_role = role_key


@branches_bp.route("/<int:user_id>/deactivate", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def deactivate_branch(user_id):
    user = tenant_query(User).filter_by(id=user_id, role=UserRole.STAFF).first_or_404()
    assert_owns(user)
    user.is_active = False
    record_audit(current_user, "branch_deactivated", tenant_id=current_user.tenant_id, entity_type="user", entity_id=user.id)
    db.session.commit()
    flash(f"{user.name} deactivated.", "success")
    return redirect(url_for("branches.list_branches"))


@branches_bp.route("/<int:user_id>/activate", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def activate_branch(user_id):
    user = tenant_query(User).filter_by(id=user_id, role=UserRole.STAFF).first_or_404()
    assert_owns(user)
    full = None if user.is_active else _room_for_a_branch(current_user.tenant)
    if full:
        flash(full, "error")
        return redirect(url_for("branches.list_branches"))
    user.is_active = True
    record_audit(current_user, "branch_activated", tenant_id=current_user.tenant_id, entity_type="user", entity_id=user.id)
    db.session.commit()
    flash(f"{user.name} reactivated.", "success")
    return redirect(url_for("branches.list_branches"))


@branches_bp.route("/<int:user_id>/reset-password", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def reset_branch_password(user_id):
    user = tenant_query(User).filter_by(id=user_id, role=UserRole.STAFF).first_or_404()
    assert_owns(user)
    form = SetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.new_password.data)
        user.must_change_password = True
        record_audit(
            current_user, "branch_password_reset", tenant_id=current_user.tenant_id, entity_type="user", entity_id=user.id
        )
        db.session.commit()
        flash(f"Password updated for {user.name}. Share it with them directly.", "success")
        return redirect(url_for("branches.list_branches"))
    return render_template("branches/reset_password.html", form=form, branch_user=user)


@branches_bp.route("/<int:user_id>/stock")
@roles_required(UserRole.CLIENT_ADMIN)
def branch_stock(user_id):
    # View-only - stock reaches a branch exclusively through Invoicing (a
    # branch-billed invoice transfers it automatically on save). This page
    # only tracks today's running total, it never sends stock itself.
    branch = tenant_query(User).filter_by(id=user_id, role=UserRole.STAFF).first_or_404()
    assert_owns(branch)
    status = branch_stock_status(current_user.tenant_id, branch.id)
    return render_template("branches/stock.html", branch=branch, status=status)


@branches_bp.route("/<int:user_id>/permissions", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def branch_permissions(user_id):
    """A branch login's role, its counter permissions and the modules at
    its counter. The package caps what can be given, and whatever the
    Ultra Admin set for this login shows as "Set by ARFA" and stays."""
    branch = tenant_query(User).filter_by(id=user_id, role=UserRole.STAFF).first_or_404()
    assert_owns(branch)
    tenant = current_user.tenant
    form = BranchPermissionsForm(obj=branch)
    lists_on = tenant.has_feature("multi_price_list")
    price_lists = tenant_query(PriceList).filter_by(is_active=True).order_by(PriceList.name).all() if lists_on else []
    form.price_list_id.choices = [("", "Normal prices")] + [(str(pl.id), pl.name) for pl in price_lists]
    if request.method == "GET":
        form.price_list_id.data = str(branch.price_list_id) if branch.price_list_id else ""
    ctx = branch_login_context(branch)
    if form.validate_on_submit():
        before = {name: getattr(branch, name) for name in PERMISSION_FIELDS}
        before_hidden = list(branch.hidden_modules or [])
        previous_list = branch.price_list_id
        form.populate_obj(branch)
        branch.price_list_id = previous_list
        if lists_on:
            chosen = (form.price_list_id.raw_data or [""])[0]
            valid = {str(pl.id): pl.id for pl in price_lists}
            branch.price_list_id = valid.get(chosen)
        role_key = request.form.get("role")
        if role_key in BRANCH_ROLES:
            for key, value in BRANCH_ROLES[role_key]["permissions"].items():
                setattr(branch, PERMISSION_COLUMNS[key], value)
            branch.max_discount_percent = BRANCH_ROLES[role_key]["max_discount"]
        else:
            # One by one: what the package doesn't allow, or ARFA set for this
            # login, was shown fixed and isn't sent - keep it as it was. (A
            # role is stored whole; the package still caps it when used.)
            allowed = counter_permissions_allowed(tenant)
            for row in ctx["rows"]:
                if not row["allowed"] or row["arfa"] is not None:
                    setattr(branch, row["column"], before[row["column"]])
            if (("discount" not in allowed or ctx["arfa_discount_cap"] is not None)
                    and before["max_discount_percent"] is not None):
                branch.max_discount_percent = before["max_discount_percent"]
        # A discount limit only means something with discounts allowed;
        # switching discounts off also zeroes it so a later re-enable
        # starts from a deliberate number, not a forgotten old one.
        if not branch.can_give_discount or branch.max_discount_percent is None:
            branch.max_discount_percent = Decimal("0")
        branch.branch_role = role_of({k: getattr(branch, c) for k, c in PERMISSION_COLUMNS.items()},
                                     branch.max_discount_percent)
        # Modules at this counter: those ticked stay; the business keeps them all.
        # Only a module that was on the form can be hidden by leaving it
        # unticked; any other (off for the business, hidden by ARFA, or
        # switched on since the page opened) keeps the earlier choice.
        on_form = set(request.form.getlist("module_shown"))
        ticked = set(request.form.getlist("module"))
        hidden = []
        for m in ctx["modules"]:
            if m["key"] in on_form and m["business_on"] and not m["hidden_by_arfa"]:
                if m["key"] not in ticked:
                    hidden.append(m["key"])
            elif m["hidden_by_client"]:
                hidden.append(m["key"])
        branch.hidden_modules = hidden or None
        after = {name: getattr(branch, name) for name in PERMISSION_FIELDS}
        record_audit(
            current_user,
            "branch_permissions_changed",
            tenant_id=current_user.tenant_id,
            entity_type="user",
            entity_id=branch.id,
            details={
                "before": {k: str(v) for k, v in before.items()},
                "after": {k: str(v) for k, v in after.items()},
                "role": branch.branch_role,
                **({"hidden_modules": [before_hidden, branch.hidden_modules or []]}
                   if before_hidden != (branch.hidden_modules or []) else {}),
                **({"price_list_id": [previous_list, branch.price_list_id]} if previous_list != branch.price_list_id else {}),
            },
        )
        db.session.commit()
        flash(f"Permissions updated for {branch.name}.", "success")
        return redirect(url_for("branches.list_branches"))
    return render_template("branches/permissions.html", form=form, branch=branch, lists_on=lists_on, ctx=ctx)


PERMISSION_FIELDS = (
    "can_give_discount",
    "max_discount_percent",
    "can_edit_price_at_pos",
    "can_refund",
    "can_void",
    "can_view_reports",
    "can_manage_stock",
)


@branches_bp.route("/cashiers", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("cashier_pin")
def cashiers():
    """Cashiers at each counter login (a branch, or the Client Admin's own
    counter) and their PINs (feature "cashier_pin")."""
    from app.models.platform import Cashier
    from app.pos import cashiers as cashier_service

    tenant = current_user.tenant
    counters = cashier_service.counter_logins(tenant)
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action in ("add", "activate") and not may_add(tenant, "logins"):
                flash(limit_message(tenant, "logins"), "error")
                return redirect(url_for("branches.cashiers"))
            if action == "add":
                counter = tenant_query(User).filter_by(id=request.form.get("counter_id", type=int)).first_or_404()
                if counter not in counters:
                    abort(404)
                cashier = cashier_service.add_cashier(tenant, counter, request.form.get("name"), request.form.get("pin"))
                message = f"{cashier.name} added at {counter.name} - they sign in at the counter with their PIN."
            else:
                cashier = tenant_query(Cashier).filter_by(id=request.form.get("cashier_id", type=int)).first_or_404()
                if action == "pin":
                    cashier_service.change_pin(cashier, request.form.get("pin"))
                    message = f"New PIN saved for {cashier.name}."
                elif action in ("deactivate", "activate"):
                    cashier_service.set_active(cashier, action == "activate")
                    message = f"{cashier.name} {'can sign in again' if action == 'activate' else 'can no longer sign in'}."
                else:
                    abort(400)
        except cashier_service.CashierError as exc:
            db.session.rollback()
            flash(exc.message, "error")
            return redirect(url_for("branches.cashiers"))
        record_audit(current_user, f"cashier_{action}", tenant_id=tenant.id, entity_type="cashier", entity_id=cashier.id)
        db.session.commit()
        flash(message, "success")
        return redirect(url_for("branches.cashiers"))
    rows = [(c, cashier_service.cashiers_for(tenant, c, active_only=False)) for c in counters]
    return render_template("branches/cashiers.html", rows=rows, meter=limit_meter(tenant, "logins"))
