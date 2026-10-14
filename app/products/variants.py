"""Variants (module "variants"): one design - a shirt, a shoe - sold in a
matrix of sizes / colours. Each variant is a full item of its own (own
SKU, barcode, price and stock) pointing at its design through parent_id;
the design itself is never sold or stocked (Product.has_variants)."""

from decimal import Decimal, InvalidOperation
from itertools import product as cartesian

from app.extensions import db
from app.inventory import ledger
from app.models.product import Product

MAX_ATTRIBUTES = 2
MAX_VALUES = 20
MAX_VARIANTS = 200

# Copied from the design onto each new variant.
_COPIED = ("description", "hsn_or_sac_code", "gst_rate", "unit", "sale_price", "price_includes_tax", "wholesale_price",
           "mrp", "purchase_rate", "cost_price", "min_stock_level", "is_service", "allow_negative_stock", "image_id",
           "category_id", "sub_category_id", "track_batch", "track_serial", "warranty_months")


class VariantError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def parse_attributes(names, values) -> list[tuple[str, list[str]]]:
    """[("Size", ["S", "M", "L"]), ("Colour", ["Red", "Blue"])] from the
    form's attribute name / comma-separated values pairs."""
    attributes = []
    for name, raw in zip(names, values):
        name = (name or "").strip()[:30]
        options = []
        for v in (raw or "").split(","):
            v = v.strip()[:30]
            if v and v.lower() not in {o.lower() for o in options}:
                options.append(v)
        if not name and not options:
            continue
        if not name or not options:
            raise VariantError("Give each attribute a name (e.g. Size) and its values (e.g. S, M, L).")
        if len(options) > MAX_VALUES:
            raise VariantError(f"At most {MAX_VALUES} values per attribute.")
        attributes.append((name, options))
    if not attributes:
        raise VariantError("Add at least one attribute, e.g. Size: S, M, L.")
    if len(attributes) > MAX_ATTRIBUTES:
        raise VariantError(f"At most {MAX_ATTRIBUTES} attributes (e.g. size and colour).")
    if len({n.lower() for n, _ in attributes}) != len(attributes):
        raise VariantError("Two attributes have the same name.")
    return attributes


def _key(attrs: dict) -> tuple:
    return tuple(sorted((k.lower(), str(v).lower()) for k, v in (attrs or {}).items()))


def _code(value: str) -> str:
    return "".join(ch for ch in value.upper() if ch.isalnum())[:8]


def generate(parent: Product, attributes: list[tuple[str, list[str]]]) -> list[Product]:
    """Makes the variants of the matrix that don't exist yet; returns the
    new ones. Existing variants are left as they are."""
    if parent.parent_id:
        raise VariantError("A variant can't have variants of its own.")
    if parent.is_service:
        raise VariantError("A service can't have sizes or colours.")
    if not parent.has_variants:
        on_hand = ledger.stock_on_hand(parent.tenant_id, [parent.id]).get(parent.id, Decimal("0"))
        if on_hand != 0:
            raise VariantError(f"{parent.name} has {ledger._fmt(on_hand)} in stock - adjust it to 0 first "
                               "(stock is kept per size/colour once it has variants).")
    combos = list(cartesian(*[[(name, v) for v in values] for name, values in attributes]))
    existing = {_key(v.variant_attributes): v for v in parent.variants}
    if len(set(existing) | {_key(dict(c)) for c in combos}) > MAX_VARIANTS:
        raise VariantError(f"At most {MAX_VARIANTS} variants per design.")
    created = []
    for combo in combos:
        attrs = dict(combo)
        if _key(attrs) in existing:
            continue
        label = " / ".join(attrs.values())
        variant = Product(tenant_id=parent.tenant_id, parent_id=parent.id, variant_attributes=attrs,
                          name=f"{parent.name} - {label}"[:255])
        for f in _COPIED:
            setattr(variant, f, getattr(parent, f))
        if parent.sku:
            variant.sku = _free_sku(parent.tenant_id, f"{parent.sku}-" + "-".join(_code(v) for v in attrs.values()))
        db.session.add(variant)
        created.append(variant)
    parent.has_variants = True
    parent.variant_attributes = {name: values for name, values in attributes}
    db.session.flush()
    return created


def _free_sku(tenant_id: int, wanted: str) -> str | None:
    wanted = wanted[:64]
    if Product.query.filter_by(tenant_id=tenant_id, sku=wanted).first() is None:
        return wanted
    return None  # left blank rather than clashing; the client can set one


def update_variants(parent: Product, form) -> int:
    """Saves the matrix table: each variant's SKU, barcode, price and
    whether it's still sold. Returns how many changed."""
    changed = 0
    for v in parent.variants:
        sku = (form.get(f"sku_{v.id}") or "").strip()[:64] or None
        barcode = (form.get(f"barcode_{v.id}") or "").strip()[:64] or None
        raw_price = (form.get(f"price_{v.id}") or "").strip()
        try:
            price = Decimal(raw_price).quantize(Decimal("0.01")) if raw_price else v.sale_price
        except InvalidOperation:
            raise VariantError(f"{v.name}: the price isn't a number.")
        if price < 0:
            raise VariantError(f"{v.name}: the price can't be negative.")
        active = bool(form.get(f"active_{v.id}"))
        for field_, value in (("sku", sku), ("barcode", barcode)):
            if value and Product.query.filter(Product.tenant_id == parent.tenant_id, getattr(Product, field_) == value,
                                              Product.id != v.id).first():
                raise VariantError(f"{v.name}: {field_.upper() if field_ == 'sku' else 'barcode'} {value} is already used.")
        before = (v.sku, v.barcode, v.sale_price, v.is_active)
        v.sku, v.barcode, v.sale_price, v.is_active = sku, barcode, price, active
        if before != (v.sku, v.barcode, v.sale_price, v.is_active):
            changed += 1
    db.session.flush()
    return changed
