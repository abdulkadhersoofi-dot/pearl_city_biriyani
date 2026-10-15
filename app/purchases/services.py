"""Purchase bills, returns to suppliers (debit notes) and purchase orders.

A purchase bill is posted the moment it's saved: its goods go into
stock at landed cost (app.utils.itc.landed_costs), the items' weighted-
average cost moves (app.inventory.ledger.receive_cost), and the input
tax credit it gives is stored on the bill. It is never deleted - voiding
takes the goods back out and drops the credit.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.inventory import ledger
from app.inventory.services import get_or_create_batch
from app.models.inventory import MovementType, ProductSerial, SerialStatus, StockLocation
from app.models.invoice_series import DocumentType
from app.models.mixins import utcnow
from app.models.product import Product
from app.models.purchase import (
    ItcEligibility,
    PurchaseBill,
    PurchaseBillLine,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    PurchaseReturn,
    PurchaseReturnLine,
    PurchaseStatus,
    Supplier,
)
from app.models.tenant import Gstin, RegistrationType
from app.utils import itc
from app.utils.financial_year import financial_year_for
from app.utils.gst import compute_invoice_totals, return_line_amounts
from app.utils.gst_config import GST_RATE_SLABS
from app.utils.numbering import allocate_number, get_or_create_series

ZERO = Decimal("0")
_AMOUNT_FIELDS = ("discount_amount", "taxable_value", "cgst_amount", "sgst_amount", "igst_amount", "line_total")


class PurchaseError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class PurchaseLineInput:
    product_id: int | None
    description: str
    hsn_or_sac_code: str
    qty: Decimal
    unit: str
    rate: Decimal
    gst_rate: Decimal
    discount_percent: Decimal = Decimal("0")
    itc_eligibility: ItcEligibility = ItcEligibility.ELIGIBLE
    batch_no: str | None = None
    mfg_date: date | None = None
    expiry_date: date | None = None
    mrp: Decimal | None = None
    serials: list = field(default_factory=list)


@dataclass
class PurchaseInput:
    supplier_id: int
    gstin_id: int
    supplier_invoice_no: str
    supplier_invoice_date: date
    location_id: int
    lines: list[PurchaseLineInput]
    bill_date: date | None = None
    is_rcm: bool = False
    other_charges: Decimal = Decimal("0")
    other_charges_label: str | None = None
    notes: str | None = None
    purchase_order_id: int | None = None
    attachment_id: int | None = None


def _num(value, label, default=None):
    value = (value or "").strip() if isinstance(value, str) else value
    if value in (None, ""):
        if default is not None:
            return default
        raise PurchaseError(f"{label} is required.")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise PurchaseError(f"{label} must be a number.")


def _date(value, label):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise PurchaseError(f"{label} isn't a valid date.")


def parse_purchase_lines(form) -> list[PurchaseLineInput]:
    """Reads the purchase bill form's line arrays."""
    get = form.getlist
    descriptions = get("line_description[]")
    lines = []
    for i, description in enumerate(descriptions):
        description = (description or "").strip()
        if not description:
            continue
        at = lambda key, default="": (get(key)[i] if i < len(get(key)) else default)
        try:
            eligibility = ItcEligibility(at("line_itc[]") or "eligible")
        except ValueError:
            raise PurchaseError("Pick whether the GST on each line can be claimed.")
        pid = at("line_product_id[]")
        lines.append(PurchaseLineInput(
            product_id=int(pid) if pid and pid.isdigit() else None,
            description=description[:255],
            hsn_or_sac_code=at("line_hsn[]").strip()[:8],
            qty=_num(at("line_qty[]"), f"Quantity of {description}"),
            unit=(at("line_unit[]") or "PCS").strip()[:20],
            rate=_num(at("line_rate[]"), f"Rate of {description}"),
            gst_rate=_num(at("line_gst_rate[]"), f"GST rate of {description}", Decimal("0")),
            discount_percent=_num(at("line_discount[]"), "Discount", Decimal("0")),
            itc_eligibility=eligibility,
            batch_no=at("line_batch[]").strip() or None,
            expiry_date=_date(at("line_expiry[]"), "Expiry date"),
            mfg_date=_date(at("line_mfg[]"), "Manufacturing date"),
            mrp=_num(at("line_mrp[]"), "MRP", ZERO) or None,
            serials=[s.strip() for s in at("line_serials[]").replace("\n", ",").split(",") if s.strip()],
        ))
    if not lines:
        raise PurchaseError("Add at least one item.")
    return lines


def _location(tenant, location_id) -> StockLocation:
    location = StockLocation.query.filter_by(id=location_id, tenant_id=tenant.id, is_active=True).first()
    if location is None:
        raise PurchaseError("Pick where the goods are received.")
    return location


def _supplier(tenant, supplier_id) -> Supplier:
    supplier = Supplier.query.filter_by(id=supplier_id, tenant_id=tenant.id).first()
    if supplier is None:
        raise PurchaseError("Pick a supplier.")
    return supplier


def _gstin(tenant, gstin_id) -> Gstin:
    gstin = Gstin.query.filter_by(id=gstin_id, tenant_id=tenant.id, is_active=True).first()
    if gstin is None:
        raise PurchaseError("Pick which of your registrations received the goods.")
    return gstin


def _catalog(tenant, lines) -> dict[int, Product]:
    ids = {l.product_id for l in lines if l.product_id}
    products = {p.id: p for p in Product.query.filter(Product.id.in_(ids), Product.tenant_id == tenant.id).all()} if ids else {}
    for line in lines:
        if line.product_id and line.product_id not in products:
            raise PurchaseError(f"{line.description} isn't in your catalog.")
    return products


def _validate_lines(tenant, lines, products) -> None:
    slabs = {Decimal(str(s)) for s in GST_RATE_SLABS}
    batches_on, serials_on = tenant.has_feature("batch_expiry"), tenant.has_feature("serial_imei")
    seen_serials: set[str] = set()
    for line in lines:
        if line.qty <= 0:
            raise PurchaseError(f"Quantity of {line.description} must be more than 0.")
        if line.rate < 0:
            raise PurchaseError(f"Rate of {line.description} can't be negative.")
        if line.discount_percent < 0 or line.discount_percent > 100:
            raise PurchaseError("A discount must be between 0% and 100%.")
        if line.gst_rate not in slabs:
            raise PurchaseError(f"{line.gst_rate}% isn't a GST rate.")
        product = products.get(line.product_id)
        if product is None:
            continue
        if product.has_variants:
            raise PurchaseError(f"{product.name} has variants - buy the size/colour, not the design.")
        if batches_on and product.track_batch and not line.batch_no:
            raise PurchaseError(f"{product.name} tracks batches - enter the batch number.")
        if serials_on and product.track_serial:
            if Decimal(len(line.serials)) != line.qty:
                raise PurchaseError(f"{product.name}: enter one serial/IMEI per unit ({len(line.serials)} for {line.qty}).")
            for serial in line.serials:
                if serial in seen_serials:
                    raise PurchaseError(f"Serial/IMEI {serial} is entered twice.")
                seen_serials.add(serial)
            taken = ProductSerial.query.filter(ProductSerial.tenant_id == tenant.id, ProductSerial.serial_no.in_(line.serials)).first()
            if taken is not None:
                raise PurchaseError(f"Serial/IMEI {taken.serial_no} is already in your records.")


# --- purchase bills -----------------------------------------------------------------

def can_claim_itc(tenant, gstin: Gstin | None = None) -> bool:
    """Only a regular GST registration claims input tax credit (s.16).
    An unregistered business, or one under the composition scheme
    (s.10(4)), pays the GST on its purchases as part of their cost."""
    if tenant.registration_type != RegistrationType.REGULAR:
        return False
    if gstin is not None:
        return bool(gstin.gstin)
    return tenant.gstins.filter(Gstin.is_active.is_(True), Gstin.gstin.isnot(None)).first() is not None


def create_purchase_bill(tenant, user, data: PurchaseInput) -> PurchaseBill:
    supplier = _supplier(tenant, data.supplier_id)
    gstin = _gstin(tenant, data.gstin_id)
    location = _location(tenant, data.location_id)
    invoice_no = (data.supplier_invoice_no or "").strip()[:50]
    if not invoice_no:
        raise PurchaseError("Enter the supplier's invoice number.")
    if not data.supplier_invoice_date:
        raise PurchaseError("Enter the supplier's invoice date.")
    duplicate = PurchaseBill.query.filter(
        PurchaseBill.tenant_id == tenant.id, PurchaseBill.supplier_id == supplier.id,
        db.func.lower(PurchaseBill.supplier_invoice_no) == invoice_no.lower(), PurchaseBill.status == PurchaseStatus.POSTED,
    ).first()
    if duplicate is not None:
        raise PurchaseError(f"Invoice {invoice_no} from {supplier.name} is already entered as {duplicate.number}.")
    other_charges = Decimal(data.other_charges or 0)
    if other_charges < 0:
        raise PurchaseError("Other charges can't be negative.")
    products = _catalog(tenant, data.lines)
    _validate_lines(tenant, data.lines, products)
    if not can_claim_itc(tenant, gstin):
        for line in data.lines:
            line.itc_eligibility = ItcEligibility.INELIGIBLE
    # Goods bought for resale under the margin scheme never give ITC
    # (Rule 32(5)) - their full price is what the margin is worked from.
    from app.margin.services import margin_on

    margin_ids = {pid for pid, p in products.items() if p.is_margin_scheme} if margin_on(tenant) else set()
    for line in data.lines:
        if line.product_id in margin_ids:
            line.itc_eligibility = ItcEligibility.INELIGIBLE

    is_intra = (supplier.state_code or gstin.state_code) == gstin.state_code
    computed = [itc.compute_purchase_line(l.qty, l.rate, l.discount_percent, l.gst_rate, is_intra) for l in data.lines]
    eligibilities = [l.itc_eligibility for l in data.lines]
    totals = itc.purchase_totals(computed, eligibilities, other_charges, data.is_rcm)
    costs = itc.landed_costs(computed, [l.qty for l in data.lines], eligibilities, other_charges)

    bill_date = data.bill_date or date.today()
    series = get_or_create_series(tenant.id, gstin.id, DocumentType.PURCHASE_BILL, financial_year_for(bill_date))
    bill = PurchaseBill(
        tenant_id=tenant.id, gstin_id=gstin.id, invoice_series_id=series.id, number=allocate_number(series),
        supplier_id=supplier.id, supplier_snapshot=supplier.snapshot(), supplier_invoice_no=invoice_no,
        supplier_invoice_date=data.supplier_invoice_date, bill_date=bill_date,
        place_of_supply_state_code=gstin.state_code, is_rcm=bool(data.is_rcm), location_id=location.id,
        purchase_order_id=data.purchase_order_id, other_charges=other_charges,
        other_charges_label=(data.other_charges_label or "").strip()[:60] or None,
        notes=(data.notes or "").strip() or None, attachment_id=data.attachment_id, created_by_id=user.id,
        **totals,
    )
    db.session.add(bill)
    db.session.flush()

    batches_on, serials_on = tenant.has_feature("batch_expiry"), tenant.has_feature("serial_imei")
    for order, (line, amounts, (share, unit_cost)) in enumerate(zip(data.lines, computed, costs)):
        product = products.get(line.product_id)
        batch = None
        if product is not None and batches_on and product.track_batch and line.batch_no:
            batch = get_or_create_batch(tenant.id, product, line.batch_no, line.expiry_date, line.mfg_date, line.mrp)
        bill_line = PurchaseBillLine(
            bill_id=bill.id, product_id=line.product_id, description=line.description,
            hsn_or_sac_code=line.hsn_or_sac_code, qty=line.qty, unit=line.unit, rate=line.rate,
            discount_percent=line.discount_percent, gst_rate=line.gst_rate, itc_eligibility=line.itc_eligibility,
            charges_share=share, unit_cost=unit_cost, batch_id=batch.id if batch else None, sort_order=order,
            is_margin_scheme=line.product_id in margin_ids,
            **{f: amounts[f] for f in _AMOUNT_FIELDS},
        )
        db.session.add(bill_line)
        db.session.flush()
        if not ledger.sells_from_stock(product):
            continue
        ledger.receive_cost(product, line.qty, unit_cost)
        product.purchase_rate = line.rate
        if serials_on and product.track_serial:
            warranty_end = bill_date + timedelta(days=30 * product.warranty_months) if product.warranty_months else None
            for serial_no in line.serials:
                unit = ProductSerial(tenant_id=tenant.id, product_id=product.id, serial_no=serial_no[:64],
                                     location_id=location.id, purchase_bill_id=bill.id, warranty_end=warranty_end)
                db.session.add(unit)
                db.session.flush()
                ledger.post(tenant.id, product.id, location.id, 1, MovementType.PURCHASE, "purchase_bill", bill.id,
                            source_line_id=bill_line.id, unit_cost=unit_cost, serial_id=unit.id, user_id=user.id)
        else:
            ledger.post(tenant.id, product.id, location.id, line.qty, MovementType.PURCHASE, "purchase_bill", bill.id,
                        source_line_id=bill_line.id, unit_cost=unit_cost, batch_id=batch.id if batch else None,
                        user_id=user.id)

    if data.purchase_order_id:
        order = PurchaseOrder.query.filter_by(id=data.purchase_order_id, tenant_id=tenant.id).first()
        if order is not None and order.status == PurchaseOrderStatus.OPEN:
            order.status = PurchaseOrderStatus.CONVERTED
    db.session.flush()
    return bill


def void_purchase_bill(bill: PurchaseBill, user, reason: str) -> None:
    if bill.status != PurchaseStatus.POSTED:
        raise PurchaseError("This bill is already voided.")
    if not (reason or "").strip():
        raise PurchaseError("A reason is required to void a purchase bill.")
    if PurchaseReturn.query.filter_by(purchase_bill_id=bill.id, status=PurchaseStatus.POSTED).first() is not None:
        raise PurchaseError("Goods on this bill were returned to the supplier - void those returns first.")
    from app.margin.services import sold_from

    if sold_from([l.id for l in bill.lines if l.is_margin_scheme]):
        raise PurchaseError("Goods on this bill were sold under the margin scheme - their sales use its price. "
                            "Void those sales first.")
    sold = ProductSerial.query.filter(ProductSerial.purchase_bill_id == bill.id, ProductSerial.status != SerialStatus.IN_STOCK).first()
    if sold is not None:
        raise PurchaseError(f"Serial/IMEI {sold.serial_no} from this bill was already sold.")
    for line in bill.lines:
        if line.product is not None and ledger.sells_from_stock(line.product):
            ledger.unreceive_cost(line.product, line.qty, line.unit_cost)
    ledger.reverse_source(bill.tenant_id, "purchase_bill", bill.id, user.id, f"{bill.number} voided")
    from app.accounts.services import release_payment_allocations

    release_payment_allocations(bill.tenant_id, purchase_bill_id=bill.id)
    for unit in ProductSerial.query.filter_by(purchase_bill_id=bill.id).all():
        unit.status = SerialStatus.RETURNED_TO_SUPPLIER
    bill.status = PurchaseStatus.VOIDED
    bill.void_reason = reason.strip()
    bill.voided_at = utcnow()
    bill.voided_by_id = user.id


# --- returns to the supplier (debit notes) ------------------------------------------------

def returnable(bill: PurchaseBill) -> list[tuple[PurchaseBillLine, Decimal, dict]]:
    """[(line, quantity already sent back, amounts already sent back)]."""
    done: dict[int, dict] = {}
    # Queried, not bill.returns: a return made earlier in this same
    # session may not be in an already-loaded collection yet.
    for ret in PurchaseReturn.query.filter_by(purchase_bill_id=bill.id).all():
        if ret.status != PurchaseStatus.POSTED:
            continue
        for rl in ret.lines:
            entry = done.setdefault(rl.bill_line_id, {"qty": ZERO, **{f: ZERO for f in _AMOUNT_FIELDS}})
            entry["qty"] += rl.qty
            for f in _AMOUNT_FIELDS:
                entry[f] += getattr(rl, f)
    return [(line, done.get(line.id, {}).get("qty", ZERO), {k: v for k, v in done.get(line.id, {}).items() if k != "qty"})
            for line in bill.lines]


def create_purchase_return(bill: PurchaseBill, user, quantities: dict[int, Decimal], reason: str,
                           return_date: date | None = None) -> PurchaseReturn:
    if bill.status != PurchaseStatus.POSTED:
        raise PurchaseError("Only a posted purchase bill can have goods returned.")
    reason = (reason or "").strip()
    if not reason:
        raise PurchaseError("A reason is required for a return to the supplier.")
    picked = {k: v for k, v in quantities.items() if v and v > 0}
    if not picked:
        raise PurchaseError("Enter how many of at least one item go back.")
    tenant = bill.gstin.tenant
    computed = []
    for line, done_qty, done_amounts in returnable(bill):
        qty = picked.pop(line.id, None)
        if qty is None:
            continue
        if qty > line.qty - done_qty:
            raise PurchaseError(f"Only {(line.qty - done_qty).normalize():f} of {line.description} can still go back.")
        if line.is_margin_scheme:
            from app.margin.services import lot_remaining

            unsold = lot_remaining(line)
            if qty > unsold:
                raise PurchaseError(f"Only {unsold.normalize():f} of {line.description} from this bill are unsold "
                                    "- the rest were sold under the margin scheme.")
        amounts = return_line_amounts(
            original={"rate": line.rate, "discount_percent": line.discount_percent, "discount_flat": 0,
                      "gst_rate": line.gst_rate, "price_includes_tax": False, "is_intra_state": not line.igst_amount,
                      **{f: getattr(line, f) for f in _AMOUNT_FIELDS}},
            original_qty=line.qty, already_returned=done_amounts, already_returned_qty=done_qty, return_qty=qty,
        )
        computed.append((line, qty, amounts))
    if picked:
        raise PurchaseError("An item picked isn't on this bill.")

    location = bill.location
    wanted: dict[int, Decimal] = {}
    for line, qty, _ in computed:
        if line.product_id:
            wanted[line.product_id] = wanted.get(line.product_id, ZERO) + qty
    products = {line.product_id: line.product for line, _, _ in computed if line.product_id}
    try:
        ledger.check_available(tenant, location.id, wanted, products)
    except ledger.StockError as exc:
        raise PurchaseError(exc.message)

    totals = compute_invoice_totals([a for _, _, a in computed])
    itc_back = [itc.return_itc(a, line.itc_eligibility) for line, _, a in computed]
    return_date = return_date or date.today()
    series = get_or_create_series(bill.tenant_id, bill.gstin_id, DocumentType.PURCHASE_RETURN, financial_year_for(return_date))
    ret = PurchaseReturn(
        tenant_id=bill.tenant_id, gstin_id=bill.gstin_id, invoice_series_id=series.id, number=allocate_number(series),
        bill=bill, supplier_id=bill.supplier_id, supplier_snapshot=bill.supplier_snapshot,
        return_date=return_date, reason=reason, location_id=location.id, created_by_id=user.id,
        itc_cgst=sum((c for c, _, _ in itc_back), ZERO), itc_sgst=sum((s for _, s, _ in itc_back), ZERO),
        itc_igst=sum((i for _, _, i in itc_back), ZERO), **totals,
    )
    db.session.add(ret)
    db.session.flush()
    for line, qty, amounts in computed:
        r_line = PurchaseReturnLine(
            return_id=ret.id, bill_line_id=line.id, product_id=line.product_id, description=line.description,
            hsn_or_sac_code=line.hsn_or_sac_code, qty=qty, unit=line.unit, rate=line.rate, gst_rate=line.gst_rate,
            unit_cost=line.unit_cost, itc_eligibility=line.itc_eligibility, **amounts,
        )
        db.session.add(r_line)
        db.session.flush()
        product = line.product
        if not ledger.sells_from_stock(product):
            continue
        if product.track_serial and tenant.has_feature("serial_imei"):
            units = (ProductSerial.query.filter_by(tenant_id=bill.tenant_id, product_id=product.id, purchase_bill_id=bill.id,
                                                   status=SerialStatus.IN_STOCK).order_by(ProductSerial.id).limit(int(qty)).all())
            if len(units) < qty:
                raise PurchaseError(f"{product.name}: only {len(units)} unsold unit(s) from this bill are in stock.")
            for unit in units:
                unit.status = SerialStatus.RETURNED_TO_SUPPLIER
                ledger.post(bill.tenant_id, product.id, location.id, -1, MovementType.PURCHASE_RETURN, "purchase_return",
                            ret.id, source_line_id=r_line.id, unit_cost=line.unit_cost, serial_id=unit.id, user_id=user.id)
        else:
            ledger.post(bill.tenant_id, product.id, location.id, -qty, MovementType.PURCHASE_RETURN, "purchase_return",
                        ret.id, source_line_id=r_line.id, unit_cost=line.unit_cost, batch_id=line.batch_id, user_id=user.id)
        ledger.unreceive_cost(product, qty, line.unit_cost)
    db.session.flush()
    return ret


def void_purchase_return(ret: PurchaseReturn, user, reason: str) -> None:
    if ret.status != PurchaseStatus.POSTED:
        raise PurchaseError("This return is already voided.")
    if not (reason or "").strip():
        raise PurchaseError("A reason is required to void it.")
    for line in ret.lines:
        if line.product is not None and ledger.sells_from_stock(line.product):
            ledger.receive_cost(line.product, line.qty, line.unit_cost)
    ledger.reverse_source(ret.tenant_id, "purchase_return", ret.id, user.id, f"{ret.number} voided")
    from app.models.inventory import StockMovement

    for m in StockMovement.query.filter_by(tenant_id=ret.tenant_id, source_type="purchase_return", source_id=ret.id).all():
        if m.serial_id and m.reversal_of_id is None:
            unit = db.session.get(ProductSerial, m.serial_id)
            if unit is not None:
                unit.status = SerialStatus.IN_STOCK
    ret.status = PurchaseStatus.VOIDED
    ret.void_reason = reason.strip()
    ret.voided_at = utcnow()
    ret.voided_by_id = user.id


# --- purchase orders ------------------------------------------------------------------

def create_purchase_order(tenant, user, supplier_id, gstin_id, lines: list[PurchaseLineInput],
                          order_date: date | None = None, expected_date: date | None = None, notes: str | None = None) -> PurchaseOrder:
    supplier = _supplier(tenant, supplier_id)
    gstin = _gstin(tenant, gstin_id)
    _catalog(tenant, lines)
    for line in lines:
        if line.qty <= 0:
            raise PurchaseError(f"Quantity of {line.description} must be more than 0.")
    order_date = order_date or date.today()
    series = get_or_create_series(tenant.id, gstin.id, DocumentType.PURCHASE_ORDER, financial_year_for(order_date))
    estimated = sum((l.qty * l.rate * (1 + l.gst_rate / 100) for l in lines), ZERO).quantize(Decimal("0.01"))
    order = PurchaseOrder(
        tenant_id=tenant.id, gstin_id=gstin.id, invoice_series_id=series.id, number=allocate_number(series),
        supplier_id=supplier.id, order_date=order_date, expected_date=expected_date,
        notes=(notes or "").strip() or None, estimated_total=estimated, created_by_id=user.id,
    )
    db.session.add(order)
    db.session.flush()
    for line in lines:
        db.session.add(PurchaseOrderLine(order_id=order.id, product_id=line.product_id, description=line.description,
                                         hsn_or_sac_code=line.hsn_or_sac_code, qty=line.qty, unit=line.unit,
                                         rate=line.rate, gst_rate=line.gst_rate))
    db.session.flush()
    return order


def cancel_purchase_order(order: PurchaseOrder, reason: str) -> None:
    if order.status != PurchaseOrderStatus.OPEN:
        raise PurchaseError("Only an open order can be cancelled.")
    if not (reason or "").strip():
        raise PurchaseError("A reason is required to cancel an order.")
    order.status = PurchaseOrderStatus.CANCELLED
    order.cancel_reason = reason.strip()
