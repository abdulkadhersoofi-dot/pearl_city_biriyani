from flask import flash, redirect, render_template, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.auth.forms import SetPasswordForm
from app.extensions import db
from app.models.user import User, UserRole
from app.staff import staff_bp
from app.staff.forms import StaffForm
from app.utils.audit import record_audit
from app.utils.tenant_scope import assert_owns, tenant_query


@staff_bp.route("/")
@roles_required(UserRole.CLIENT_ADMIN)
def list_staff():
    staff = tenant_query(User).filter_by(role=UserRole.STAFF).order_by(User.name).all()
    return render_template("staff/list.html", staff=staff)


@staff_bp.route("/new", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def new_staff():
    form = StaffForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        if User.query.filter_by(email=email).first():
            flash("A user with that email already exists.", "error")
            return render_template("staff/form.html", form=form)

        user = User(
            tenant_id=current_user.tenant_id,
            name=form.name.data.strip(),
            email=email,
            phone=form.phone.data,
            role=UserRole.STAFF,
            must_change_password=True,
        )
        user.set_password(form.password.data)
        db.session.add(user)
        record_audit(current_user, "staff_created", tenant_id=current_user.tenant_id, entity_type="user")
        db.session.commit()
        flash(f"{user.name} added. Share the password you set with them.", "success")
        return redirect(url_for("staff.list_staff"))
    return render_template("staff/form.html", form=form)


@staff_bp.route("/<int:user_id>/deactivate", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def deactivate_staff(user_id):
    user = tenant_query(User).filter_by(id=user_id, role=UserRole.STAFF).first_or_404()
    assert_owns(user)
    user.is_active = False
    record_audit(current_user, "staff_deactivated", tenant_id=current_user.tenant_id, entity_type="user", entity_id=user.id)
    db.session.commit()
    flash(f"{user.name} deactivated.", "success")
    return redirect(url_for("staff.list_staff"))


@staff_bp.route("/<int:user_id>/activate", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def activate_staff(user_id):
    user = tenant_query(User).filter_by(id=user_id, role=UserRole.STAFF).first_or_404()
    assert_owns(user)
    user.is_active = True
    record_audit(current_user, "staff_activated", tenant_id=current_user.tenant_id, entity_type="user", entity_id=user.id)
    db.session.commit()
    flash(f"{user.name} reactivated.", "success")
    return redirect(url_for("staff.list_staff"))


@staff_bp.route("/<int:user_id>/reset-password", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def reset_staff_password(user_id):
    user = tenant_query(User).filter_by(id=user_id, role=UserRole.STAFF).first_or_404()
    assert_owns(user)
    form = SetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.new_password.data)
        user.must_change_password = True
        record_audit(
            current_user, "staff_password_reset", tenant_id=current_user.tenant_id, entity_type="user", entity_id=user.id
        )
        db.session.commit()
        flash(f"Password updated for {user.name}. Share it with them directly.", "success")
        return redirect(url_for("staff.list_staff"))
    return render_template("staff/reset_password.html", form=form, staff_user=user)
