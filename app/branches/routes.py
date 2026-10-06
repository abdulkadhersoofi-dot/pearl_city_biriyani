from flask import flash, redirect, render_template, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.auth.forms import SetPasswordForm
from app.branches import branches_bp
from app.branches.forms import BranchForm
from app.extensions import db
from app.models.user import BranchType, User, UserRole
from app.pos.stock import branch_stock_status
from app.utils.audit import record_audit
from app.utils.tenant_scope import assert_owns, tenant_query


@branches_bp.route("/")
@roles_required(UserRole.CLIENT_ADMIN)
def list_branches():
    branches = tenant_query(User).filter_by(role=UserRole.STAFF).order_by(User.name).all()
    stock_by_branch = {b.id: branch_stock_status(current_user.tenant_id, b.id) for b in branches}
    return render_template("branches/list.html", branches=branches, stock_by_branch=stock_by_branch)


@branches_bp.route("/new", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def new_branch():
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
        user.set_password(form.password.data)
        db.session.add(user)
        record_audit(current_user, "branch_created", tenant_id=current_user.tenant_id, entity_type="user")
        db.session.commit()
        flash(f"{user.name} added. Share the password you set with them.", "success")
        return redirect(url_for("branches.list_branches"))
    return render_template("branches/form.html", form=form)


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
