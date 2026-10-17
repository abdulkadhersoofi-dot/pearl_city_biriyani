from datetime import date
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
from app.reports.filters import branch_choices, parse_report_filters
from app.reports.gstr import ReportPeriodError, gstr1_data, gstr3b_data
from app.reports.items import item_sales_data
from app.reports.sales import sales_report_data
from app.reports.stock import stock_report_data
from app.utils.features import feature_required


def _current_period() -> str:
    today = date.today()
    return f"{today.year:04d}-{today.month:02d}"


def _resolve_gstin(gstin_id, default=False):
    """The signed-in client's GSTIN by id (404 for another client's). With
    default=True and no id given - the menu's "GST returns" link - their
    primary active GSTIN, else their first active one."""
    if gstin_id is None and default:
        active = Gstin.query.filter_by(tenant_id=current_user.tenant_id, is_active=True)
        gstin = active.filter_by(is_primary=True).first() or active.order_by(Gstin.id).first()
    else:
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
    from app.reports.hub import hub_groups

    return render_template(
        "reports/index.html",
        tenant=tenant,
        gstins=gstins,
        period=period,
        not_regular=not_regular,
        groups=hub_groups(),
    )


@reports_bp.route("/sales")
@roles_required(UserRole.CLIENT_ADMIN)
def sales_view():
    filters = parse_report_filters(request.args)
    data = sales_report_data(current_user.tenant, filters.start, filters.end, filters.branch_ids)
    return render_template("reports/sales.html", data=data, filters=filters, branches=branch_choices(current_user.tenant))


@reports_bp.route("/sales.xlsx")
@roles_required(UserRole.CLIENT_ADMIN)
def sales_xlsx():
    filters = parse_report_filters(request.args)
    data = sales_report_data(current_user.tenant, filters.start, filters.end, filters.branch_ids)
    buf = build_sales_workbook(data)
    return send_file(
        buf,
        as_attachment=True,
        download_name=f"Sales_{filters.start.isoformat()}_to_{filters.end.isoformat()}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@reports_bp.route("/sales.pdf")
@roles_required(UserRole.CLIENT_ADMIN)
def sales_pdf():
    filters = parse_report_filters(request.args)
    data = sales_report_data(current_user.tenant, filters.start, filters.end, filters.branch_ids)
    pdf_bytes = render_sales_pdf(data)
    return send_file(
        BytesIO(pdf_bytes),
        as_attachment=False,
        download_name=f"Sales_{filters.start.isoformat()}_to_{filters.end.isoformat()}.pdf",
        mimetype="application/pdf",
    )


@reports_bp.route("/stock")
@roles_required(UserRole.CLIENT_ADMIN)
def stock_view():
    filters = parse_report_filters(request.args)
    data = stock_report_data(current_user.tenant, filters.start, filters.end, filters.branch_ids)
    return render_template("reports/stock.html", data=data, filters=filters, branches=branch_choices(current_user.tenant))


@reports_bp.route("/items")
@roles_required(UserRole.CLIENT_ADMIN)
def items_view():
    filters = parse_report_filters(request.args)
    data = item_sales_data(current_user.tenant, filters.start, filters.end, filters.branch_ids)
    return render_template("reports/items.html", data=data, filters=filters, branches=branch_choices(current_user.tenant))


@reports_bp.route("/gstr1")
@roles_required(UserRole.CLIENT_ADMIN)
def gstr1_view():
    tenant = current_user.tenant
    if tenant.registration_type != RegistrationType.REGULAR:
        flash("GSTR-1 applies to Regular scheme GSTINs - composition and unregistered clients don't file it.", "error")
        return redirect(url_for("reports.index"))
    gstin = _resolve_gstin(request.args.get("gstin_id", type=int), default=True)
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
    data = gstr1_data(current_user.tenant, _resolve_gstin(request.args.get("gstin_id", type=int), default=True), request.args.get("period", _current_period()))
    buf = build_gstr1_workbook(data)
    return send_file(buf, as_attachment=True, download_name=f"GSTR1_{data['period']}.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@reports_bp.route("/gstr1.pdf")
@roles_required(UserRole.CLIENT_ADMIN)
def gstr1_pdf():
    data = gstr1_data(current_user.tenant, _resolve_gstin(request.args.get("gstin_id", type=int), default=True), request.args.get("period", _current_period()))
    pdf_bytes = render_gstr1_pdf(data)
    return send_file(BytesIO(pdf_bytes), as_attachment=False, download_name=f"GSTR1_{data['period']}.pdf", mimetype="application/pdf")


@reports_bp.route("/gstr1.json")
@roles_required(UserRole.CLIENT_ADMIN)
def gstr1_json():
    data = gstr1_data(current_user.tenant, _resolve_gstin(request.args.get("gstin_id", type=int), default=True), request.args.get("period", _current_period()))
    return send_file(BytesIO(to_json_bytes(data)), as_attachment=True, download_name=f"GSTR1_{data['period']}.json", mimetype="application/json")


@reports_bp.route("/gstr3b")
@roles_required(UserRole.CLIENT_ADMIN)
def gstr3b_view():
    tenant = current_user.tenant
    if tenant.registration_type != RegistrationType.REGULAR:
        flash("GSTR-3B applies to Regular scheme GSTINs - composition and unregistered clients don't file it.", "error")
        return redirect(url_for("reports.index"))
    gstin = _resolve_gstin(request.args.get("gstin_id", type=int), default=True)
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
    data = gstr3b_data(current_user.tenant, _resolve_gstin(request.args.get("gstin_id", type=int), default=True), request.args.get("period", _current_period()))
    buf = build_gstr3b_workbook(data)
    return send_file(buf, as_attachment=True, download_name=f"GSTR3B_{data['period']}.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@reports_bp.route("/gstr3b.pdf")
@roles_required(UserRole.CLIENT_ADMIN)
def gstr3b_pdf():
    data = gstr3b_data(current_user.tenant, _resolve_gstin(request.args.get("gstin_id", type=int), default=True), request.args.get("period", _current_period()))
    pdf_bytes = render_gstr3b_pdf(data)
    return send_file(BytesIO(pdf_bytes), as_attachment=False, download_name=f"GSTR3B_{data['period']}.pdf", mimetype="application/pdf")


@reports_bp.route("/gstr3b.json")
@roles_required(UserRole.CLIENT_ADMIN)
def gstr3b_json():
    data = gstr3b_data(current_user.tenant, _resolve_gstin(request.args.get("gstin_id", type=int), default=True), request.args.get("period", _current_period()))
    return send_file(BytesIO(to_json_bytes(data)), as_attachment=True, download_name=f"GSTR3B_{data['period']}.json", mimetype="application/json")


# --- margin register (module "margin_scheme") ----------------------------------------

def _margin_register():
    from app.margin.reports import register_data

    filters = parse_report_filters(request.args)
    return filters, register_data(current_user.tenant, filters.start, filters.end)


@reports_bp.route("/margin-register")
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("margin_scheme")
def margin_register():
    filters, data = _margin_register()
    return render_template("reports/margin_register.html", data=data, filters=filters, branches=[])


@reports_bp.route("/margin-register.xlsx")
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("margin_scheme")
def margin_register_xlsx():
    from app.margin.reports import build_register_workbook

    filters, data = _margin_register()
    return send_file(build_register_workbook(data), as_attachment=True,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     download_name=f"Margin_register_{filters.start.isoformat()}_to_{filters.end.isoformat()}.xlsx")


@reports_bp.route("/margin-register.pdf")
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("margin_scheme")
def margin_register_pdf():
    from app.margin.reports import render_register_pdf

    filters, data = _margin_register()
    return send_file(BytesIO(render_register_pdf(data)), mimetype="application/pdf",
                     download_name=f"Margin_register_{filters.start.isoformat()}_to_{filters.end.isoformat()}.pdf")


# --- Phase 7: business reports (feature "business_reports") ------------------------

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _filters(default_mode: str):
    """The shared period filter, defaulting to this month / today when the
    page is first opened."""
    from werkzeug.datastructures import MultiDict

    args = request.args
    if not args.get("mode"):
        args = MultiDict(args)
        args["mode"] = default_mode if not (args.get("start") or args.get("end")) else "range"
    return parse_report_filters(args)


def _month_filters():
    return _filters("month")


def _location(tenant):
    from app.finance.pnl import own_locations

    locations = own_locations(tenant)
    wanted = request.args.get("location_id", type=int)
    chosen = next((l for l in locations if l.id == wanted), None)
    return locations, chosen


def _pnl():
    from app.finance.pnl import statement

    tenant = current_user.tenant
    filters = _month_filters()
    locations, chosen = _location(tenant)
    compare = request.args.get("compare", "previous")
    if compare not in ("previous", "last_year", "none"):
        compare = "previous"
    st = statement(tenant, filters.start, filters.end, chosen.id if chosen else None, compare)
    return st, filters, locations, chosen, compare


@reports_bp.route("/dashboard")
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("business_reports")
def dashboard():
    from app.finance.dashboard import dashboard as dashboard_data

    tenant = current_user.tenant
    summary = summary_link = None
    if tenant.has_feature("daily_summary"):
        from app.finance.owner_summary import owner_summary as build, summary_text, whatsapp_link

        summary = build(tenant)
        summary_link = whatsapp_link(summary_text(tenant, summary), current_user.phone)
    from app.navigation import needs_you

    d = dashboard_data(tenant)
    peak = max([v for _, v in d["series"]] + [0])
    return render_template("reports/dashboard.html", d=d, summary=summary, summary_whatsapp=summary_link,
                           needs=needs_you(tenant), peak=peak)


@reports_bp.route("/profit-loss")
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("business_reports")
def profit_loss():
    st, filters, locations, chosen, compare = _pnl()
    return render_template("reports/profit_loss.html", st=st, filters=filters, locations=locations, chosen=chosen,
                           compare=compare)


@reports_bp.route("/profit-loss.<any(xlsx, pdf):fmt>")
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("business_reports")
def profit_loss_export(fmt):
    from app.finance.exports import pnl_workbook, render_pnl_pdf

    st, filters, locations, chosen, compare = _pnl()
    name = chosen.name if chosen else "All locations"
    stem = f"Profit_and_loss_{filters.start.isoformat()}_to_{filters.end.isoformat()}"
    if fmt == "xlsx":
        return send_file(pnl_workbook(st, name), as_attachment=True, download_name=f"{stem}.xlsx", mimetype=XLSX)
    return send_file(BytesIO(render_pnl_pdf(st, name)), as_attachment=False, download_name=f"{stem}.pdf",
                     mimetype="application/pdf")


def _item_profit():
    from app.finance.sales_analysis import item_gross_profit

    tenant = current_user.tenant
    filters = _month_filters()
    locations, chosen = _location(tenant)
    return item_gross_profit(tenant, filters.start, filters.end, chosen.id if chosen else None), filters, locations, chosen


@reports_bp.route("/item-profit")
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("business_reports")
def item_profit():
    data, filters, locations, chosen = _item_profit()
    args = request.args.to_dict()
    view_urls = {v: url_for("reports.item_profit", **{**args, "view": v}) for v in ("items", "categories")}
    return render_template("reports/item_profit.html", data=data, filters=filters, locations=locations, chosen=chosen,
                           view=request.args.get("view", "items"), view_urls=view_urls)


@reports_bp.route("/item-profit.xlsx")
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("business_reports")
def item_profit_xlsx():
    from app.finance.exports import item_profit_workbook

    data, filters, _, _ = _item_profit()
    return send_file(item_profit_workbook(data, current_user.tenant), as_attachment=True, mimetype=XLSX,
                     download_name=f"Gross_profit_{filters.start.isoformat()}_to_{filters.end.isoformat()}.xlsx")


def _daily():
    from app.finance.sales_analysis import daily_summary

    filters = _filters("day")
    return daily_summary(current_user.tenant, filters.start, filters.end), filters


@reports_bp.route("/daily-summary")
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("business_reports")
def daily_summary_view():
    data, filters = _daily()
    return render_template("reports/daily_summary.html", data=data, filters=filters, locations=[], chosen=None)


@reports_bp.route("/daily-summary.pdf")
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("business_reports")
def daily_summary_pdf():
    from app.finance.exports import render_daily_pdf

    data, filters = _daily()
    return send_file(BytesIO(render_daily_pdf(data, current_user.tenant)), mimetype="application/pdf",
                     as_attachment=False, download_name=f"Daily_summary_{filters.start.isoformat()}.pdf")


@reports_bp.route("/<any(purchase, expense):kind>-register.xlsx")
@roles_required(UserRole.CLIENT_ADMIN)
def register_xlsx(kind):
    from app.reports.registers import expense_register_workbook, purchase_register_workbook

    tenant = current_user.tenant
    if not tenant.has_feature("purchases" if kind == "purchase" else "expenses"):
        abort(404)
    filters = _month_filters()
    build = purchase_register_workbook if kind == "purchase" else expense_register_workbook
    return send_file(build(tenant, filters.start, filters.end), as_attachment=True, mimetype=XLSX,
                     download_name=f"{kind.title()}_register_{filters.start.isoformat()}_to_{filters.end.isoformat()}.xlsx")


def _previous_period() -> str:
    first = date.today().replace(day=1)
    last = first.fromordinal(first.toordinal() - 1)
    return f"{last.year:04d}-{last.month:02d}"


def filing_cards(tenant, period: str) -> list[dict]:
    """The client's own view of the firm's filing tracker for a month."""
    from app.finance.filing import due_date
    from app.models.filing import RETURN_LABELS, FilingStatus, ReturnType
    from app.utils.tenant_scope import tenant_query

    rows = {(s.gstin_id, s.return_type): s for s in tenant_query(FilingStatus).filter_by(period=period)}
    cards = []
    for gstin in tenant.gstins.filter_by(is_active=True).all():
        returns = [{"label": RETURN_LABELS[rt], "due": due_date(period, rt), "status": rows.get((gstin.id, rt))}
                   for rt in ReturnType]
        cards.append({"gstin": gstin, "returns": returns,
                      "ready": all(r["status"] is not None and r["status"].data_ready for r in returns)})
    return cards


@reports_bp.route("/filing", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("business_reports")
def filing_status():
    from app.finance.filing import mark_ready, period_bounds
    from app.utils.audit import record_audit
    from app.extensions import db

    tenant = current_user.tenant
    period = request.values.get("period") or _previous_period()
    try:
        period_bounds(period)
    except (ValueError, TypeError):
        abort(404)
    if request.method == "POST":
        gstin = _resolve_gstin(request.form.get("gstin_id", type=int))
        ready = request.form.get("action") != "not_ready"
        mark_ready(tenant.id, gstin.id, period, current_user, ready)
        record_audit(current_user, "filing_data_ready" if ready else "filing_data_not_ready", tenant_id=tenant.id,
                     entity_type="gstin", entity_id=gstin.id, details={"period": period})
        db.session.commit()
        flash(f"{period} marked {'ready for filing' if ready else 'not ready yet'}.", "success")
        return redirect(url_for("reports.filing_status", period=period))
    return render_template("reports/filing.html", cards=filing_cards(tenant, period), period=period)


# --- the owner's end-of-day summary (feature "daily_summary") ------------------------------

def _summary_day():
    try:
        return date.fromisoformat(request.args.get("day") or "")
    except ValueError:
        return date.today()


@reports_bp.route("/owner-summary")
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("daily_summary")
def owner_summary():
    from app.finance.owner_summary import email_ready, owner_emails, owner_summary as build, summary_text, whatsapp_link

    tenant = current_user.tenant
    day = _summary_day()
    data = build(tenant, day)
    text = summary_text(tenant, data)
    return render_template("reports/owner_summary.html", data=data, text=text,
                           whatsapp=whatsapp_link(text, current_user.phone), email_ready=email_ready(),
                           emails=owner_emails(tenant))


@reports_bp.route("/owner-summary/email", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("daily_summary")
def owner_summary_email():
    from app.extensions import db
    from app.finance.owner_summary import email_ready, owner_summary as build, send_email
    from app.utils.audit import record_audit

    tenant = current_user.tenant
    day = _summary_day()
    if not email_ready():
        abort(404)
    try:
        sent = send_email(tenant, build(tenant, day))
    except (OSError, Exception) as exc:  # smtplib raises a family of errors
        flash(f"The email couldn't be sent: {exc}", "error")
        return redirect(url_for("reports.owner_summary", day=day.isoformat()))
    record_audit(current_user, "owner_summary_emailed", tenant_id=tenant.id,
                 details={"day": day.isoformat(), "to": sent})
    db.session.commit()
    flash(f"Sent to {', '.join(sent)}." if sent else "No Client Admin email to send it to.", "success" if sent else "error")
    return redirect(url_for("reports.owner_summary", day=day.isoformat()))
