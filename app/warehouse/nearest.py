"""The nearest-warehouse default for a sales order (owner's choice:
straight-line distance plus manual service areas; an order may be split
across warehouses when no single one has it all).

suggest_warehouse(tenant, pincode, lines) picks, in order:
  1. a manual service area matching the delivery pincode (an exact pincode
     beats the longest prefix);
  2. else the nearest active godown (haversine km from the pincode table) that
     has every line free - "nearest" when that is the nearest one, else
     "nearest_in_stock" with why the nearer ones were passed over;
     none can fill it all: the nearest, with what it is short of (and a plan
     to split the order across warehouses);
  3. an unknown or blank pincode: the branch's fallback warehouse, flagged.

Never in a template; the order form asks /warehouse/suggest.json, and
create_sales_document asks it when no warehouse is given (bulk / API).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

from app.inventory import ledger
from app.models.inventory import StockLocation
from app.models.product import Product
from app.models.warehouse import PincodeGeo, WarehouseServiceArea
from app.warehouse import dispatch

ZERO = Decimal("0")


def clean_pincode(value) -> str:
    digits = "".join(c for c in str(value or "") if c.isdigit())
    return digits if len(digits) == 6 and digits[0] != "0" else ""


def point_of_pincode(pincode: str) -> tuple[float, float] | None:
    pincode = clean_pincode(pincode)
    row = PincodeGeo.query.get(pincode) if pincode else None
    return (row.lat, row.lng) if row is not None else None


def point_of(location: StockLocation) -> tuple[float, float] | None:
    """A warehouse's point: typed in, else its pincode's."""
    if location.latitude is not None and location.longitude is not None:
        return location.latitude, location.longitude
    return point_of_pincode(location.pincode)


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle (straight-line) distance in km."""
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def service_area(tenant_id: int, pincode: str) -> StockLocation | None:
    """The warehouse whose service area covers this pincode: an exact pincode
    first, then the longest prefix. Switched-off warehouses don't count."""
    pincode = clean_pincode(pincode)
    if not pincode:
        return None
    rows = WarehouseServiceArea.query.filter(WarehouseServiceArea.tenant_id == tenant_id).all()
    best = None
    for row in rows:
        code = row.pincode_or_prefix
        loc = row.location
        if not pincode.startswith(code) or loc is None or not loc.is_active or loc.kind.value != "godown":
            continue
        if best is None or len(code) > len(best.pincode_or_prefix):
            best = row
    return best.location if best else None


def set_service_area(location: StockLocation, text: str) -> list[str]:
    """Replace a warehouse's service area from "628001, 628002, 629" (pincodes
    or 1-5 digit prefixes). A code already given to another warehouse is refused."""
    codes = []
    for part in str(text or "").replace("\n", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or not 1 <= len(part) <= 6 or part[0] == "0":
            raise ValueError(f"'{part}' isn't a pincode or a pincode's first digits.")
        if part not in codes:
            codes.append(part)
    others = {r.pincode_or_prefix: r.location.name for r in WarehouseServiceArea.query.filter(
        WarehouseServiceArea.tenant_id == location.tenant_id, WarehouseServiceArea.location_id != location.id)}
    clash = [c for c in codes if c in others]
    if clash:
        raise ValueError(f"{clash[0]} is already in {others[clash[0]]}'s service area.")
    from app.extensions import db

    WarehouseServiceArea.query.filter_by(tenant_id=location.tenant_id, location_id=location.id).delete()
    for code in codes:
        db.session.add(WarehouseServiceArea(tenant_id=location.tenant_id, location_id=location.id,
                                            pincode_or_prefix=code))
    db.session.flush()
    return codes


@dataclass
class Option:
    location: StockLocation
    km: float | None
    free: dict[int, Decimal] = field(default_factory=dict)
    short: list[tuple[str, Decimal, Decimal]] = field(default_factory=list)   # (item, free, wanted)

    @property
    def enough(self) -> bool:
        return not self.short


@dataclass
class Suggestion:
    location: StockLocation | None
    reason: str                       # service_area | nearest | nearest_in_stock | fallback
    ranked: list[Option]
    message: str = ""
    split: list[tuple[StockLocation, dict[int, Decimal]]] | None = None   # a plan: warehouse -> {line index: qty}


def _strict(tenant, wanted: dict[int, Decimal]) -> tuple[dict[int, Decimal], dict[int, Product]]:
    """The items a warehouse must actually have free (as check_free holds them)."""
    if not wanted or not tenant.has_feature("inventory"):
        return {}, {}
    products = {p.id: p for p in Product.query.filter(Product.id.in_(wanted.keys())).all()}
    strict = {pid: q for pid, q in wanted.items() if ledger.sells_from_stock(products.get(pid))
              and (products[pid].track_serial or not products[pid].allow_negative_stock)}
    return strict, products


def _fmt(qty) -> str:
    return ledger._fmt(Decimal(qty))


def suggest_warehouse(tenant, pincode, lines, fallback: StockLocation | None = None,
                      exclude_doc_id: int | None = None) -> Suggestion:
    """(location, reason, ranked list) for an order going to this pincode.
    lines: anything with product_id and qty."""
    warehouses = dispatch.warehouses(tenant.id)
    wanted = dispatch._wanted(lines)
    strict, products = _strict(tenant, wanted)
    rows = {r["location"].id: r["items"] for r in dispatch.free_by_warehouse(tenant.id, list(strict), exclude_doc_id)}
    here = point_of_pincode(pincode)
    ranked = []
    for loc in warehouses:
        there = point_of(loc)
        option = Option(loc, round(km(here, there), 1) if here and there else None)
        for pid, qty in strict.items():
            free = max(rows.get(loc.id, {}).get(pid, {}).get("free", ZERO), ZERO)
            option.free[pid] = free
            if free < qty:
                option.short.append((products[pid].name, free, qty))
        ranked.append(option)
    ranked.sort(key=lambda o: (o.km is None, o.km if o.km is not None else 0, o.location.name.lower()))
    if not warehouses:
        return Suggestion(None, "fallback", ranked, "No warehouse yet - add a godown under Stock locations.")

    def shortage(o: Option) -> str:
        return ", ".join(f"{name} ({_fmt(free)} free of {_fmt(want)})" for name, free, want in o.short)

    def where(o: Option) -> str:
        return f"{o.location.name}" + (f" ({o.km:g} km)" if o.km is not None else "")

    area = service_area(tenant.id, pincode)
    if area is not None:
        chosen = next(o for o in ranked if o.location.id == area.id)
        message = f"{area.name} serves this pincode."
        if not chosen.enough:
            message += f" It is short of {shortage(chosen)}."
        return Suggestion(area, "service_area", ranked, message, _split_if_needed(tenant, ranked, lines, chosen))
    if here is None:
        loc = fallback if fallback is not None and fallback.is_active else (warehouses[0] if len(warehouses) == 1 else None)
        what = "Pincode not found - check the warehouse." if clean_pincode(pincode) else "No delivery pincode - check the warehouse."
        return Suggestion(loc, "fallback", ranked, what if loc else what.replace("check", "pick"))
    with_point = [o for o in ranked if o.km is not None] or ranked
    nearest = with_point[0]
    if nearest.enough:
        return Suggestion(nearest.location, "nearest", ranked, f"Nearest: {where(nearest)}.")
    stocked = next((o for o in with_point if o.enough), None)
    if stocked is not None:
        return Suggestion(stocked.location, "nearest_in_stock", ranked,
                          f"{where(nearest)} is short of {shortage(nearest)} - defaulted to {where(stocked)}.")
    return Suggestion(nearest.location, "nearest", ranked,
                      f"No warehouse has the whole order. {where(nearest)} is short of {shortage(nearest)}.",
                      plan_split(tenant, ranked, lines))


def _split_if_needed(tenant, ranked, lines, chosen: Option):
    return None if chosen.enough or any(o.enough for o in ranked) else plan_split(tenant, ranked, lines)


def plan_split(tenant, ranked: list[Option], lines) -> list[tuple[StockLocation, dict[int, Decimal]]] | None:
    """Spread the order over warehouses, nearest first: each line takes what
    the nearest warehouse has free, the rest from the next. Items that needn't
    be in stock go with the nearest. None when even all of them together
    can't fill it."""
    if not ranked:
        return None
    wanted = dispatch._wanted(lines)
    strict, _ = _strict(tenant, wanted)
    left = {o.location.id: dict(o.free) for o in ranked}
    plan: dict[int, dict[int, Decimal]] = {}
    for index, line in enumerate(lines):
        pid = getattr(line, "product_id", None)
        qty = Decimal(line.qty)
        if not pid or pid not in strict:
            plan.setdefault(ranked[0].location.id, {})[index] = qty
            continue
        for option in ranked:
            if qty <= 0:
                break
            have = left[option.location.id].get(pid, ZERO)
            take = min(have, qty)
            if take > 0:
                plan.setdefault(option.location.id, {})[index] = plan.get(option.location.id, {}).get(index, ZERO) + take
                left[option.location.id][pid] = have - take
                qty -= take
        if qty > 0:
            return None
    by_id = {o.location.id: o.location for o in ranked}
    return [(by_id[lid], parts) for lid, parts in sorted(plan.items(), key=lambda kv: [o.location.id for o in ranked].index(kv[0]))]


def to_json(s: Suggestion, products: dict[int, str] | None = None) -> dict:
    return {
        "default": s.location.id if s.location else None, "reason": s.reason, "message": s.message,
        "warehouses": [{"id": o.location.id, "name": o.location.name, "km": o.km,
                        "free": {str(pid): float(q) for pid, q in o.free.items()}, "enough": o.enough}
                       for o in s.ranked],
        "split": ([{"id": loc.id, "name": loc.name, "lines": {str(i): float(q) for i, q in parts.items()}}
                   for loc, parts in s.split] if s.split else None),
    }
