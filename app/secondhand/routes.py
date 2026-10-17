"""Second-hand goods (module "margin_scheme"): one place for the whole
cycle, in order -

  /second-hand/              overview: the steps, used stock and this month
  /second-hand/buy           purchase vouchers (SHP) - buy from a person or a dealer
  /second-hand/stock         used stock on hand, unit by unit
  /second-hand/sell          margin-scheme invoices (MSI) - sell with a live margin preview
  /second-hand/eway          e-way bills for what was bought and sold
  /second-hand/settings      declaration wording, how returns show the non-margin part

The rules live in app/margin (buying.py, selling.py, services.py) and
app/utils/margin.py; the margin register is app/margin/reports.py.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

from flask import current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.extensions import db
from app.margin import buying, selling
from app.margin import services as ms
from app.models.customer import Customer
from app.models.einvoice import EWayBill, EWayBillStatus
from app.models.invoice import InvoiceStatus
from app.models.margin import MarginAllocation
from app.models.product import Product
from app.models.purchase import PurchaseBill, PurchaseStatus
from app.models.user import UserRole
from app.secondhand import margin_bp
from app.utils.audit import record_audit
from app.utils.features import feature_required
from app.utils.gst_config import GST_RATE_SLABS
from app.utils.indian_states import INDIAN_STATES
from app.utils.module_settings import module_settings, save_module_settings
from app.utils.tenant_scope import assert_owns, tenant_query

FLAG = "margin_scheme"
ADMIN = UserRole.CLIENT_ADMIN
ZERO = Decimal("0")


def _dec(raw, label, default=None) -> Decimal:
    raw = (raw or "").strip()
    if not raw:
        if default is not None:
            return default
        raise ValueError(f"{label} is needed.")
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{label} must be a number.")


def _date(raw):
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _month_figures(tenant) -> dict:
    """This month's second-hand sales from the margin register."""
    start = date.today().replace(day=1)
    rows = (MarginAllocation.query.filter(MarginAllocation.tenant_id == tenant.id, MarginAllocation.sold_on >= start,
                                          MarginAllocation.released_at.is_(None)).all())
    sale = sum((Decimal(r.sale_value) for r in rows), ZERO)
    margin = sum((Decimal(r.margin) for r in rows), ZERO)
    tax = sum((Decimal(r.cgst_amount + r.sgst_amount + r.igst_amount) for r in rows), ZERO)
    return {"units": sum((Decimal(r.qty) - Decimal(r.returned_qty) for r in rows), ZERO), "sale": sale,
            "margin": margin, "tax": tax, "no_gst": sale - margin}


# --- overview -----------------------------------------------------------------------------------

@margin_bp.route("/")
@roles_required(ADMIN)
@feature_required(FLAG)
def index():
    tenant = current_user.tenant
    rows = selling.stock_units(tenant)
    vouchers = buying.vouchers(tenant.id).limit(5).all()
    sales = selling.sales(tenant.id).limit(5).all()
    return render_template("secondhand/index.html", stock=selling.stock_totals(rows), month=_month_figures(tenant),
                           vouchers=vouchers, sales=sales, ready=ms.margin_on(tenant),
                           eway_on=tenant.has_feature("eway_bill"), oldest=rows[:5] if rows else [])


# --- buy -------------------------------------------------------------------------------------------

@margin_bp.route("/buy")
@roles_required(ADMIN)
@feature_required(FLAG)
def buy_list():
    vouchers = buying.vouchers(current_user.tenant_id).limit(300).all()
    return render_template("secondhand/buy_list.html", vouchers=vouchers, paid_by=buying.PAID_BY)


def _buy_lines(form) -> list[buying.BuyLine]:
    get = form.getlist
    out = []
    for i, pid in enumerate(get("line_product_id[]")):
        at = lambda key, default="": (get(key)[i] if i < len(get(key)) else default)  # noqa: E731
        new_name = at("line_new_name[]").strip()
        if not pid and not new_name:
            continue
        serials = [s.strip() for s in at("line_serials[]").replace("\n", ",").split(",") if s.strip()]
        qty = Decimal(len(serials)) if serials else _dec(at("line_qty[]"), "Quantity", Decimal("1"))
        name = new_name or "the item"
        out.append(buying.BuyLine(
            product_id=int(pid) if pid and pid.isdigit() else None, qty=qty,
            price=_dec(at("line_price[]"), f"Price paid for {name}"), new_name=new_name,
            new_hsn=at("line_new_hsn[]").strip(), new_gst_rate=_dec(at("line_new_rate[]"), "GST rate", Decimal("18")),
            seller_gst_rate=_dec(at("line_seller_gst[]"), "Seller's GST rate", ZERO), serials=serials))
    return out


@margin_bp.route("/buy/new", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required(FLAG)
def buy_new():
    tenant = current_user.tenant
    items = (tenant_query(Product).filter_by(is_margin_scheme=True, is_active=True).order_by(Product.name).all())
    if request.method == "POST":
        f = request.form
        dealer = f.get("seller_kind") == "dealer"
        try:
            seller = buying.Seller(
                name=f.get("seller_name", ""), phone=f.get("seller_phone", ""), address=f.get("seller_address", ""),
                state_code=f.get("seller_state", ""), id_type=f.get("id_type", "") if not dealer else "",
                id_no=f.get("id_no", "") if not dealer else "", gstin=f.get("seller_gstin", "") if dealer else "",
                invoice_no=f.get("invoice_no", "") if dealer else "",
                invoice_date=_date(f.get("invoice_date")) if dealer else None)
            bill = buying.buy(tenant, current_user, seller, _buy_lines(f), paid_by=f.get("paid_by", "cash"),
                              notes=f.get("notes", ""), bill_date=_date(f.get("bill_date")))
            record_audit(current_user, "margin_purchase_posted", tenant_id=tenant.id, entity_type="purchase_bill",
                         entity_id=bill.id, details={"number": bill.number, "total": str(bill.grand_total)})
            db.session.commit()
            flash(f"Bought - voucher {bill.number}. The goods are in your used stock.", "success")
            return redirect(url_for("margin.voucher", bill_id=bill.id))
        except (buying.BuyError, ValueError) as exc:
            db.session.rollback()
            flash(getattr(exc, "message", str(exc)), "error")
    settings = module_settings(tenant, "margin")
    return render_template("secondhand/buy_form.html", items=items, id_types=buying.ID_TYPES, paid_by=buying.PAID_BY,
                           states=INDIAN_STATES, home_state=selling._gstin(tenant).state_code if ms.margin_on(tenant) else "",
                           id_required=settings.get("id_proof_required"), serials_on=tenant.has_feature("serial_imei"),
                           today=date.today().isoformat(), form=request.form, rates=GST_RATE_SLABS)


def _voucher(bill_id) -> PurchaseBill:
    bill = tenant_query(PurchaseBill).filter_by(id=bill_id, kind=buying.VOUCHER).first_or_404()
    assert_owns(bill)
    return bill


@margin_bp.route("/buy/<int:bill_id>")
@roles_required(ADMIN)
@feature_required(FLAG)
def voucher(bill_id):
    bill = _voucher(bill_id)
    lots = [(line, ms.lot_remaining(line) if bill.status == PurchaseStatus.POSTED else ZERO) for line in bill.lines]
    return render_template("secondhand/voucher.html", bill=bill, lots=lots, id_types=buying.ID_TYPES,
                           paid_by=buying.PAID_BY, sold_any=ms.sold_from([l.id for l in bill.lines]))


@margin_bp.route("/buy/<int:bill_id>/voucher.pdf")
@roles_required(ADMIN)
@feature_required(FLAG)
def voucher_pdf(bill_id):
    from io import BytesIO

    from weasyprint import HTML

    from app.utils.entitlements import brand_logo_id
    from app.utils.uploads import image_data_uri

    bill = _voucher(bill_id)
    tenant = current_user.tenant
    html = render_template("secondhand/voucher_pdf.html", bill=bill, tenant=tenant, id_types=buying.ID_TYPES,
                           paid_by=buying.PAID_BY, logo_data_uri=image_data_uri(brand_logo_id(tenant)))
    pdf = HTML(string=html, base_url=current_app.root_path).write_pdf()
    return send_file(BytesIO(pdf), mimetype="application/pdf", download_name=f"{bill.number.replace('/', '-')}.pdf")


@margin_bp.route("/buy/<int:bill_id>/void", methods=["POST"])
@roles_required(ADMIN)
@feature_required(FLAG)
def voucher_void(bill_id):
    from app.purchases import services as ps

    bill = _voucher(bill_id)
    try:
        ps.void_purchase_bill(bill, current_user, request.form.get("reason", ""))
        record_audit(current_user, "margin_purchase_voided", tenant_id=bill.tenant_id, entity_type="purchase_bill",
                     entity_id=bill.id, details={"number": bill.number})
        db.session.commit()
        flash(f"Voucher {bill.number} voided - the goods are out of your used stock.", "success")
    except ps.PurchaseError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("margin.voucher", bill_id=bill.id))


# --- used stock --------------------------------------------------------------------------------------

@margin_bp.route("/stock")
@roles_required(ADMIN)
@feature_required(FLAG)
def stock():
    rows = selling.stock_units(current_user.tenant)
    q = (request.args.get("q") or "").strip().lower()
    if q:
        rows = [r for r in rows if q in r["product"].name.lower() or q in (r["serial"] or "").lower()
                or q in r["seller"].lower() or q in r["bill"].number.lower()]
    return render_template("secondhand/stock.html", rows=rows, totals=selling.stock_totals(rows), q=q)


# --- sell -----------------------------------------------------------------------------------------------

@margin_bp.route("/sell")
@roles_required(ADMIN)
@feature_required(FLAG)
def sell_list():
    invoices = selling.sales(current_user.tenant_id).limit(300).all()
    return render_template("secondhand/sell_list.html", invoices=invoices)


def _sell_lines(form) -> list[selling.SellLine]:
    """Ticked rows of the sell form: one per serial unit, or per item with a quantity."""
    out = []
    for key in form.getlist("pick"):
        price = _dec(form.get(f"price_{key}"), "Selling price")
        kind, _, rest = key.partition("-")
        if kind == "s":                                   # s-<product id>-<serial>
            pid, _, serial = rest.partition("-")
            out.append(selling.SellLine(product_id=int(pid), price=price, serials=[serial]))
        elif kind == "p" and rest.isdigit():              # p-<product id>
            out.append(selling.SellLine(product_id=int(rest), price=price,
                                        qty=_dec(form.get(f"qty_{key}"), "Quantity", Decimal("1"))))
    return out


def _sell_rows(tenant):
    """The used stock as sell-form rows: each serial unit on its own, other
    items once with the quantity on hand."""
    rows, lots = [], {}
    for r in selling.stock_units(tenant):
        if r["serial"]:
            rows.append({"key": f"s-{r['product'].id}-{r['serial']}", "product": r["product"], "serial": r["serial"],
                         "qty": Decimal("1"), "unit_cost": r["unit_cost"], "days": r["days"]})
        else:
            entry = lots.setdefault(r["product"].id, {"key": f"p-{r['product'].id}", "product": r["product"],
                                                      "serial": None, "qty": ZERO, "unit_cost": r["unit_cost"],
                                                      "days": r["days"]})
            entry["qty"] += r["qty"]
    return rows + list(lots.values())


def _buyer(form) -> selling.Buyer:
    cid = form.get("customer_id", type=int)
    if form.get("buyer_kind") == "existing" and cid:
        return selling.Buyer(customer_id=cid)
    return selling.Buyer(name=form.get("buyer_name", ""), phone=form.get("buyer_phone", ""),
                         gstin=form.get("buyer_gstin", ""), state_code=form.get("buyer_state", ""),
                         address=form.get("buyer_address", ""))


@margin_bp.route("/sell/new", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required(FLAG)
def sell_new():
    tenant = current_user.tenant
    if request.method == "POST":
        try:
            invoice = selling.sell(tenant, current_user, _buyer(request.form), _sell_lines(request.form),
                                   invoice_date=_date(request.form.get("invoice_date")),
                                   notes=request.form.get("notes", ""))
            record_audit(current_user, "invoice_created", tenant_id=tenant.id, entity_type="invoice",
                         entity_id=invoice.id, details={"number": invoice.invoice_number, "margin_scheme": True})
            db.session.commit()
            flash(f"Sold - {invoice.invoice_number}.", "success")
            return redirect(url_for("invoicing.view_invoice", invoice_id=invoice.id))
        except (selling.SellError, ValueError) as exc:
            db.session.rollback()
            flash(getattr(exc, "message", str(exc)), "error")
    customers = tenant_query(Customer).filter_by(is_active=True).order_by(Customer.name).all()
    return render_template("secondhand/sell_form.html", rows=_sell_rows(tenant), customers=customers,
                           remainder_exempt=module_settings(tenant, "margin").get("remainder_as") == "exempt",
                           states=INDIAN_STATES, home_state=selling._gstin(tenant).state_code if ms.margin_on(tenant) else "",
                           today=date.today().isoformat(), form=request.form)


@margin_bp.route("/sell/preview", methods=["POST"])
@roles_required(ADMIN)
@feature_required(FLAG)
def sell_preview():
    """The live margin and GST box on the sell form (HTMX) - nothing saved."""
    tenant = current_user.tenant
    try:
        lines = _sell_lines(request.form)
    except ValueError as exc:
        return render_template("secondhand/_preview.html", data=None, error=str(exc))
    buyer = _buyer(request.form)
    state = None
    if buyer.customer_id:
        customer = tenant_query(Customer).filter_by(id=buyer.customer_id).first()
        state = customer.state_code if customer else None
    elif buyer.gstin:
        state = buyer.gstin.strip()[:2]
    else:
        state = buyer.state_code or None
    data = selling.preview(tenant, lines, state) if lines else None
    return render_template("secondhand/_preview.html", data=data, error=None)


# --- e-way bills ------------------------------------------------------------------------------------------

@margin_bp.route("/eway")
@roles_required(ADMIN)
@feature_required(FLAG)
def eway():
    """Goods bought (coming in) and sold (going out) under the scheme, with
    their e-way bill - needed when the value is over the limit."""
    from app.einvoice import eway as ew
    from app.einvoice import rules

    tenant = current_user.tenant
    on = tenant.has_feature("eway_bill")

    def row(source, direction):
        bills = (EWayBill.query.filter_by(tenant_id=tenant.id, source_type=ew.source_type_of(source),
                                          source_id=source.id).order_by(EWayBill.id.desc()).all())
        value = ew.source_value(source)
        return {"source": source, "direction": direction, "value": value, "needed": rules.eway_bill_needed(value),
                "bill": next((b for b in bills if b.status != EWayBillStatus.CANCELLED), None),
                "new_url": url_for("einvoice.eway_new", source_type=ew.source_type_of(source), source_id=source.id)
                if on else None}

    sold = [row(i, "out") for i in selling.sales(tenant.id).filter_by(status=InvoiceStatus.ISSUED).limit(200)]
    bought = [row(b, "in") for b in buying.vouchers(tenant.id).filter_by(status=PurchaseStatus.POSTED).limit(200)]
    show_all = request.args.get("all") == "1"
    if not show_all:
        sold = [r for r in sold if r["needed"] or r["bill"]]
        bought = [r for r in bought if r["needed"] or r["bill"]]
    from app.utils.gst_config import EWAY_BILL_VALUE_THRESHOLD

    return render_template("secondhand/eway.html", sold=sold, bought=bought, eway_on=on, show_all=show_all,
                           threshold=EWAY_BILL_VALUE_THRESHOLD)


# --- settings ----------------------------------------------------------------------------------------------

@margin_bp.route("/settings", methods=["GET", "POST"])
@roles_required(ADMIN)
@feature_required(FLAG)
def settings():
    from app.utils.entitlements import locked_for_client
    from app.utils.margin import DEFAULT_DECLARATION

    tenant = current_user.tenant
    locked = locked_for_client(tenant, "margin_declaration")
    if request.method == "POST":
        remainder = request.form.get("remainder_as")
        save_module_settings(tenant, "margin", {
            "remainder_as": remainder if remainder in ("exempt", "none") else "exempt",
            "id_proof_required": bool(request.form.get("id_proof_required")),
        })
        if not locked:
            tenant.margin_scheme_declaration = (request.form.get("declaration") or "").strip() or None
        record_audit(current_user, "margin_settings_saved", tenant_id=tenant.id,
                     details={"remainder_as": remainder, "id_proof_required": bool(request.form.get("id_proof_required"))})
        db.session.commit()
        flash("Saved.", "success")
        return redirect(url_for("margin.settings"))
    return render_template("secondhand/settings.html", settings=module_settings(tenant, "margin"),
                           declaration=tenant.margin_scheme_declaration or "", default_declaration=DEFAULT_DECLARATION,
                           locked=locked)

