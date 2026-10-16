from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO

from flask import abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from weasyprint import HTML

from app.accounts import accounts_bp, books, payables, receivables, services
from app.accounts.services import AccountsError, Allocation, VoucherInput
from app.auth.decorators import roles_required
from app.extensions import db
from app.models.accounts import (
    MONEY_MODE_LABELS,
    ChequeStatus,
    CustomerReceipt,
    MoneyMode,
    RefundVoucher,
    SupplierPayment,
    VoucherStatus,
)
from app.models.customer import Customer
from app.models.purchase import Supplier
from app.models.tenant import Gstin, Tenant
from app.models.user import UserRole
from app.pos.share import whatsapp_link
from app.utils.audit import record_audit
from app.utils.features import feature_required
from app.utils.gst_config import GST_RATE_SLABS
from app.utils.indian_states import INDIAN_STATES
from app.utils.tenant_scope import assert_owns, tenant_query

ADMIN = UserRole.CLIENT_ADMIN
STATEMENT_SALT = "customer-statement-v1"
STATEMENT_LINK_DAYS = 15


def _date(value, default=None):
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


def _int(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _audit(action, entity_type, obj, **details):
    record_audit(current_user, action, tenant_id=current_user.tenant_id, entity_type=entity_type, entity_id=obj.id,
                 details={"number": getattr(obj, "number", None), **details})


def _mode(value) -> MoneyMode:
    try:
        return MoneyMode(value)
    except ValueError:
        raise AccountsError("Pick how the money was paid.")


def _allocations(form, kinds) -> list[Allocation]:
    """Amounts typed against each open bill: fields named alloc-<kind>-<id>."""
    out = []
    for key, value in form.items():
        if not key.startswith("alloc-") or not (value or "").strip():
            continue
        _, kind, item_id = key.split("-", 2)
        if kind not in kinds:
            continue
        amount = services.money(value)
        if amount:
            out.append(Allocation(kind, _int(item_id) or None, amount))
    return out


def _voucher_input(form, party_field) -> VoucherInput:
    return VoucherInput(
        party_id=_int(form.get(party_field)),
        voucher_date=_date(form.get("voucher_date"), date.today()),
        amount=services.money(form.get("amount")),
        mode=_mode(form.get("mode", "cash")),
        reference=form.get("reference"),
        cheque_no=form.get("cheque_no"),
        cheque_date=_date(form.get("cheque_date")),
        bank_name=form.get("bank_name"),
        notes=form.get("notes"),
        gstin_id=_int(form.get("gstin_id")),
    )


def _gstins():
    return tenant_query(Gstin).filter_by(is_active=True).order_by(Gstin.is_primary.desc(), Gstin.id).all()


def _pdf(template, filename, **ctx):
    html = render_template(template, **ctx)
    pdf = HTML(string=html, base_url=current_app.root_path).write_pdf()
    return send_file(BytesIO(pdf), mimetype="application/pdf", as_attachment=False, download_name=filename)


# --- receivables -------------------------------------------------------------------

def _customer(customer_id) -> Customer:
    customer = tenant_query(Customer).filter_by(id=customer_id).first_or_404()
    assert_owns(customer)
    return customer


@accounts_bp.route("/receivables")
@roles_required(ADMIN)
@feature_required("receivables")
def receivables_index():
    as_of = _date(request.args.get("as_of"), date.today())
    rows = receivables.customer_balances(current_user.tenant, as_of)
    keys = [name for name, _, _ in receivables.AGE_BUCKETS] + ["on_account", "balance"]
    totals = {k: sum((r["ageing"][k] for r in rows), Decimal("0")) for k in keys}
    return render_template("accounts/receivables.html", rows=rows, totals=totals, as_of=as_of,
                           buckets=receivables.AGE_BUCKETS)


@accounts_bp.route("/customers/<int:customer_id>")
@roles_required(ADMIN)
@feature_required("receivables")
def customer_ledger(customer_id):
    customer = _customer(customer_id)
    tenant = current_user.tenant
    account = receivables.customer_account(tenant, customer)
    today = date.today()
    return render_template(
        "accounts/customer.html", customer=customer, account=account, ageing=account.ageing(today),
        buckets=receivables.AGE_BUCKETS, today=today, statement_start=today.replace(day=1) - timedelta(days=90),
        receipts=tenant_query(CustomerReceipt).filter_by(customer_id=customer.id)
        .order_by(CustomerReceipt.voucher_date.desc(), CustomerReceipt.id.desc()).limit(50).all(),
    )


def _statement_range():
    end = _date(request.args.get("end"), date.today())
    start = _date(request.args.get("start"), end - timedelta(days=90))
    if start > end:
        start, end = end, start
    return start, end


@accounts_bp.route("/customers/<int:customer_id>/statement.pdf")
@roles_required(ADMIN)
@feature_required("receivables")
def customer_statement(customer_id):
    customer = _customer(customer_id)
    start, end = _statement_range()
    data = receivables.statement(current_user.tenant, customer, start, end)
    return _pdf("accounts/statement_pdf.html", f"statement-{customer.id}-{end.isoformat()}.pdf", data=data,
                buckets=receivables.AGE_BUCKETS)


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=STATEMENT_SALT)


@accounts_bp.route("/customers/<int:customer_id>/remind")
@roles_required(ADMIN)
@feature_required("receivables")
def customer_reminder(customer_id):
    """WhatsApp click-to-chat with the balance and a signed, expiring link to
    the customer's statement - no WhatsApp API is called."""
    customer = _customer(customer_id)
    tenant = current_user.tenant
    balance = receivables.balance_of(tenant, customer)
    end = date.today()
    token = _serializer().dumps({"t": tenant.id, "c": customer.id, "s": (end - timedelta(days=90)).isoformat(),
                                 "e": end.isoformat()})
    link = url_for("accounts.shared_statement", token=token, _external=True)
    text = (f"Dear {customer.name}, this is a reminder from {tenant.display_name}: Rs. {balance:,.2f} is outstanding "
            f"on your account. Statement: {link}")
    _audit("payment_reminder_sent", "customer", customer, balance=str(balance))
    db.session.commit()
    return redirect(whatsapp_link(customer.phone, text))


@accounts_bp.route("/shared/statement/<token>.pdf")
def shared_statement(token):
    """Public, no sign-in: only through the signed link in a reminder."""
    try:
        data = _serializer().loads(token, max_age=STATEMENT_LINK_DAYS * 86400)
        tenant_id, customer_id = int(data["t"]), int(data["c"])
        start, end = date.fromisoformat(data["s"]), date.fromisoformat(data["e"])
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError):
        abort(404)
    tenant = db.session.get(Tenant, tenant_id)
    customer = Customer.query.filter_by(id=customer_id, tenant_id=tenant_id).first()
    if tenant is None or customer is None or not tenant.is_active or not tenant.has_feature("receivables"):
        abort(404)
    stmt = receivables.statement(tenant, customer, start, end)
    html = render_template("accounts/statement_pdf.html", data=stmt, buckets=receivables.AGE_BUCKETS,
                           firm_name=tenant.display_name)
    pdf = HTML(string=html, base_url=current_app.root_path).write_pdf()
    return send_file(BytesIO(pdf), mimetype="application/pdf", as_attachment=False,
                     download_name=f"statement-{end.isoformat()}.pdf")


# --- receipts ----------------------------------------------------------------------

def _receipt(receipt_id) -> CustomerReceipt:
    receipt = tenant_query(CustomerReceipt).filter_by(id=receipt_id).first_or_404()
    assert_owns(receipt)
    return receipt


@accounts_bp.route("/receipts")
@roles_required(ADMIN)
@feature_required("receivables")
def receipts():
    query = tenant_query(CustomerReceipt)
    kind = request.args.get("kind", "")
    if kind == "advance":
        query = query.filter_by(is_advance=True)
    elif kind == "pending_cheques":
        query = query.filter_by(cheque_status=ChequeStatus.PENDING, status=VoucherStatus.POSTED)
    rows = query.order_by(CustomerReceipt.voucher_date.desc(), CustomerReceipt.id.desc()).limit(300).all()
    refunds = tenant_query(RefundVoucher).order_by(RefundVoucher.voucher_date.desc()).limit(50).all() \
        if kind == "advance" else []
    return render_template("accounts/receipts.html", rows=rows, kind=kind, refunds=refunds, modes=MONEY_MODE_LABELS)


@accounts_bp.route("/receipts/new", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("receivables")
def new_receipt():
    tenant = current_user.tenant
    form = request.form if request.method == "POST" else request.args
    is_advance = form.get("advance") == "1"
    customer = None
    customer_id = _int(form.get("customer_id"))
    if customer_id:
        customer = tenant_query(Customer).filter_by(id=customer_id).first()
    if request.method == "POST":
        try:
            data = _voucher_input(request.form, "customer_id")
            data.allocations = [] if is_advance else _allocations(request.form, ("opening", "invoice", "pos_bill"))
            if is_advance:
                data.is_advance = True
                data.for_services = request.form.get("for_services") == "1"
                data.gst_rate = services.money(request.form.get("gst_rate") or "0", "GST rate")
                data.place_of_supply_state_code = request.form.get("place_of_supply_state_code") or None
            receipt = services.create_receipt(tenant, current_user, data)
            if tenant.has_feature("wholesale") and request.form.get("salesman_id"):
                from app.models.business import Salesman

                salesman = tenant_query(Salesman).filter_by(id=_int(request.form.get("salesman_id")) or 0).first()
                if salesman is None:
                    raise AccountsError("Pick a valid salesman.")
                receipt.salesman_id = salesman.id
            _audit("receipt_created", "customer_receipt", receipt, amount=str(receipt.amount),
                   customer_id=receipt.customer_id, advance=receipt.is_advance,
                   allocated=str(services.receipt_used(receipt)))
            db.session.commit()
            flash(f"{'Advance receipt voucher' if receipt.is_advance else 'Receipt'} {receipt.number} saved.", "success")
            return redirect(url_for("accounts.view_receipt", receipt_id=receipt.id))
        except AccountsError as exc:
            db.session.rollback()
            flash(exc.message, "error")
    account = receivables.customer_account(tenant, customer, with_entries=False) if customer else None
    gstins = _gstins()
    return render_template(
        "accounts/receipt_form.html", form=form, customer=customer, account=account, is_advance=is_advance,
        customers=tenant_query(Customer).filter_by(is_active=True).order_by(Customer.name).all(),
        modes=MONEY_MODE_LABELS, gstins=gstins, rates=GST_RATE_SLABS, states=INDIAN_STATES,
        gst_on_advances=bool(gstins) and services.charges_gst_on_advances(tenant, gstins[0]),
        today=date.today(),
    )


@accounts_bp.route("/receipts/<int:receipt_id>")
@roles_required(ADMIN)
@feature_required("receivables")
def view_receipt(receipt_id):
    receipt = _receipt(receipt_id)
    tenant = current_user.tenant
    account = receivables.customer_account(tenant, receipt.customer, with_entries=False)
    usage = services.advance_usage(receipt) if receipt.is_advance else []
    unused = services.receipt_unused(receipt)
    can_allocate = receipt.settles and unused > 0
    can_refund = can_allocate and (receipt.mode != MoneyMode.CHEQUE or receipt.cheque_status == ChequeStatus.CLEARED)
    return render_template("accounts/receipt_view.html", receipt=receipt, account=account, modes=MONEY_MODE_LABELS,
                           unused=unused, usage=usage, today=date.today(), can_allocate=can_allocate,
                           can_refund=can_refund)


@accounts_bp.route("/receipts/<int:receipt_id>/pdf")
@roles_required(ADMIN)
@feature_required("receivables")
def receipt_pdf(receipt_id):
    receipt = _receipt(receipt_id)
    return _pdf("accounts/voucher_pdf.html", f"{receipt.number.replace('/', '-')}.pdf", voucher=receipt,
                kind="advance" if receipt.is_advance else "receipt", modes=MONEY_MODE_LABELS,
                party=receipt.customer, usage=[])


@accounts_bp.route("/receipts/<int:receipt_id>/allocate", methods=["POST"])
@roles_required(ADMIN)
@feature_required("receivables")
def allocate_receipt(receipt_id):
    receipt = _receipt(receipt_id)
    try:
        allocations = _allocations(request.form, ("opening", "invoice", "pos_bill"))
        if not allocations:
            raise AccountsError("Enter an amount against at least one bill.")
        on = _date(request.form.get("allocated_on"), date.today())
        services.allocate_receipt(current_user.tenant, receipt, allocations, on)
        _audit("receipt_allocated", "customer_receipt", receipt,
               allocations=[{"kind": a.kind, "id": a.item_id, "amount": str(a.amount)} for a in allocations])
        db.session.commit()
        flash("Set against the bills.", "success")
    except AccountsError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("accounts.view_receipt", receipt_id=receipt.id))


@accounts_bp.route("/receipts/<int:receipt_id>/allocations/<int:allocation_id>/remove", methods=["POST"])
@roles_required(ADMIN)
@feature_required("receivables")
def remove_receipt_allocation(receipt_id, allocation_id):
    receipt = _receipt(receipt_id)
    try:
        alloc = services.remove_receipt_allocation(receipt, allocation_id)
        _audit("receipt_allocation_removed", "customer_receipt", receipt, amount=str(alloc.amount),
               invoice_id=alloc.invoice_id, pos_bill_id=alloc.pos_bill_id, opening=alloc.is_opening)
        db.session.commit()
        flash("Taken off that bill - the amount is back on account.", "success")
    except AccountsError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("accounts.view_receipt", receipt_id=receipt.id))


def _cheque_status_from_form():
    try:
        return ChequeStatus(request.form.get("status", "")), _date(request.form.get("on"), date.today())
    except ValueError:
        raise AccountsError("Mark the cheque cleared or bounced.")


@accounts_bp.route("/receipts/<int:receipt_id>/cheque", methods=["POST"])
@roles_required(ADMIN)
@feature_required("receivables")
def receipt_cheque(receipt_id):
    receipt = _receipt(receipt_id)
    try:
        status, on = _cheque_status_from_form()
        services.set_receipt_cheque_status(receipt, status, on)
        _audit("cheque_" + status.value, "customer_receipt", receipt, cheque_no=receipt.cheque_no, on=on.isoformat())
        db.session.commit()
        flash(f"Cheque marked {status.value}.", "success")
    except AccountsError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("accounts.view_receipt", receipt_id=receipt.id))


@accounts_bp.route("/receipts/<int:receipt_id>/void", methods=["POST"])
@roles_required(ADMIN)
@feature_required("receivables")
def void_receipt(receipt_id):
    receipt = _receipt(receipt_id)
    try:
        services.void_receipt(receipt, current_user, request.form.get("reason", ""))
        _audit("receipt_voided", "customer_receipt", receipt, reason=receipt.void_reason)
        db.session.commit()
        flash(f"{receipt.number} voided.", "success")
    except AccountsError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("accounts.view_receipt", receipt_id=receipt.id))


@accounts_bp.route("/receipts/<int:receipt_id>/refund", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("receivables")
def new_refund(receipt_id):
    receipt = _receipt(receipt_id)
    if request.method == "POST":
        try:
            refund = services.create_refund(
                current_user.tenant, current_user, receipt, services.money(request.form.get("amount")),
                _date(request.form.get("voucher_date"), date.today()), _mode(request.form.get("mode", "cash")),
                request.form.get("reference"), request.form.get("reason", ""),
            )
            _audit("refund_voucher_created", "refund_voucher", refund, amount=str(refund.amount),
                   receipt=receipt.number)
            db.session.commit()
            flash(f"Refund voucher {refund.number} saved.", "success")
            return redirect(url_for("accounts.view_refund", refund_id=refund.id))
        except AccountsError as exc:
            db.session.rollback()
            flash(exc.message, "error")
    return render_template("accounts/refund_form.html", receipt=receipt, unused=services.receipt_unused(receipt),
                           modes=MONEY_MODE_LABELS, form=request.form, today=date.today())


def _refund(refund_id) -> RefundVoucher:
    refund = tenant_query(RefundVoucher).filter_by(id=refund_id).first_or_404()
    assert_owns(refund)
    return refund


@accounts_bp.route("/refunds/<int:refund_id>")
@roles_required(ADMIN)
@feature_required("receivables")
def view_refund(refund_id):
    return render_template("accounts/refund_view.html", refund=_refund(refund_id), modes=MONEY_MODE_LABELS)


@accounts_bp.route("/refunds/<int:refund_id>/pdf")
@roles_required(ADMIN)
@feature_required("receivables")
def refund_pdf(refund_id):
    refund = _refund(refund_id)
    return _pdf("accounts/voucher_pdf.html", f"{refund.number.replace('/', '-')}.pdf", voucher=refund, kind="refund",
                modes=MONEY_MODE_LABELS, party=refund.customer, usage=[])


@accounts_bp.route("/refunds/<int:refund_id>/void", methods=["POST"])
@roles_required(ADMIN)
@feature_required("receivables")
def void_refund(refund_id):
    refund = _refund(refund_id)
    try:
        services.void_refund(refund, current_user, request.form.get("reason", ""))
        _audit("refund_voucher_voided", "refund_voucher", refund, reason=refund.void_reason)
        db.session.commit()
        flash(f"{refund.number} voided.", "success")
    except AccountsError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("accounts.view_refund", refund_id=refund.id))


# --- payables ----------------------------------------------------------------------

def _supplier(supplier_id) -> Supplier:
    supplier = tenant_query(Supplier).filter_by(id=supplier_id).first_or_404()
    assert_owns(supplier)
    return supplier


def _payment(payment_id) -> SupplierPayment:
    payment = tenant_query(SupplierPayment).filter_by(id=payment_id).first_or_404()
    assert_owns(payment)
    return payment


@accounts_bp.route("/payables")
@roles_required(ADMIN)
@feature_required("payables")
def payables_index():
    as_of = _date(request.args.get("as_of"), date.today())
    rows = payables.supplier_balances(current_user.tenant, as_of)
    keys = [name for name, _, _ in receivables.AGE_BUCKETS] + ["on_account", "balance"]
    totals = {k: sum((r["ageing"][k] for r in rows), Decimal("0")) for k in keys}
    return render_template("accounts/payables.html", rows=rows, totals=totals, as_of=as_of,
                           buckets=receivables.AGE_BUCKETS)


@accounts_bp.route("/suppliers/<int:supplier_id>")
@roles_required(ADMIN)
@feature_required("payables")
def supplier_ledger(supplier_id):
    supplier = _supplier(supplier_id)
    account = payables.supplier_account(current_user.tenant, supplier)
    today = date.today()
    return render_template(
        "accounts/supplier.html", supplier=supplier, account=account, ageing=account.ageing(today),
        buckets=receivables.AGE_BUCKETS, today=today,
        payments=tenant_query(SupplierPayment).filter_by(supplier_id=supplier.id)
        .order_by(SupplierPayment.voucher_date.desc(), SupplierPayment.id.desc()).limit(50).all(),
    )


@accounts_bp.route("/payments")
@roles_required(ADMIN)
@feature_required("payables")
def payments():
    query = tenant_query(SupplierPayment)
    if request.args.get("kind") == "pending_cheques":
        query = query.filter_by(cheque_status=ChequeStatus.PENDING, status=VoucherStatus.POSTED)
    rows = query.order_by(SupplierPayment.voucher_date.desc(), SupplierPayment.id.desc()).limit(300).all()
    return render_template("accounts/payments.html", rows=rows, kind=request.args.get("kind", ""),
                           modes=MONEY_MODE_LABELS)


@accounts_bp.route("/payments/new", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required("payables")
def new_payment():
    tenant = current_user.tenant
    form = request.form if request.method == "POST" else request.args
    supplier_id = _int(form.get("supplier_id"))
    supplier = tenant_query(Supplier).filter_by(id=supplier_id).first() if supplier_id else None
    if request.method == "POST":
        try:
            data = _voucher_input(request.form, "supplier_id")
            data.allocations = _allocations(request.form, ("opening", "purchase_bill", "expense"))
            payment = services.create_payment(tenant, current_user, data)
            _audit("payment_created", "supplier_payment", payment, amount=str(payment.amount),
                   supplier_id=payment.supplier_id)
            db.session.commit()
            flash(f"Payment voucher {payment.number} saved.", "success")
            return redirect(url_for("accounts.view_payment", payment_id=payment.id))
        except AccountsError as exc:
            db.session.rollback()
            flash(exc.message, "error")
    account = payables.supplier_account(tenant, supplier, with_entries=False) if supplier else None
    return render_template(
        "accounts/payment_form.html", form=form, supplier=supplier, account=account, modes=MONEY_MODE_LABELS,
        suppliers=tenant_query(Supplier).filter_by(is_active=True).order_by(Supplier.name).all(), gstins=_gstins(),
        today=date.today(),
    )


@accounts_bp.route("/payments/<int:payment_id>")
@roles_required(ADMIN)
@feature_required("payables")
def view_payment(payment_id):
    payment = _payment(payment_id)
    account = payables.supplier_account(current_user.tenant, payment.supplier, with_entries=False)
    unused = services.payment_unused(payment)
    return render_template("accounts/payment_view.html", payment=payment, account=account, modes=MONEY_MODE_LABELS,
                           unused=unused, today=date.today(), can_allocate=payment.settles and unused > 0)


@accounts_bp.route("/payments/<int:payment_id>/pdf")
@roles_required(ADMIN)
@feature_required("payables")
def payment_pdf(payment_id):
    payment = _payment(payment_id)
    return _pdf("accounts/voucher_pdf.html", f"{payment.number.replace('/', '-')}.pdf", voucher=payment,
                kind="payment", modes=MONEY_MODE_LABELS, party=payment.supplier, usage=[])


@accounts_bp.route("/payments/<int:payment_id>/allocate", methods=["POST"])
@roles_required(ADMIN)
@feature_required("payables")
def allocate_payment(payment_id):
    payment = _payment(payment_id)
    try:
        allocations = _allocations(request.form, ("opening", "purchase_bill", "expense"))
        if not allocations:
            raise AccountsError("Enter an amount against at least one bill.")
        services.allocate_payment(current_user.tenant, payment, allocations, date.today())
        _audit("payment_allocated", "supplier_payment", payment,
               allocations=[{"kind": a.kind, "id": a.item_id, "amount": str(a.amount)} for a in allocations])
        db.session.commit()
        flash("Set against the bills.", "success")
    except AccountsError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("accounts.view_payment", payment_id=payment.id))


@accounts_bp.route("/payments/<int:payment_id>/allocations/<int:allocation_id>/remove", methods=["POST"])
@roles_required(ADMIN)
@feature_required("payables")
def remove_payment_allocation(payment_id, allocation_id):
    payment = _payment(payment_id)
    try:
        alloc = services.remove_payment_allocation(payment, allocation_id)
        _audit("payment_allocation_removed", "supplier_payment", payment, amount=str(alloc.amount))
        db.session.commit()
        flash("Taken off that bill - the amount is back on account.", "success")
    except AccountsError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("accounts.view_payment", payment_id=payment.id))


@accounts_bp.route("/payments/<int:payment_id>/cheque", methods=["POST"])
@roles_required(ADMIN)
@feature_required("payables")
def payment_cheque(payment_id):
    payment = _payment(payment_id)
    try:
        status, on = _cheque_status_from_form()
        services.set_payment_cheque_status(payment, status, on)
        _audit("cheque_" + status.value, "supplier_payment", payment, cheque_no=payment.cheque_no, on=on.isoformat())
        db.session.commit()
        flash(f"Cheque marked {status.value}.", "success")
    except AccountsError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("accounts.view_payment", payment_id=payment.id))


@accounts_bp.route("/payments/<int:payment_id>/void", methods=["POST"])
@roles_required(ADMIN)
@feature_required("payables")
def void_payment(payment_id):
    payment = _payment(payment_id)
    try:
        services.void_payment(payment, current_user, request.form.get("reason", ""))
        _audit("payment_voided", "supplier_payment", payment, reason=payment.void_reason)
        db.session.commit()
        flash(f"{payment.number} voided.", "success")
    except AccountsError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("accounts.view_payment", payment_id=payment.id))


# --- cash & bank book --------------------------------------------------------------

@accounts_bp.route("/<any(cash, bank):book>-book")
@roles_required(ADMIN)
@feature_required("cash_bank_book")
def book(book):
    tenant = current_user.tenant
    end = _date(request.args.get("end"), date.today())
    start = _date(request.args.get("start"), end.replace(day=1))
    if start > end:
        start, end = end, start
    data = books.book(tenant, book, start, end)
    return render_template("accounts/book.html", data=data, book=book, start=start, end=end, tenant=tenant)


@accounts_bp.route("/books/opening", methods=["POST"])
@roles_required(ADMIN)
def books_opening():
    """The books opening date and cash / bank opening balances - shared by
    customer accounts, supplier accounts and the cash & bank book."""
    tenant = current_user.tenant
    if not any(tenant.has_feature(k) for k in ("receivables", "payables", "cash_bank_book")):
        abort(404)
    try:
        tenant.books_opening_date = _date(request.form.get("books_opening_date"))
        tenant.cash_opening_balance = services.money(request.form.get("cash_opening_balance") or "0", "Cash in hand")
        tenant.bank_opening_balance = services.money(request.form.get("bank_opening_balance") or "0", "Bank balance")
        record_audit(current_user, "books_opening_set", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id,
                     details={"date": str(tenant.books_opening_date), "cash": str(tenant.cash_opening_balance),
                              "bank": str(tenant.bank_opening_balance)})
        db.session.commit()
        flash("Opening balances saved.", "success")
    except AccountsError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    back = request.form.get("back")
    if back in ("cash", "bank") and tenant.has_feature("cash_bank_book"):
        return redirect(url_for("accounts.book", book=back))
    if tenant.has_feature("receivables"):
        return redirect(url_for("accounts.receivables_index"))
    return redirect(url_for("accounts.payables_index"))
