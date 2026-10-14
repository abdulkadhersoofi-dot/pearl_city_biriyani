"""Where selling and buying documents touch the stock ledger. Called from
the documents' own services (app.pos.services, app.pos.returns,
app.invoicing.services, app.purchases.services) so every path that
creates or voids a document moves stock the same way."""

from datetime import date
from decimal import Decimal

from app.extensions import db
from app.inventory import ledger
from app.models.inventory import MovementType, ProductSerial, SerialStatus
from app.models.mixins import utcnow
from app.models.product import Product

ZERO = Decimal("0")


class SaleLine:
    """What a sale line needs here, whatever document it came from."""

    def __init__(self, product_id, qty, line_id=None, serials=None):
        self.product_id = product_id
        self.qty = Decimal(qty)
        self.line_id = line_id
        self.serials = list(serials or [])


def _products(lines) -> dict[int, Product]:
    ids = {l.product_id for l in lines if l.product_id}
    if not ids:
        return {}
    return {p.id: p for p in Product.query.filter(Product.id.in_(ids)).all()}


def check_sale(tenant, location_id: int, lines, require_serials: bool = True) -> None:
    """Refuse a sale that would break a stock rule - before anything is
    numbered or saved. Raises ledger.StockError. Serial numbers are picked
    at the POS counter; an invoice doesn't carry them (require_serials)."""
    products = _products(lines)
    wanted: dict[int, Decimal] = {}
    for line in lines:
        if line.product_id:
            wanted[line.product_id] = wanted.get(line.product_id, ZERO) + Decimal(line.qty)
    ledger.check_available(tenant, location_id, wanted, products)
    if tenant.has_feature("batch_expiry"):
        today = date.today()
        for pid, qty in wanted.items():
            product = products.get(pid)
            if product is None or not product.track_batch:
                continue
            batches = ledger.batch_stock(tenant.id, pid, location_id)
            fresh = sum((q for b, q in batches if not _expired(b, today)), ZERO)
            expired = sum((q for b, q in batches if _expired(b, today)), ZERO)
            if expired > 0 and fresh < qty:
                raise ledger.StockError(
                    f"{product.name}: only {ledger._fmt(fresh)} in date - expired stock can't be sold."
                    if fresh > 0 else f"{product.name}: all stock here has expired - it can't be sold."
                )
    if require_serials and tenant.has_feature("serial_imei"):
        for line in lines:
            product = products.get(line.product_id)
            if product is None or not product.track_serial:
                continue
            _check_serials(tenant.id, product, line)


def _check_serials(tenant_id: int, product: Product, line) -> None:
    serials = [s.strip() for s in line.serials if s and s.strip()]
    if Decimal(line.qty) != len(serials) or len(set(serials)) != len(serials):
        raise ledger.StockError(f"{product.name}: pick one serial/IMEI for each unit ({ledger._fmt(line.qty)}).")
    found = ProductSerial.query.filter(
        ProductSerial.tenant_id == tenant_id,
        ProductSerial.product_id == product.id,
        ProductSerial.serial_no.in_(serials),
    ).all()
    by_no = {s.serial_no: s for s in found}
    for no in serials:
        unit = by_no.get(no)
        if unit is None:
            raise ledger.StockError(f"{product.name}: serial/IMEI {no} isn't in your stock.")
        if unit.status != SerialStatus.IN_STOCK:
            raise ledger.StockError(f"{product.name}: serial/IMEI {no} was already sold.")


def _expired(batch, today) -> bool:
    return batch.expiry_date is not None and batch.expiry_date < today


def post_sale(tenant, location_id: int, lines, source_type: str, source_id: int, user_id: int | None,
              pos_bill=None) -> None:
    """Takes sold goods out of stock - by batch, earliest expiry first,
    for batch-tracked items; unit by unit for serial-tracked ones."""
    products = _products(lines)
    batches_on = tenant.has_feature("batch_expiry")
    serials_on = tenant.has_feature("serial_imei")
    today = date.today()
    for line in lines:
        product = products.get(line.product_id)
        if not ledger.sells_from_stock(product):
            continue
        cost = ledger.issue_cost(product)
        if serials_on and product.track_serial and line.serials:
            units = ProductSerial.query.filter(
                ProductSerial.tenant_id == tenant.id, ProductSerial.product_id == product.id,
                ProductSerial.serial_no.in_([s.strip() for s in line.serials]),
            ).all()
            for unit in units:
                unit.status = SerialStatus.SOLD
                unit.sold_at = utcnow()
                if pos_bill is not None:
                    unit.sold_pos_bill_id = pos_bill.id
                    unit.sold_pos_line_id = line.line_id
                ledger.post(tenant.id, product.id, location_id, -1, MovementType.SALE, source_type, source_id,
                            source_line_id=line.line_id, unit_cost=cost, serial_id=unit.id, user_id=user_id)
            continue
        remaining = line.qty
        if batches_on and product.track_batch:
            for batch, qty in ledger.batch_stock(tenant.id, product.id, location_id):
                if remaining <= 0:
                    break
                if _expired(batch, today):
                    continue
                take = min(qty, remaining)
                ledger.post(tenant.id, product.id, location_id, -take, MovementType.SALE, source_type, source_id,
                            source_line_id=line.line_id, unit_cost=cost, batch_id=batch.id, user_id=user_id)
                remaining -= take
        if remaining > 0:
            ledger.post(tenant.id, product.id, location_id, -remaining, MovementType.SALE, source_type, source_id,
                        source_line_id=line.line_id, unit_cost=cost, user_id=user_id)
    db.session.flush()


def undo_sale(tenant_id: int, source_type: str, source_id: int, user_id: int | None, note: str) -> None:
    """A voided (or fully refunded) sale: its goods go back where they
    came from, and its serial numbers back into stock."""
    ledger.reverse_source(tenant_id, source_type, source_id, user_id, note)
    if source_type == "pos_bill":
        for unit in ProductSerial.query.filter_by(tenant_id=tenant_id, sold_pos_bill_id=source_id).all():
            unit.status = SerialStatus.IN_STOCK
            unit.sold_pos_bill_id = None
            unit.sold_pos_line_id = None
            unit.sold_at = None


def post_return(tenant_id: int, location_id: int, ret, user_id: int | None) -> None:
    """Goods brought back on a POS sales return go back on the shelf of
    the counter that took them back - into the batches they were sold
    from, and with their serial numbers back in stock."""
    for rl in ret.lines:
        product = rl.product
        if not ledger.sells_from_stock(product):
            continue
        sold = [m for m in _sale_movements(tenant_id, ret.bill_id, rl.bill_line_id) if m.qty < 0]
        remaining = Decimal(rl.qty)
        for m in sold:
            if remaining <= 0:
                break
            if m.serial_id:
                unit = db.session.get(ProductSerial, m.serial_id)
                if unit is not None and unit.status == SerialStatus.SOLD:
                    unit.status = SerialStatus.IN_STOCK
                    unit.location_id = location_id
                    unit.sold_pos_bill_id = unit.sold_pos_line_id = None
                    unit.sold_at = None
            take = min(-m.qty, remaining)
            ledger.post(tenant_id, product.id, location_id, take, MovementType.SALE_RETURN, "pos_return", ret.id,
                        source_line_id=rl.id, unit_cost=m.unit_cost, batch_id=m.batch_id, serial_id=m.serial_id,
                        user_id=user_id)
            remaining -= take
        if remaining > 0:
            ledger.post(tenant_id, product.id, location_id, remaining, MovementType.SALE_RETURN, "pos_return", ret.id,
                        source_line_id=rl.id, unit_cost=ledger.issue_cost(product), user_id=user_id)
    db.session.flush()


def _sale_movements(tenant_id: int, bill_id: int, line_id: int):
    from app.models.inventory import StockMovement

    return (
        StockMovement.query.filter_by(tenant_id=tenant_id, source_type="pos_bill", source_id=bill_id,
                                      source_line_id=line_id, reversal_of_id=None)
        .order_by(StockMovement.id)
        .all()
    )


def pos_lines(bill) -> list[SaleLine]:
    return [SaleLine(l.product_id, l.qty, l.id, getattr(l, "_serials", None)) for l in bill.lines if l.product_id]


def post_invoice(tenant, invoice, user_id: int | None) -> None:
    """A customer invoice sells from the main store. An invoice billed to
    a branch is how stock reaches that branch: a transfer from the main
    store into the branch's location (the daily branch counter is fed
    separately, as before)."""
    store = ledger.default_location(tenant.id)
    lines = [SaleLine(l.product_id, l.qty, l.id) for l in invoice.lines if l.product_id]
    if invoice.branch_user_id:
        from app.models.user import User

        branch = ledger.branch_location(tenant.id, db.session.get(User, invoice.branch_user_id))
        products = _products(lines)
        for line in lines:
            product = products.get(line.product_id)
            if not ledger.sells_from_stock(product):
                continue
            cost = ledger.issue_cost(product)
            ledger.post(tenant.id, product.id, store.id, -line.qty, MovementType.TRANSFER_OUT, "invoice", invoice.id,
                        source_line_id=line.line_id, unit_cost=cost, user_id=user_id)
            ledger.post(tenant.id, product.id, branch.id, line.qty, MovementType.TRANSFER_IN, "invoice", invoice.id,
                        source_line_id=line.line_id, unit_cost=cost, user_id=user_id)
        db.session.flush()
        return
    post_sale(tenant, store.id, lines, "invoice", invoice.id, user_id)
