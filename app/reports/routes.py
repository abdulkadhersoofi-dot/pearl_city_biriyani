from datetime import date, timedelta
from io import BytesIO

from flask import abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.models.tenant import Gstin, RegistrationType
from app.models.user import UserRole
from app.reports import reports_bp
from app.reports.export import (
    build_gstr1_workbook,
    build_gstr3b_workbook,
    build_sales_workbook,
    render_gstr1_pdf,
    render_gstr3b_pdf,
    render_sales_pdf,
    to_json_bytes,
)
from app.reports.gstr import ReportPeriodError, gstr1_data, gstr3b_data
from app.reports.sales import sales_report_data


def _current_period() -> str:
    today = date.today()
    return f"{today.year:04d}-{today.month:02d}"


def _resolve_gstin(gstin_id):
    gstin = Gstin.query.filter_by(id=gstin_id, tenant_id=current_user.tenant_id).first()
    if not gstin:
        abort(404)
    return gstin


@reports_bp.route("/")
@roles_required(UserRole.CLIENT_ADMIN)
def index():
    tenant = current_user.tenant
    gstins = tenant.gstins.filter_by(is_active=True).all()
    period = request.args.get("period", _current_period())
    not_regular = tenant.registration_type != RegistrationType.REGULAR
    sales_start, sales_end = _default_sales_range()
    return render_template(
        "reports/index.html",
        tenant=tenant,
        gstins=gstins,
        period=period,
        not_regular=not_regular,
        sales_start=sales_start,
        sales_end=sales_end,
    )


def _default_sales_range() -> tuple[date, date]:
    today = date.today()
    return today - timedelta(days=30), today


def _parse_sales_range():
    """(start, end) from ?start=&end=, defaulting to the last 30 days;
    swaps them if given backwards rather than erroring, and a bad/blank
    date just falls back to its own default independently."""
    default_start, default_end = _default_sales_range()

    def _parse(value, fallback):
        try:
            return date.fromisoformat(value) if value else fallback
        except ValueError:
            return fallback

    start = _parse(request.args.get("start"), default_start)
    end = _parse(request.args.get("end"), default_end)
    if start > end:
        start, end = end, start
    return start, end


@reports_bp.route("/sales")
@roles_required(UserRole.CLIENT_ADMIN)
def sales_view():
    start, end = _parse_sales_range()
    data = sales_report_data(current_user.tenant, start, end)
    return render_template("reports/sales.html", data=data)


@reports_bp.route("/sales.xlsx")
@roles_required(UserRole.CLIENT_ADMIN)
def sales_xlsx():
    start, end = _parse_sales_range()
    data = sales_report_data(current_user.tenant, start, end)
    buf = build_sales_workbook(data)
    return send_file(
        buf,
        as_attachment=True,
        download_name=f"Sales_{start.isoformat()}_to_{end.isoformat()}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@reports_bp.route("/sales.pdf")
@roles_required(UserRole.CLIENT_ADMIN)
def sales_pdf():
    start, end = _parse_sales_range()
    data = sales_report_data(current_user.tenant, start, end)
    pdf_bytes = render_sales_pdf(data)
    return send_file(
        BytesIO(pdf_bytes),
        as_attachment=False,
        download_name=f"Sales_{start.isoformat()}_to_{end.isoformat()}.pdf",
        mimetype="application/pdf",
    )


@reports_bp.route("/gstr1")
@roles_required(UserRole.CLIENT_ADMIN)
def gstr1_view():
    tenant = current_user.tenant
    if tenant.registration_type != RegistrationType.REGULAR:
        flash("GSTR-1 applies to Regular scheme GSTINs - composition and unregistered clients don't file it.", "error")
        return redirect(url_for("reports.index"))
    gstin = _resolve_gstin(request.args.get("gstin_id", type=int))
    period = request.args.get("period", _current_period())
    try:
        data = gstr1_data(tenant, gstin, period)
    except ReportPeriodError as exc:
        flash(exc.message, "error")
        return redirect(url_for("reports.index"))
    return render_template("reports/gstr1.html", data=data)


@reports_bp.route("/gstr1.xlsx")
@roles_required(UserRole.CLIENT_ADMIN)
def gstr1_xlsx():
    data = gstr1_data(current_user.tenant, _resolve_gstin(request.args.get("gstin_id", type=int)), request.args.get("period", _current_period()))
    buf = build_gstr1_workbook(data)
    return send_file(buf, as_attachment=True, download_name=f"GSTR1_{data['period']}.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@reports_bp.route("/gstr1.pdf")
@roles_required(UserRole.CLIENT_ADMIN)
def gstr1_pdf():
    data = gstr1_data(current_user.tenant, _resolve_gstin(request.args.get("gstin_id", type=int)), request.args.get("period", _current_period()))
    pdf_bytes = render_gstr1_pdf(data)
    return send_file(BytesIO(pdf_bytes), as_attachment=False, download_name=f"GSTR1_{data['period']}.pdf", mimetype="application/pdf")


@reports_bp.route("/gstr1.json")
@roles_required(UserRole.CLIENT_ADMIN)
def gstr1_json():
    data = gstr1_data(current_user.tenant, _resolve_gstin(request.args.get("gstin_id", type=int)), request.args.get("period", _current_period()))
    return send_file(BytesIO(to_json_bytes(data)), as_attachment=True, download_name=f"GSTR1_{data['period']}.json", mimetype="application/json")


@reports_bp.route("/gstr3b")
@roles_required(UserRole.CLIENT_ADMIN)
def gstr3b_view():
    tenant = current_user.tenant
    if tenant.registration_type != RegistrationType.REGULAR:
        flash("GSTR-3B applies to Regular scheme GSTINs - composition and unregistered clients don't file it.", "error")
        return redirect(url_for("reports.index"))
    gstin = _resolve_gstin(request.args.get("gstin_id", type=int))
    period = request.args.get("period", _current_period())
    try:
        data = gstr3b_data(tenant, gstin, period)
    except ReportPeriodError as exc:
        flash(exc.message, "error")
        return redirect(url_for("reports.index"))
    return render_template("reports/gstr3b.html", data=data)


@reports_bp.route("/gstr3b.xlsx")
@roles_required(UserRole.CLIENT_ADMIN)
def gstr3b_xlsx():
    data = gstr3b_data(current_user.tenant, _resolve_gstin(request.args.get("gstin_id", type=int)), request.args.get("period", _current_period()))
    buf = build_gstr3b_workbook(data)
    return send_file(buf, as_attachment=True, download_name=f"GSTR3B_{data['period']}.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@reports_bp.route("/gstr3b.pdf")
@roles_required(UserRole.CLIENT_ADMIN)
def gstr3b_pdf():
    data = gstr3b_data(current_user.tenant, _resolve_gstin(request.args.get("gstin_id", type=int)), request.args.get("period", _current_period()))
    pdf_bytes = render_gstr3b_pdf(data)
    return send_file(BytesIO(pdf_bytes), as_attachment=False, download_name=f"GSTR3B_{data['period']}.pdf", mimetype="application/pdf")


@reports_bp.route("/gstr3b.json")
@roles_required(UserRole.CLIENT_ADMIN)
def gstr3b_json():
    data = gstr3b_data(current_user.tenant, _resolve_gstin(request.args.get("gstin_id", type=int)), request.args.get("period", _current_period()))
    return send_file(BytesIO(to_json_bytes(data)), as_attachment=True, download_name=f"GSTR3B_{data['period']}.json", mimetype="application/json")
