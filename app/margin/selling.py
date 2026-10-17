"""Selling second-hand goods under the margin scheme (module
"margin_scheme"): the used stock on hand, a preview of the margin and GST
before anything is saved, and the sale - an "Invoice - Margin Scheme"
(series MSI) made through the one invoicing service, with each unit's
serial/IMEI so its own purchase price sets its margin.

The GST law (Rule 32(5) of the CGST Rules, 2017): the value of the supply
is the margin - selling price minus purchase price - and GST is paid on it
at the goods' own rate (18% for most used goods such as phones; the rate
on the item). A negative margin is ignored (no tax, never a refund). The
buyer pays one price; GST isn't added on top. The rest of the price (the
purchase cost inside it) carries no GST - see app/reports/gstr.py for how
the returns show it.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.margin import services as ms
from app.models.customer import Customer
from app.models.inventory import ProductSerial, SerialStatus
from app.models.invoice import Invoice
from app.models.invoice_series import DocumentType
from app.models.product import Product
from app.models.purchase import PurchaseBill, PurchaseBillLine, PurchaseStatus
from app.models.tenant import Gstin
from app.utils.indian_states import STATE_NAME_BY_CODE

ZERO = Decimal("0")


class SellError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class SellLine:
    product_id: int
    price: Decimal                   # per unit - what the buyer pays
    qty: Decimal = Decimal("1")
    serials: list = field(default_factory=list)


@dataclass
class Buyer:
    customer_id: int | None = None
    name: str = ""
    phone: str = ""
    gstin: str = ""
    state_code: str = ""
    address: str = ""


# --- used stock --------------------------------------------------------------------------

def stock_units(tenant) -> list[dict]:
    """Every second-hand unit (or lot) still unsold, oldest first: what it
    cost, when and from whom it was bought, how long it has been held."""
    today = date.today()
    serials_on = tenant.has_feature("serial_imei")
    rows = []
    products = (Product.query.filter_by(tenant_id=tenant.id, is_margin_scheme=True)
                .order_by(Product.name).all())
    for product in products:
        if serials_on and product.track_serial:
            units = (ProductSerial.query.filter_by(tenant_id=tenant.id, product_id=product.id,
                                                   status=SerialStatus.IN_STOCK)
                     .filter(ProductSerial.purchase_bill_id.isnot(None)).order_by(ProductSerial.id).all())
            for unit in units:
                line = PurchaseBillLine.query.filter_by(bill_id=unit.purchase_bill_id, product_id=product.id,
                                                        is_margin_scheme=True).first()
                if line is None or line.bill.status != PurchaseStatus.POSTED:
                    continue
                rows.append(_row(product, line, Decimal("1"), unit.serial_no, today))
        else:
            for line, left in ms.available_lots(tenant.id, product.id):
                rows.append(_row(product, line, left, None, today))
    rows.sort(key=lambda r: (r["bought_on"], r["product"].name))
    return rows


def _row(product, line: PurchaseBillLine, qty: Decimal, serial: str | None, today: date) -> dict:
    bill: PurchaseBill = line.bill
    cost = line.margin_unit_price
    return {"product": product, "serial": serial, "qty": qty, "unit_cost": cost, "value": cost * qty,
            "bought_on": bill.bill_date, "bill": bill, "seller": (bill.supplier_snapshot or {}).get("name", ""),
            "days": (today - bill.bill_date).days}


def stock_totals(rows) -> dict:
    return {"units": sum((r["qty"] for r in rows), ZERO), "value": sum((r["value"] for r in rows), ZERO),
            "item_count": len({r["product"].id for r in rows})}


# --- preview -----------------------------------------------------------------------------------

def _gstin(tenant) -> Gstin:
    gstin = (tenant.gstins.filter(Gstin.is_active.is_(True), Gstin.gstin.isnot(None))
             .order_by(Gstin.is_primary.desc(), Gstin.id).first())
    if gstin is None:
        raise SellError("The margin scheme needs a GST registration - add the business's GSTIN first.")
    return gstin


def preview(tenant, lines: list[SellLine], place_of_supply: str | None = None) -> dict:
    """What the sale would be, line by line, without saving anything:
    sale price, purchase price, margin, taxable value and GST on the
    margin, and the part of the price that carries no GST."""
    gstin = _gstin(tenant)
    pos = place_of_supply or gstin.state_code
    intra = pos == gstin.state_code
    out, errors = [], []
    keys = ("sale_value", "purchase_value", "margin", "taxable_value", "cgst", "sgst", "igst", "tax", "no_gst")
    totals = {k: ZERO for k in keys}
    for line in lines:
        product = Product.query.filter_by(id=line.product_id, tenant_id=tenant.id, is_margin_scheme=True).first()
        if product is None:
            errors.append("An item picked isn't a second-hand item.")
            continue
        qty = Decimal(len(line.serials)) if line.serials else Decimal(line.qty)
        try:
            plan = ms.plan_line(tenant, product, qty, line.serials)
        except ms.MarginError as exc:
            errors.append(exc.message)
            continue
        priced = ms.price_line(qty, Decimal(line.price), ZERO, 0, product.gst_rate, intra, plan)
        tax = priced["cgst_amount"] + priced["sgst_amount"] + priced["igst_amount"]
        row = {"product": product, "serials": line.serials, "qty": qty, "price": Decimal(line.price),
               "rate": product.gst_rate, "sale_value": priced["line_total"],
               "purchase_value": priced["margin_purchase_value"], "margin": priced["margin_value"],
               "loss": priced["line_total"] < priced["margin_purchase_value"],
               "taxable_value": priced["taxable_value"], "cgst": priced["cgst_amount"], "sgst": priced["sgst_amount"],
               "igst": priced["igst_amount"], "tax": tax, "no_gst": priced["line_total"] - priced["margin_value"]}
        out.append(row)
        for k in keys:
            totals[k] += row[k]
    return {"lines": out, "totals": totals, "errors": errors, "intra": intra, "place_of_supply": pos}


# --- the sale -------------------------------------------------------------------------------------

def _customer(tenant, buyer: Buyer, home_state: str) -> Customer:
    if buyer.customer_id:
        customer = Customer.query.filter_by(id=buyer.customer_id, tenant_id=tenant.id).first()
        if customer is None:
            raise SellError("Pick a customer from your list.")
        return customer
    name = (buyer.name or "").strip()
    if not name:
        raise SellError("Enter the buyer's name, or pick a customer.")
    from app.utils.gst import GSTIN_REGEX, normalize_gstin
    import re

    gstin = normalize_gstin(buyer.gstin or "")
    if gstin and not re.match(GSTIN_REGEX, gstin):
        raise SellError(f"{gstin} isn't a valid GSTIN.")
    state = (gstin[:2] if gstin else buyer.state_code) or home_state
    customer = Customer(tenant_id=tenant.id, name=name[:255], gstin=gstin, phone=(buyer.phone or "").strip()[:20] or None,
                        address_line1=(buyer.address or "").strip()[:255] or None, state_code=state,
                        state_name=STATE_NAME_BY_CODE.get(state, ""))
    db.session.add(customer)
    db.session.flush()
    return customer


def sell(tenant, user, buyer: Buyer, lines: list[SellLine], invoice_date: date | None = None,
         notes: str = "") -> Invoice:
    """The margin-scheme invoice. Raises SellError."""
    from app.invoicing import services as inv

    if not ms.margin_on(tenant):
        raise SellError("The margin scheme is for a regular GST registration with the module on.")
    if not lines:
        raise SellError("Pick at least one item to sell.")
    gstin = _gstin(tenant)
    customer = _customer(tenant, buyer, gstin.state_code)
    invoice_lines = []
    for line in lines:
        product = Product.query.filter_by(id=line.product_id, tenant_id=tenant.id, is_margin_scheme=True).first()
        if product is None:
            raise SellError("An item picked isn't a second-hand item.")
        if Decimal(line.price) <= 0:
            raise SellError(f"{product.name}: enter the selling price.")
        serials = [s.strip() for s in (line.serials or []) if s and s.strip()]
        qty = Decimal(len(serials)) if serials else Decimal(line.qty)
        description = product.name + (f" - IMEI/Serial {', '.join(serials)}" if serials else "")
        invoice_lines.append(inv.LineInput(
            description=description[:255], hsn_or_sac_code=product.hsn_or_sac_code, qty=qty,
            rate=Decimal(line.price), discount_percent=ZERO, gst_rate=Decimal(product.gst_rate),
            product_id=product.id, unit=product.unit or "PCS", price_includes_tax=True, serials=serials))
    try:
        return inv.create_invoice(tenant, user, inv.InvoiceInput(
            gstin_id=gstin.id, document_type=DocumentType.TAX_INVOICE,
            place_of_supply_state_code=customer.state_code or gstin.state_code,
            invoice_date=invoice_date or date.today(), notes=(notes or "").strip() or None,
            customer_id=customer.id, lines=invoice_lines))
    except inv.InvoiceValidationError as exc:
        raise SellError(exc.message)


def sales(tenant_id: int):
    """Margin-scheme invoices, newest first (counter bills are in the POS day-book)."""
    return (Invoice.query.filter_by(tenant_id=tenant_id, document_type=DocumentType.MARGIN_SCHEME_INVOICE)
            .order_by(Invoice.invoice_date.desc(), Invoice.id.desc()))
