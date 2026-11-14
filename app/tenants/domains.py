"""Sign-in domains, managed by the Ultra Admin only (app/auth/login_domain.py
serves them): add a domain the firm points at this server, let clients onto
it - every login of each client may then sign in there - choose whose look
the sign-in page wears, switch it off or remove it. A client is on at most
one domain; any client can be moved onto one at any time, whatever their
package (that is how a client becomes a VVIP client)."""

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.auth.login_domain import domain_problem, normalise_domain
from app.extensions import db
from app.models.platform import SignInDomain
from app.models.tenant import Tenant
from app.models.user import UserRole
from app.tenants import tenants_bp
from app.utils.audit import record_audit


def _all_clients():
    return Tenant.query.order_by(Tenant.legal_name).all()


def _put_on_domain(tenant: Tenant, domain: SignInDomain | None) -> None:
    """Moves a client onto a domain (or off any). A client that wore the
    look of the domain it leaves stops doing so."""
    old = tenant.sign_in_domain
    if old is not None and old is not domain and old.brand_tenant_id == tenant.id:
        old.brand_tenant_id = None
    tenant.sign_in_domain = domain
    record_audit(current_user, "tenant_sign_in_domain_changed", tenant_id=tenant.id, entity_type="tenant",
                 entity_id=tenant.id, details={"from": old.domain if old else None,
                                               "to": domain.domain if domain else None})


@tenants_bp.route("/sign-in-domains", methods=["GET", "POST"])
@roles_required(UserRole.SUPER_ADMIN)
def sign_in_domains():
    if request.method == "POST":
        domain = normalise_domain(request.form.get("domain"))
        if domain is None:
            flash("That isn't a domain name - enter it like pcbinvoice.com or login.theirbrand.com.", "error")
        elif domain_problem(domain):
            flash(domain_problem(domain), "error")
        elif SignInDomain.query.filter_by(domain=domain).first() is not None:
            flash(f"{domain} is already a sign-in domain.", "error")
        else:
            row = SignInDomain(domain=domain, created_by_id=current_user.id)
            db.session.add(row)
            db.session.flush()
            record_audit(current_user, "sign_in_domain_added", entity_type="sign_in_domain", entity_id=row.id,
                         details={"domain": domain})
            db.session.commit()
            flash(f"{domain} added. Next: choose its clients below, and ask Claude for its DNS records.", "success")
            return redirect(url_for("tenants.sign_in_domain", domain_id=row.id))
    domains = SignInDomain.query.order_by(SignInDomain.domain).all()
    return render_template("tenants/sign_in_domains.html", domains=domains)


@tenants_bp.route("/sign-in-domains/<int:domain_id>", methods=["GET", "POST"])
@roles_required(UserRole.SUPER_ADMIN)
def sign_in_domain(domain_id):
    domain = SignInDomain.query.get_or_404(domain_id)
    if request.method == "POST":
        action = request.form.get("action")
        tenant = Tenant.query.get(request.form.get("tenant_id", type=int) or 0)
        if action == "add_client" and tenant is not None:
            moved_from = tenant.sign_in_domain.domain if tenant.sign_in_domain and tenant.sign_in_domain is not domain else None
            _put_on_domain(tenant, domain)
            db.session.commit()
            flash(f"{tenant.display_name}'s logins can now sign in at {domain.domain}"
                  + (f" (moved from {moved_from})." if moved_from else "."), "success")
        elif action == "remove_client" and tenant is not None and tenant.sign_in_domain_id == domain.id:
            _put_on_domain(tenant, None)
            db.session.commit()
            flash(f"{tenant.display_name}'s logins no longer sign in at {domain.domain}.", "success")
        elif action == "settings":
            brand_id = request.form.get("brand_tenant_id", type=int)
            brand = Tenant.query.get(brand_id) if brand_id else None
            if brand is not None and brand.sign_in_domain_id != domain.id:
                flash("The look can only be one of this domain's clients.", "error")
                return redirect(url_for("tenants.sign_in_domain", domain_id=domain.id))
            before = {"brand": domain.brand_tenant_id, "active": domain.is_active}
            domain.brand_tenant_id = brand.id if brand else None
            domain.is_active = request.form.get("is_active") == "1"
            record_audit(current_user, "sign_in_domain_changed", entity_type="sign_in_domain", entity_id=domain.id,
                         details={"domain": domain.domain, "before": before,
                                  "after": {"brand": domain.brand_tenant_id, "active": domain.is_active}})
            db.session.commit()
            flash(f"{domain.domain} saved.", "success")
        elif action == "delete":
            for client in list(domain.tenants):
                _put_on_domain(client, None)
            record_audit(current_user, "sign_in_domain_removed", entity_type="sign_in_domain", entity_id=domain.id,
                         details={"domain": domain.domain})
            name = domain.domain
            db.session.delete(domain)
            db.session.commit()
            flash(f"{name} removed - its clients sign in at the main address. Ask Claude to disconnect it from "
                  "hosting too.", "success")
            return redirect(url_for("tenants.sign_in_domains"))
        return redirect(url_for("tenants.sign_in_domain", domain_id=domain.id))
    on_domain = list(domain.tenants)
    others = [t for t in _all_clients() if t.sign_in_domain_id != domain.id]
    return render_template("tenants/sign_in_domain.html", domain=domain, on_domain=on_domain, others=others)


@tenants_bp.route("/clients/<int:tenant_id>/sign-in-domain", methods=["POST"])
@roles_required(UserRole.SUPER_ADMIN)
def set_client_sign_in_domain(tenant_id):
    """From the client's page: put any client on a sign-in domain (or none)."""
    tenant = Tenant.query.get_or_404(tenant_id)
    domain_id = request.form.get("domain_id", type=int)
    domain = SignInDomain.query.get(domain_id) if domain_id else None
    if domain_id and domain is None:
        flash("Pick a sign-in domain from the list.", "error")
    elif domain is not tenant.sign_in_domain:
        _put_on_domain(tenant, domain)
        db.session.commit()
        flash(f"{tenant.display_name}'s logins sign in at {domain.domain}." if domain
              else f"{tenant.display_name}'s logins sign in at the main address.", "success")
    return redirect(url_for("tenants.detail", tenant_id=tenant.id) + "#sec-branding")
