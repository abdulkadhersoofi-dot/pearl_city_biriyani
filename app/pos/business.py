"""Where the business-type modules (Phase 9) touch a counter sale - called
from app.pos.services so the counter's own flow stays in one place. Each
rule only acts with its module's flag on; with every flag off a sale is
exactly what it was before.

  restaurant_mode  sold-out items refused; the order type's price list
  pharmacy /       sell by the pack (alt unit): price x factor, stock
   wholesale       moves in the item's own unit (qty x factor)
  wholesale        customer group price list, quantity slabs
  jewellery        price from today's metal rate (app.utils.jewellery)
  pharmacy         Schedule H / H1 / X: prescriber and patient required
  electronics      customer warranty from the sale date
  jewellery /      old gold vouchers and loyalty points as payments
   loyalty"""

import calendar
from datetime import date
from decimal import Decimal

from app.models.pos_bill import PaymentMode
from app.utils.gst_config import DRUG_SCHEDULES, DRUG_SCHEDULES_REGISTER


class RuleError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def sells_by_pack(tenant) -> bool:
    return tenant.has_feature("pharmacy") or tenant.has_feature("wholesale")


def line_rules(tenant, product, line, customer, price_list, list_prices, may_edit_price: bool) -> None:
    """Adjust one catalog line: unit, price, jewellery breakdown. Raises RuleError."""
    from app.pos.services import counter_price

    if tenant.has_feature("restaurant_mode"):
        from app.restaurant.services import is_sold_out

        if is_sold_out(product):
            raise RuleError(f"{product.name} is sold out today.")

    factor = Decimal("1")
    if line.alt and product.alt_unit and product.alt_unit_factor and sells_by_pack(tenant):
        factor = Decimal(product.alt_unit_factor)
        line.unit = product.alt_unit
    else:
        line.alt = False
    line.unit_factor = factor

    if tenant.has_feature("jewellery") and product.is_jewellery:
        from app.utils.jewellery import JewelleryError, price_product

        override = line.net_weight if (may_edit_price and line.net_weight) else None
        try:
            priced = price_product(product, net_weight=override)
        except JewelleryError as exc:
            raise RuleError(exc.message)
        line.rate = priced.total
        line.price_includes_tax = False
        line.jewellery = priced.as_dict()
        line.unit_factor = Decimal("1")
        return

    if may_edit_price:
        return
    base = counter_price(product, price_list, list_prices)
    if tenant.has_feature("wholesale"):
        from app.wholesale.pricing import unit_price

        explicit = price_list is not None and list_prices is not None and product.id in list_prices
        base = unit_price(tenant.id, product, Decimal(line.qty) * factor, customer, base=base if explicit else None)
    line.rate = base * factor


def stock_qty(line) -> Decimal:
    """What a line takes out of stock, in the item's own unit."""
    return Decimal(line.qty) * Decimal(getattr(line, "unit_factor", None) or 1)


def prescription(tenant, cart) -> dict | None:
    """A sale with Schedule H / H1 / X drugs needs the prescriber and the
    patient (and, for the H1 / X register, the doctor's registration number
    and the patient's address). Returns what's stored on the bill."""
    if not tenant.has_feature("pharmacy"):
        return None
    from app.models.product import Product

    ids = [l.product_id for l in cart.lines if l.product_id]
    if not ids:
        return None
    scheduled = Product.query.filter(Product.tenant_id == tenant.id, Product.id.in_(ids),
                                     Product.drug_schedule.isnot(None)).all()
    schedules = sorted({p.drug_schedule for p in scheduled if p.drug_schedule in DRUG_SCHEDULES})
    if not schedules:
        return None
    given = {k: (str(v or "").strip()) for k, v in (cart.prescription or {}).items()}
    names = ", ".join(p.name for p in scheduled)
    if not given.get("doctor") or not given.get("patient"):
        raise RuleError(f"{names}: {'/'.join(DRUG_SCHEDULES[s] for s in schedules)} - enter the doctor's and "
                        "the patient's name from the prescription.")
    if set(schedules) & DRUG_SCHEDULES_REGISTER and (not given.get("doctor_reg_no") or not given.get("patient_address")):
        raise RuleError(f"{names}: for the H1 / X register enter the doctor's registration number and the "
                        "patient's address too.")
    return {"doctor": given.get("doctor", "")[:100], "doctor_reg_no": given.get("doctor_reg_no", "")[:40],
            "patient": given.get("patient", "")[:100], "patient_address": given.get("patient_address", "")[:255],
            "schedules": schedules}


# --- payments ------------------------------------------------------------------------------

def extra_modes(tenant) -> list[PaymentMode]:
    """Payment modes a module adds on top of the counter's own list."""
    modes = []
    if tenant.has_feature("jewellery"):
        modes.append(PaymentMode.OLD_GOLD)
    if tenant.has_feature("loyalty"):
        modes.append(PaymentMode.LOYALTY)
    return modes


def check_payment(tenant, mode: PaymentMode, amount: Decimal, reference: str | None, customer):
    """Validate an old gold / loyalty payment; returns the locked voucher
    (old gold) or None. Raises RuleError."""
    if mode == PaymentMode.OLD_GOLD:
        from app.jewellery.services import lock_for_payment
        from app.utils.jewellery import JewelleryError

        try:
            voucher = lock_for_payment(tenant.id, reference)
        except JewelleryError as exc:
            raise RuleError(exc.message)
        if amount > voucher.balance:
            raise RuleError(f"Only Rs. {voucher.balance:.2f} is left on old gold voucher {voucher.number}.")
        return voucher
    if mode == PaymentMode.LOYALTY:
        from app.loyalty.points import LoyaltyError, check_spend

        try:
            check_spend(tenant, customer, amount)
        except LoyaltyError as exc:
            raise RuleError(exc.message)
    return None


def after_checkout(tenant, user, bill, cart, used_vouchers) -> dict:
    """Book what the modules need once the bill exists. Returns extras for
    the counter (e.g. the KOT printed for a takeaway order)."""
    from app.jewellery.services import use

    for voucher, amount in used_vouchers:
        use(voucher, amount)
    if tenant.has_feature("loyalty"):
        from app.loyalty.points import record_bill

        record_bill(tenant, bill, user.id)
    if tenant.has_feature("electronics"):
        _warranty_from_sale(tenant, bill)
    extras = {}
    if tenant.has_feature("restaurant_mode"):
        from app.restaurant.services import after_checkout as restaurant_after

        kot = restaurant_after(tenant, user, bill, cart)
        if kot is not None:
            extras["kot_id"] = kot.id
    return extras


def add_months(start: date, months: int) -> date:
    month = start.month - 1 + months
    year = start.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def _warranty_from_sale(tenant, bill) -> None:
    """The customer's warranty runs from the day they bought it."""
    from app.models.inventory import ProductSerial

    today = date.today()
    for unit in ProductSerial.query.filter_by(tenant_id=tenant.id, sold_pos_bill_id=bill.id).all():
        months = unit.product.warranty_months if unit.product else None
        if months:
            unit.warranty_end = add_months(today, months)


def on_void(bill, user, note: str) -> None:
    """A voided bill: old gold value back on its voucher, loyalty undone."""
    from app.models.business import OldGoldPurchase

    for payment in bill.payments:
        if payment.mode == PaymentMode.OLD_GOLD and payment.reference:
            voucher = (OldGoldPurchase.query.filter_by(tenant_id=bill.tenant_id, number=payment.reference)
                       .with_for_update().first())
            if voucher is not None:
                from app.jewellery.services import release

                release(voucher, payment.amount)
    if bill.customer_id:
        from app.loyalty.points import reverse_bill

        reverse_bill(bill.tenant_id, bill, user.id, note)
