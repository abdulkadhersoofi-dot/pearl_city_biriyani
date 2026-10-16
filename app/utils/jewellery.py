"""Jewellery pricing - the one place it is worked out (module "jewellery").

A piece's price before GST:
  metal value  = net weight (g) x today's rate per gram for its purity
  making       = per gram x net weight, or a % of the metal value, or a flat amount
  stones       = the stone value on the item
GST is then charged on the whole at the item's rate - 3% for jewellery
(gst_config.JEWELLERY_GST_RATE) - by the ordinary GST service. Making
charges on the customer's own gold (job work) are a separate service line
at 5% (gst_config.JEWELLERY_JOB_WORK_GST_RATE), not priced here.

Old gold taken in exchange: value = net weight x the rate for its purity;
it is a payment against the new bill, after GST (the firm's decision -
GST on the new jewellery's full value).

The day's rate: the latest one set for that date; if only the pure rate
(24K / 999) was set, a lower purity's rate is derived from it by purity
(gst_config.METAL_PURITIES)."""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.utils.gst_config import METAL_PURITIES

ZERO = Decimal("0")
CENT = Decimal("0.01")
GRAM = Decimal("0.001")
MAKING_KINDS = {"per_gram": "Per gram", "percent": "% of metal value", "flat": "Flat amount"}
METALS = {"gold": "Gold", "silver": "Silver", "platinum": "Platinum"}


class JewelleryError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def money(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def weight(value) -> Decimal:
    return Decimal(value).quantize(GRAM, rounding=ROUND_HALF_UP)


@dataclass
class Price:
    metal: str
    purity: str
    net_weight: Decimal
    rate: Decimal
    metal_value: Decimal
    making: Decimal
    stones: Decimal

    @property
    def total(self) -> Decimal:
        return self.metal_value + self.making + self.stones

    def as_dict(self) -> dict:
        return {"metal": self.metal, "purity": self.purity, "net_weight": str(self.net_weight), "rate": str(self.rate),
                "metal_value": str(self.metal_value), "making": str(self.making), "stones": str(self.stones)}


def price(metal: str, purity: str, net_weight, rate, making_kind: str | None, making_value, stone_value) -> Price:
    net = weight(net_weight or 0)
    if net <= 0:
        raise JewelleryError("Net weight must be more than 0.")
    rate = money(rate)
    if rate <= 0:
        raise JewelleryError(f"Set today's {metal} {purity} rate first.")
    metal_value = money(net * rate)
    making_value = Decimal(making_value or 0)
    if making_value < 0 or Decimal(stone_value or 0) < 0:
        raise JewelleryError("Making charges and stone value can't be negative.")
    if not making_kind or making_value == 0:
        making = ZERO
    elif making_kind == "per_gram":
        making = money(net * making_value)
    elif making_kind == "percent":
        making = money(metal_value * making_value / 100)
    elif making_kind == "flat":
        making = money(making_value)
    else:
        raise JewelleryError("Unknown making-charge type.")
    return Price(metal, purity, net, rate, metal_value, making, money(stone_value or 0))


def derived_rate(metal: str, purity: str, rates: dict[str, Decimal]) -> Decimal | None:
    """rates: {purity: rate} set for the day. The purity's own rate, else
    one derived from the purest grade set."""
    if purity in rates:
        return money(rates[purity])
    grades = METAL_PURITIES.get(metal, {})
    if purity not in grades:
        return None
    for grade, fraction in sorted(grades.items(), key=lambda kv: -kv[1]):
        if grade in rates and fraction > 0:
            return money(rates[grade] / fraction * grades[purity])
    return None


def todays_rate(tenant_id: int, metal: str, purity: str, on: date | None = None) -> Decimal | None:
    from app.models.business import MetalRate

    on = on or date.today()
    rows = (MetalRate.query.filter_by(tenant_id=tenant_id, metal=metal, rate_date=on)
            .order_by(MetalRate.id).all())
    rates = {}
    for r in rows:  # later rows (corrections) win
        rates[r.purity] = r.rate_per_gram
    return derived_rate(metal, purity, rates)


def price_product(product, on: date | None = None, net_weight=None, making_value=None) -> Price:
    """A jewellery item's price today. The counter may weigh the piece
    again (net_weight) or agree a different making charge (making_value)."""
    rate = todays_rate(product.tenant_id, product.metal, product.purity, on)
    if rate is None:
        raise JewelleryError(f"Set today's {METALS.get(product.metal, product.metal).lower()} {product.purity} rate "
                             "first (Jewellery -> Today's rates).")
    return price(product.metal, product.purity, net_weight if net_weight is not None else product.net_weight, rate,
                 product.making_charge_kind,
                 making_value if making_value is not None else product.making_charge_value, product.stone_value)


def old_gold_value(net_weight, rate) -> Decimal:
    net = weight(net_weight or 0)
    if net <= 0:
        raise JewelleryError("Net weight must be more than 0.")
    if Decimal(rate or 0) <= 0:
        raise JewelleryError("Enter the rate per gram.")
    return money(net * Decimal(rate))
