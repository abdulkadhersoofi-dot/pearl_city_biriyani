from datetime import date

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.auth.decorators import tenant_user_required
from app.extensions import db
from app.jobs import jobs_bp, services
from app.models.business import JOB_STATUS_LABELS, JobCard, JobCardLine, JobStatus
from app.models.customer import Customer
from app.models.inventory import ProductSerial
from app.models.pos_bill import POSBill
from app.models.product import Product
from app.pos.share import whatsapp_link
from app.utils.audit import record_audit
from app.utils.features import feature_required
from app.utils.gst_config import GST_RATE_SLABS
from app.utils.tenant_scope import assert_owns, tenant_query

FLAG = "job_cards"


def _job(job_id):
    job = tenant_query(JobCard).filter_by(id=job_id).first_or_404()
    assert_owns(job)
    return job


@jobs_bp.route("/")
@tenant_user_required
@feature_required(FLAG)
def index():
    status = request.args.get("status", "open")
    q = (request.args.get("q") or "").strip()
    query = tenant_query(JobCard)
    if status == "open":
        query = query.filter(JobCard.status.in_([JobStatus.RECEIVED, JobStatus.IN_PROGRESS, JobStatus.READY]))
    elif status in {s.value for s in JobStatus}:
        query = query.filter(JobCard.status == JobStatus(status))
    if q:
        like = f"%{q}%"
        query = query.join(Customer, Customer.id == JobCard.customer_id).filter(db.or_(
            JobCard.number.ilike(like), JobCard.item.ilike(like), JobCard.serial_no.ilike(like),
            Customer.name.ilike(like), Customer.phone.ilike(like)))
    jobs = query.order_by(JobCard.id.desc()).limit(300).all()
    counts = {s: tenant_query(JobCard).filter_by(status=s).count() for s in (JobStatus.RECEIVED, JobStatus.IN_PROGRESS, JobStatus.READY)}
    return render_template("jobs/list.html", jobs=jobs, status=status, q=q, labels=JOB_STATUS_LABELS, counts=counts)


@jobs_bp.route("/new", methods=["GET", "POST"])
@tenant_user_required
@feature_required(FLAG)
def new():
    if request.method == "POST":
        try:
            job = services.create_job(current_user.tenant, current_user, request.form)
            record_audit(current_user, "job_card_created", tenant_id=current_user.tenant_id, entity_type="job_card",
                         entity_id=job.id, details={"number": job.number})
            db.session.commit()
            flash(f"Job {job.number} opened.", "success")
            return redirect(url_for("jobs.view", job_id=job.id))
        except (services.JobError, ValueError) as exc:
            db.session.rollback()
            flash(getattr(exc, "message", "Check the dates."), "error")
    customers = tenant_query(Customer).filter_by(is_active=True).order_by(Customer.name).all()
    return render_template("jobs/form.html", customers=customers, form=request.form if request.method == "POST" else request.args)


@jobs_bp.route("/<int:job_id>", methods=["GET", "POST"])
@tenant_user_required
@feature_required(FLAG)
def view(job_id):
    job = _job(job_id)
    tenant = current_user.tenant
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "status":
                services.set_status(job, JobStatus(request.form.get("status")))
                if request.form.get("work_notes") is not None:
                    job.work_notes = request.form.get("work_notes", "").strip() or None
            elif action == "part":
                services.add_part(tenant, current_user, job, request.form.get("product_id", type=int) or 0, request.form.get("qty"))
            elif action == "labour":
                services.add_labour(job, request.form.get("description"), request.form.get("amount"),
                                    request.form.get("gst_rate"), request.form.get("sac"))
            elif action == "remove":
                line = JobCardLine.query.filter_by(id=request.form.get("line_id", type=int) or 0, job_id=job.id).first_or_404()
                services.remove_line(job, line, current_user)
            elif action == "invoice":
                if not current_user.is_client_admin:
                    abort(403)
                invoice = services.make_invoice(tenant, current_user, job)
                record_audit(current_user, "job_card_invoiced", tenant_id=tenant.id, entity_type="job_card", entity_id=job.id,
                             details={"invoice": invoice.invoice_number})
            elif action == "cancel":
                services.cancel(job, current_user, request.form.get("reason", ""))
            else:
                abort(400)
            if action != "invoice":
                record_audit(current_user, f"job_card_{action}", tenant_id=tenant.id, entity_type="job_card",
                             entity_id=job.id, details={k: v for k, v in request.form.items() if k != "csrf_token"})
            db.session.commit()
            flash("Saved.", "success")
        except services.JobError as exc:
            db.session.rollback()
            flash(exc.message, "error")
        except ValueError:
            db.session.rollback()
            flash("Pick a valid status.", "error")
        return redirect(url_for("jobs.view", job_id=job.id))
    parts = (tenant_query(Product).filter_by(is_active=True, is_service=False).filter(Product.has_variants.is_(False))
             .order_by(Product.name).all())
    share = None
    if job.status == JobStatus.READY:
        share = whatsapp_link(job.customer.phone, services.ready_message(tenant, job))
    return render_template("jobs/view.html", job=job, labels=JOB_STATUS_LABELS, flow=services.FLOW, parts=parts,
                           gst_rates=GST_RATE_SLABS, share=share)


@jobs_bp.route("/warranty")
@tenant_user_required
@feature_required("electronics")
def warranty():
    """Warranty check by IMEI / serial: what it is, when and to whom it was
    sold, and whether the customer's warranty still runs."""
    q = (request.args.get("q") or "").strip()
    unit = tenant_query(ProductSerial).filter_by(serial_no=q).first() if q else None
    bill = tenant_query(POSBill).filter_by(id=unit.sold_pos_bill_id).first() if unit and unit.sold_pos_bill_id else None
    jobs = tenant_query(JobCard).filter_by(serial_no=q).order_by(JobCard.id.desc()).all() if q else []
    return render_template("jobs/warranty.html", q=q, unit=unit, bill=bill, today=date.today(), jobs=jobs,
                           job_cards_on=current_user.tenant.has_feature(FLAG))
