"""Jewellery (flag "jewellery"): today's metal rates and old gold bought
from customers.

Old gold: a numbered purchase voucher (series OG, row-locked allocator)
with the seller, weights, purity, rate and value. Its value is then spent
as the "Old gold" payment on the new bill - after GST, which is charged
on the new jewellery's full value (the firm's choice). A voucher can be
used over more than one bill until its value is gone, is never deleted,
and is voided with a reason (only while none of it is used)."""

from datetime import date
from decimal import Decimal

from app.extensions import db
from app.models.business import MetalRate, OldGoldPurchase, OldGoldStatus
from app.models.customer import Customer
from app.models.invoice_series import DocumentType
from app.models.mixins import utcnow
from app.utils.financial_year import financial_year_for
from app.utils.gst_config import METAL_PURITIES
from app.utils.jewellery import JewelleryError, money, old_gold_value, todays_rate, weight
from app.utils.numbering import allocate_number, get_or_create_series


def set_rate(tenant, user, metal: str, purity: str, rate, on: date | None = None) -> MetalRate:
    if metal not in METAL_PURITIES or purity not in METAL_PURITIES[metal]:
        raise JewelleryError("Pick a metal and purity.")
    try:
        rate = money(Decimal(str(rate)))
    except Exception:
        raise JewelleryError("The rate must be a number.")
    if rate <= 0:
        raise JewelleryError("The rate must be more than 0.")
    row = MetalRate(tenant_id=tenant.id, rate_date=on or date.today(), metal=metal, purity=purity, rate_per_gram=rate,
                    set_by_id=user.id)
    db.session.add(row)
    return row


def rate_board(tenant, on: date | None = None) -> list[dict]:
    """Every metal / purity with today's rate (set or derived)."""
    on = on or date.today()
    out = []
    for metal, grades in METAL_PURITIES.items():
        for purity in grades:
            set_today = (MetalRate.query.filter_by(tenant_id=tenant.id, metal=metal, purity=purity, rate_date=on)
                         .order_by(MetalRate.id.desc()).first())
            out.append({"metal": metal, "purity": purity, "row": set_today,
                        "rate": todays_rate(tenant.id, metal, purity, on)})
    return out


def create_old_gold(tenant, user, data: dict) -> OldGoldPurchase:
    metal, purity = data.get("metal"), data.get("purity")
    if metal not in METAL_PURITIES or purity not in METAL_PURITIES[metal]:
        raise JewelleryError("Pick the metal and purity.")
    name = (data.get("seller_name") or "").strip()
    if not name:
        raise JewelleryError("The seller's name is needed.")
    description = (data.get("description") or "").strip() or f"Old {metal}"
    try:
        gross = weight(Decimal(str(data.get("gross_weight") or "0")))
        net = weight(Decimal(str(data.get("net_weight") or "0")))
    except Exception:
        raise JewelleryError("Weights must be numbers (grams).")
    if gross <= 0 or net <= 0 or net > gross:
        raise JewelleryError("Net weight must be more than 0 and not more than the gross weight.")
    rate = data.get("rate_per_gram")
    if rate in (None, ""):
        rate = todays_rate(tenant.id, metal, purity)
        if rate is None:
            raise JewelleryError(f"Set today's {metal} {purity} rate, or enter the rate per gram.")
    try:
        rate = money(Decimal(str(rate)))
    except Exception:
        raise JewelleryError("The rate must be a number.")
    value = old_gold_value(net, rate)
    customer = None
    if data.get("customer_id"):
        customer = Customer.query.filter_by(id=int(data["customer_id"]), tenant_id=tenant.id).first()
        if customer is None:
            raise JewelleryError("Pick a valid customer.")
    gstin = tenant.gstins.filter_by(is_active=True).first()
    if gstin is None:
        raise JewelleryError("This client has no business location on file.")
    on = date.today()
    series = get_or_create_series(tenant.id, gstin.id, DocumentType.OLD_GOLD_PURCHASE, financial_year_for(on))
    voucher = OldGoldPurchase(
        tenant_id=tenant.id, gstin_id=gstin.id, invoice_series_id=series.id, number=allocate_number(series),
        purchase_date=on, customer_id=customer.id if customer else None, seller_name=name[:255],
        seller_phone=(data.get("seller_phone") or "").strip()[:20] or None,
        seller_address=(data.get("seller_address") or "").strip() or None,
        id_proof=(data.get("id_proof") or "").strip()[:60] or None, description=description[:255], metal=metal,
        purity=purity, gross_weight=gross, net_weight=net, rate_per_gram=rate, value=value, created_by_id=user.id,
    )
    db.session.add(voucher)
    db.session.flush()
    return voucher


def void_old_gold(voucher: OldGoldPurchase, user, reason: str) -> None:
    if voucher.status == OldGoldStatus.VOID:
        raise JewelleryError("This voucher is already void.")
    if not reason or not reason.strip():
        raise JewelleryError("A reason is needed to void a voucher.")
    if voucher.used_amount:
        raise JewelleryError("Part of this voucher's value has paid for a bill - void that bill first.")
    voucher.status = OldGoldStatus.VOID
    voucher.void_reason = reason.strip()
    voucher.voided_at = utcnow()
    voucher.voided_by_id = user.id


def lock_for_payment(tenant_id: int, number: str) -> OldGoldPurchase:
    voucher = (OldGoldPurchase.query.filter_by(tenant_id=tenant_id, number=(number or "").strip())
               .with_for_update().first())
    if voucher is None:
        raise JewelleryError("That old gold voucher number isn't on file.")
    if voucher.status == OldGoldStatus.VOID:
        raise JewelleryError("That old gold voucher was voided.")
    return voucher


def use(voucher: OldGoldPurchase, amount) -> None:
    voucher.used_amount = (voucher.used_amount or 0) + amount
    voucher.status = OldGoldStatus.USED if voucher.balance <= 0 else OldGoldStatus.OPEN


def release(voucher: OldGoldPurchase, amount) -> None:
    voucher.used_amount = max((voucher.used_amount or 0) - amount, Decimal("0"))
    if voucher.status != OldGoldStatus.VOID:
        voucher.status = OldGoldStatus.OPEN if voucher.balance > 0 else OldGoldStatus.USED
