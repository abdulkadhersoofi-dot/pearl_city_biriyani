"""Wholesale pricing (module "wholesale") - what one unit of an item costs
a given customer for a given quantity:

  1. the base price: the counter's price list if one was picked, else the
     customer's group price list (CustomerGroup.price_list), else the
     item's normal sale price; an item missing from a list sells at its
     normal price;
  2. a quantity slab (PriceSlab) applies when the quantity reaches its
     min_qty - the biggest slab reached wins - and only if it is cheaper
     than the base price (a slab is a break, never a mark-up).

Prices are on the item's own basis (GST-inclusive or not, as the item
says); quantities are in the item's own unit."""

from decimal import Decimal

from app.models.business import PriceSlab
from app.models.price_list import PriceListItem


def group_list_id(customer) -> int | None:
    group = getattr(customer, "customer_group", None) if customer is not None else None
    if group is None or not group.is_active or not group.price_list_id:
        return None
    if group.price_list is None or not group.price_list.is_active:
        return None
    return group.price_list_id


def list_price(tenant_id: int, price_list_id: int | None, product) -> Decimal | None:
    if not price_list_id:
        return None
    item = PriceListItem.query.filter_by(tenant_id=tenant_id, price_list_id=price_list_id, product_id=product.id).first()
    return item.price if item else None


def slab_price(tenant_id: int, product, qty) -> Decimal | None:
    slab = (PriceSlab.query.filter(PriceSlab.tenant_id == tenant_id, PriceSlab.product_id == product.id,
                                   PriceSlab.min_qty <= Decimal(qty))
            .order_by(PriceSlab.min_qty.desc()).first())
    return slab.price if slab else None


def unit_price(tenant_id: int, product, qty, customer=None, price_list_id: int | None = None,
               base: Decimal | None = None) -> Decimal:
    """`base` is the price already chosen by the counter (its price list);
    without it the customer's group list or the normal price is used."""
    if base is None:
        base = list_price(tenant_id, price_list_id or group_list_id(customer), product)
    if base is None:
        base = product.sale_price
    slab = slab_price(tenant_id, product, qty)
    if slab is not None and slab < base:
        return slab
    return base
