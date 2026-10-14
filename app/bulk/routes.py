"""Bulk Excel import/export screens: download a template (or your current
data), fill it in, upload, check the row-by-row preview, confirm.

The uploaded file waits in the server-side session between preview and
confirm and is re-validated on confirm - so what gets saved always
reflects the database as it is at that moment, not as it was when the
preview was drawn.
"""

import base64
from datetime import date

from flask import abort, flash, redirect, render_template, request, send_file, session, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.bulk import bulk_bp
from app.bulk.excel import MAX_UPLOAD_BYTES, WorkbookError, build_export, build_template, preview
from app.bulk.specs import SPECS
from app.extensions import db
from app.models.user import UserRole
from app.utils.audit import record_audit

_SESSION_KEY = "bulk_upload"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_LIST_ENDPOINTS = {"products": "products.list_products", "customers": "customers.list_customers"}


def _spec_or_404(kind):
    spec = SPECS.get(kind)
    if spec is None:
        abort(404)
    return spec


@bulk_bp.route("/<kind>/")
@roles_required(UserRole.CLIENT_ADMIN)
def index(kind):
    spec = _spec_or_404(kind)
    return render_template("bulk/index.html", spec=spec, back_url=url_for(_LIST_ENDPOINTS[kind]))


@bulk_bp.route("/<kind>/template.xlsx")
@roles_required(UserRole.CLIENT_ADMIN)
def template(kind):
    spec = _spec_or_404(kind)
    return send_file(build_template(spec), as_attachment=True, download_name=f"{spec.kind}-template.xlsx", mimetype=_XLSX)


@bulk_bp.route("/<kind>/export.xlsx")
@roles_required(UserRole.CLIENT_ADMIN)
def export(kind):
    spec = _spec_or_404(kind)
    return send_file(
        build_export(spec, current_user.tenant),
        as_attachment=True,
        download_name=f"{spec.kind}-{date.today().isoformat()}.xlsx",
        mimetype=_XLSX,
    )


@bulk_bp.route("/<kind>/upload", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def upload(kind):
    spec = _spec_or_404(kind)
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose an Excel file to upload.", "error")
        return redirect(url_for("bulk.index", kind=kind))
    data = file.read(MAX_UPLOAD_BYTES + 1)
    try:
        rows, _ctx = preview(spec, current_user.tenant, data)
    except WorkbookError as exc:
        flash(str(exc), "error")
        return redirect(url_for("bulk.index", kind=kind))
    if not rows:
        flash("No rows found below the headings on the first sheet.", "error")
        return redirect(url_for("bulk.index", kind=kind))

    session[_SESSION_KEY] = {
        "kind": kind,
        "tenant_id": current_user.tenant_id,
        "filename": file.filename,
        "data": base64.b64encode(data).decode("ascii"),
    }
    return render_template("bulk/preview.html", spec=spec, rows=rows, filename=file.filename, **_counts(rows))


@bulk_bp.route("/<kind>/confirm", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def confirm(kind):
    spec = _spec_or_404(kind)
    pending = session.get(_SESSION_KEY)
    if not pending or pending.get("kind") != kind or pending.get("tenant_id") != current_user.tenant_id:
        flash("That upload has expired - upload the file again.", "error")
        return redirect(url_for("bulk.index", kind=kind))

    tenant = current_user.tenant
    data = base64.b64decode(pending["data"])
    try:
        rows, ctx = preview(spec, tenant, data)
    except WorkbookError as exc:
        flash(str(exc), "error")
        return redirect(url_for("bulk.index", kind=kind))

    counts = _counts(rows)
    for row in rows:
        if row.action != "error":
            spec.save_row(row, ctx)
    record_audit(
        current_user,
        f"bulk_import_{kind}",
        tenant_id=tenant.id,
        details={"file": pending.get("filename"), **counts},
    )
    db.session.commit()
    session.pop(_SESSION_KEY, None)

    message = f"{spec.label}: {counts['created']} added, {counts['updated']} updated."
    if counts["errors"]:
        message += f" {counts['errors']} row(s) with problems were skipped - fix them and upload again."
    flash(message, "success")
    return redirect(url_for(_LIST_ENDPOINTS[kind]))


def _counts(rows) -> dict:
    return {
        "created": sum(1 for r in rows if r.action == "create"),
        "updated": sum(1 for r in rows if r.action == "update"),
        "errors": sum(1 for r in rows if r.action == "error"),
    }
