"""What each importable/exportable master looks like - its columns, how a
row is validated, which existing record it updates, and how it's saved.

The workbook plumbing (app.bulk.excel) and the screens (app.bulk.routes)
are generic over these - Items, Customers and (with the Purchases
module on) Suppliers.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Callable

from app.bulk import cells
from app.bulk.cells import CellError
from app.extensions import db
from app.models.category import Category, SubCategory
from app.models.customer import Customer
from app.models.product import Product
from app.models.purchase import Supplier
from app.utils import gst_config
from app.utils.gst import GSTIN_REGEX, exceeds_mrp, hsn_error, normalize_gstin
from app.utils.indian_states import INDIAN_STATES, STATE_NAME_BY_CODE

_GSTIN_RE = re.compile(GSTIN_REGEX)
_STATE_CODE_BY_NAME = {name.lower(): code for code, name in INDIAN_STATES}


@dataclass
class Column:
    key: str
    header: str
    help: str
    example: str = ""
    required: bool = False
    # Excel number format "@" (text) for code-like columns, so leading
    # zeros and long digit strings survive being typed into the template.
    as_text: bool = False


@dataclass
class RowResult:
    row_number: int
    values: dict
    errors: list[str] = field(default_factory=list)
    existing_id: int | None = None

    @property
    def action(self) -> str:
        if self.errors:
            return "error"
        return "update" if self.existing_id else "create"


@dataclass
class ImportSpec:
    kind: str
    label: str  # plural, e.g. "Items"
    sheet_title: str
    columns: list[Column]
    parse_row: Callable  # (raw: dict, ctx) -> RowResult values/errors filled in
    save_row: Callable  # (row: RowResult, ctx) -> None
    export_rows: Callable  # (tenant) -> list[dict]
    make_context: Callable  # (tenant) -> ImportContext
    feature: str | None = None  # module flag the client must have on, if any


@dataclass
class ImportContext:
    tenant: object
    # natural key -> existing record id, for "update this row" matching
    by_key: dict = field(default_factory=dict)
    by_alt_key: dict = field(default_factory=dict)
    # values already claimed by an earlier row in the same file
    seen: dict = field(default_factory=dict)


def _cell(raw, key, parser, errors, *args):
    try:
        return parser(raw.get(key), *args)
    except CellError as exc:
        errors.append(f"{_header_for(key)}: {exc}")
        return None


_HEADERS: dict[str, str] = {}


def _header_for(key: str) -> str:
    return _HEADERS.get(key, key)


# --- Items ----------------------------------------------------------------

PRODUCT_COLUMNS = [
    Column("name", "Item name", "Required. Matches an existing item by name to update it.", "Masala Dosa", required=True),
    Column("hsn_or_sac_code", "HSN/SAC code", "Required. 4, 6 or 8 digits; SAC (99...) for a service.", "2106", required=True, as_text=True),
    Column("gst_rate", "GST rate %", "Required. One of the GST slabs, e.g. 0, 5, 12, 18, 28.", "5", required=True),
    Column("sale_price", "Sale price", "Required. Your normal selling price.", "120", required=True),
    Column("price_includes_tax", "Price includes GST", "Yes or No. Blank = your Settings default.", "Yes"),
    Column("unit", "Unit", "A GST unit code, e.g. PCS, KGS, LTR, NOS, BOX. Blank = PCS.", "NOS", as_text=True),
    Column("is_service", "Service", "Yes for a service (no stock). Blank = No.", "No"),
    Column("mrp", "MRP", "Optional. Includes GST; the sale price can't exceed it.", ""),
    Column("wholesale_price", "Wholesale price", "Optional.", ""),
    Column("purchase_rate", "Purchase rate", "Optional. Last price paid to the supplier.", ""),
    Column("cost_price", "Cost price", "Optional. Average cost of stock.", ""),
    Column("sku", "SKU", "Optional. Your own item code - must be unique.", "", as_text=True),
    Column("barcode", "Barcode", "Optional. Must be unique.", "", as_text=True),
    Column("alt_unit", "Pack unit", "Optional bigger unit, e.g. BOX.", "", as_text=True),
    Column("alt_unit_factor", "Units per pack", "Needed with Pack unit, e.g. 12.", ""),
    Column("min_stock_level", "Low-stock alert below", "Optional.", ""),
    Column("category", "Category", "Optional. Created if new (only used with Item categories switched on).", "Breakfast"),
    Column("sub_category", "Sub-category", "Optional, under the Category.", ""),
    Column("description", "Description", "Optional.", ""),
]


def _product_context(tenant) -> ImportContext:
    ctx = ImportContext(tenant=tenant)
    for p in Product.query.filter_by(tenant_id=tenant.id).all():
        if p.is_active:
            ctx.by_key.setdefault(p.name.strip().lower(), p.id)
        if p.sku:
            ctx.by_alt_key[("sku", p.sku)] = p.id
        if p.barcode:
            ctx.by_alt_key[("barcode", p.barcode)] = p.id
    return ctx


def _parse_product(raw: dict, ctx: ImportContext, row: RowResult) -> None:
    errors = row.errors
    tenant = ctx.tenant
    v = row.values
    v["name"] = cells.text(raw.get("name"))
    if not v["name"]:
        errors.append("Item name is required.")
    elif len(v["name"]) > 255:
        errors.append("Item name: at most 255 characters.")
    v["description"] = cells.text(raw.get("description"))
    v["is_service"] = _cell(raw, "is_service", cells.yes_no, errors, False) or False
    v["hsn_or_sac_code"] = _cell(raw, "hsn_or_sac_code", cells.hsn, errors)
    if not v["hsn_or_sac_code"]:
        if "HSN/SAC code" not in " ".join(errors):
            errors.append("HSN/SAC code is required.")
    else:
        hsn_problem = hsn_error(v["hsn_or_sac_code"], tenant.aato, v["is_service"])
        if hsn_problem:
            errors.append(hsn_problem)
    v["gst_rate"] = _cell(raw, "gst_rate", cells.gst_rate, errors)
    v["sale_price"] = _cell(raw, "sale_price", cells.decimal, errors)
    if v["sale_price"] is None and not any(e.startswith("Sale price") for e in errors):
        errors.append("Sale price is required.")
    v["price_includes_tax"] = _cell(raw, "price_includes_tax", cells.yes_no, errors, tenant.default_price_includes_tax)
    unit = (cells.text(raw.get("unit")) or gst_config.DEFAULT_UQC).upper()
    if unit not in gst_config.UQC_CODES:
        errors.append(f"Unit: '{unit}' isn't a GST unit code (e.g. PCS, NOS, KGS, LTR, BOX).")
    v["unit"] = unit
    for key in ("mrp", "wholesale_price", "purchase_rate", "cost_price"):
        v[key] = _cell(raw, key, cells.decimal, errors)
    v["min_stock_level"] = _cell(raw, "min_stock_level", cells.decimal, errors, 3)
    v["alt_unit_factor"] = _cell(raw, "alt_unit_factor", cells.decimal, errors, 3)
    alt_unit = cells.text(raw.get("alt_unit"))
    v["alt_unit"] = alt_unit.upper() if alt_unit else None
    if v["alt_unit"] and v["alt_unit"] not in gst_config.UQC_CODES:
        errors.append(f"Pack unit: '{v['alt_unit']}' isn't a GST unit code.")
    if bool(v["alt_unit"]) != bool(v["alt_unit_factor"]):
        errors.append("Pack unit and Units per pack go together - fill both or neither.")
    if v["sale_price"] is not None and v["gst_rate"] is not None and exceeds_mrp(
        v["sale_price"], v["gst_rate"], v["price_includes_tax"], v["mrp"]
    ):
        errors.append("With GST, the sale price is more than the MRP.")
    v["sku"] = cells.text(raw.get("sku"))
    v["barcode"] = cells.text(raw.get("barcode"))
    v["category"] = cells.text(raw.get("category"))
    v["sub_category"] = cells.text(raw.get("sub_category"))
    if v["sub_category"] and not v["category"]:
        errors.append("Sub-category needs a Category.")

    # Which existing item this row updates: its SKU if it has one on
    # file, otherwise the same name.
    existing = None
    if v["sku"] and ("sku", v["sku"]) in ctx.by_alt_key:
        existing = ctx.by_alt_key[("sku", v["sku"])]
    elif v["name"]:
        existing = ctx.by_key.get(v["name"].lower())
    row.existing_id = existing

    for code_key, label in (("sku", "SKU"), ("barcode", "Barcode")):
        code = v[code_key]
        if not code:
            continue
        if len(code) > 64:
            errors.append(f"{label}: at most 64 characters.")
        owner = ctx.by_alt_key.get((code_key, code))
        if owner and owner != existing:
            errors.append(f"{label} {code} already belongs to another item.")
        earlier = ctx.seen.get((code_key, code))
        if earlier:
            errors.append(f"{label} {code} is also on row {earlier}.")
        ctx.seen.setdefault((code_key, code), row.row_number)
    if v["name"]:
        earlier = ctx.seen.get(("name", v["name"].lower()))
        if earlier:
            errors.append(f"'{v['name']}' is also on row {earlier}.")
        ctx.seen.setdefault(("name", v["name"].lower()), row.row_number)


def _category_ids(ctx: ImportContext, category_name, sub_name):
    if not category_name or not ctx.tenant.has_feature("categories"):
        return None, None
    tenant_id = ctx.tenant.id
    category = (
        Category.query.filter(Category.tenant_id == tenant_id, Category.is_active.is_(True))
        .filter(db.func.lower(Category.name) == category_name.lower())
        .first()
    )
    if category is None:
        category = Category(tenant_id=tenant_id, name=category_name[:100])
        db.session.add(category)
        db.session.flush()
    sub = None
    if sub_name:
        sub = (
            SubCategory.query.filter(
                SubCategory.tenant_id == tenant_id,
                SubCategory.category_id == category.id,
                SubCategory.is_active.is_(True),
            )
            .filter(db.func.lower(SubCategory.name) == sub_name.lower())
            .first()
        )
        if sub is None:
            sub = SubCategory(tenant_id=tenant_id, category_id=category.id, name=sub_name[:100])
            db.session.add(sub)
            db.session.flush()
    return category.id, sub.id if sub else None


def _save_product(row: RowResult, ctx: ImportContext) -> None:
    v = row.values
    if row.existing_id:
        product = Product.query.filter_by(id=row.existing_id, tenant_id=ctx.tenant.id).one()
    else:
        product = Product(tenant_id=ctx.tenant.id)
        db.session.add(product)
    for key in (
        "name", "description", "is_service", "hsn_or_sac_code", "gst_rate", "sale_price", "price_includes_tax",
        "unit", "mrp", "wholesale_price", "purchase_rate", "cost_price", "min_stock_level", "alt_unit",
        "alt_unit_factor", "sku", "barcode",
    ):
        setattr(product, key, v[key])
    product.is_active = True
    if v["category"]:
        product.category_id, product.sub_category_id = _category_ids(ctx, v["category"], v["sub_category"])


def _yes_no(flag: bool) -> str:
    return "Yes" if flag else "No"


def _export_products(tenant) -> list[dict]:
    rows = []
    for p in Product.query.filter_by(tenant_id=tenant.id, is_active=True).order_by(Product.name).all():
        rows.append(
            {
                "name": p.name,
                "hsn_or_sac_code": p.hsn_or_sac_code,
                "gst_rate": p.gst_rate,
                "sale_price": p.sale_price,
                "price_includes_tax": _yes_no(p.price_includes_tax),
                "unit": p.unit,
                "is_service": _yes_no(p.is_service),
                "mrp": p.mrp,
                "wholesale_price": p.wholesale_price,
                "purchase_rate": p.purchase_rate,
                "cost_price": p.cost_price,
                "sku": p.sku,
                "barcode": p.barcode,
                "alt_unit": p.alt_unit,
                "alt_unit_factor": p.alt_unit_factor,
                "min_stock_level": p.min_stock_level,
                "category": p.category.name if p.category else None,
                "sub_category": p.sub_category.name if p.sub_category else None,
                "description": p.description,
            }
        )
    return rows


# --- Customers --------------------------------------------------------------

CUSTOMER_COLUMNS = [
    Column("name", "Customer name", "Required. Matches an existing customer (by GSTIN, else name) to update it.", "Ravi Traders", required=True),
    Column("gstin", "GSTIN", "Optional. 15 characters; blank for an unregistered customer.", "33AAAAA0000A1Z5", as_text=True),
    Column("state", "State", "State name or 2-digit code. Blank = taken from the GSTIN.", "Tamil Nadu"),
    Column("address_line1", "Address line 1", "Optional.", ""),
    Column("address_line2", "Address line 2", "Optional.", ""),
    Column("city", "City", "Optional.", "Chennai"),
    Column("pincode", "PIN code", "Optional.", "600001", as_text=True),
    Column("phone", "Phone", "Optional.", "", as_text=True),
    Column("email", "Email", "Optional.", ""),
]


def _customer_context(tenant) -> ImportContext:
    ctx = ImportContext(tenant=tenant)
    for c in Customer.query.filter_by(tenant_id=tenant.id, is_active=True).all():
        ctx.by_key.setdefault(c.name.strip().lower(), c.id)
        if c.gstin:
            ctx.by_alt_key[("gstin", c.gstin)] = c.id
    return ctx


def _state_code(value) -> str | None:
    raw = cells.text(value)
    if not raw:
        return None
    if raw.isdigit():
        code = raw.zfill(2)
        return code if code in STATE_NAME_BY_CODE else None
    return _STATE_CODE_BY_NAME.get(raw.lower())


def _parse_customer(raw: dict, ctx: ImportContext, row: RowResult) -> None:
    errors = row.errors
    v = row.values
    v["name"] = cells.text(raw.get("name"))
    if not v["name"]:
        errors.append("Customer name is required.")
    elif len(v["name"]) > 255:
        errors.append("Customer name: at most 255 characters.")
    v["gstin"] = normalize_gstin(cells.text(raw.get("gstin")))
    if v["gstin"] and not _GSTIN_RE.match(v["gstin"]):
        errors.append(f"GSTIN '{v['gstin']}' isn't a valid 15-character GSTIN.")
    state_raw = raw.get("state")
    state_code = _state_code(state_raw)
    if cells.text(state_raw) and not state_code:
        errors.append(f"State '{cells.text(state_raw)}' isn't a state name or code.")
    if not state_code and v["gstin"] and v["gstin"][:2] in STATE_NAME_BY_CODE:
        state_code = v["gstin"][:2]
    if not state_code and not errors:
        errors.append("State is required (or give a GSTIN).")
    v["state_code"] = state_code
    for key, limit in (("address_line1", 255), ("address_line2", 255), ("city", 100), ("pincode", 10), ("phone", 20), ("email", 255)):
        v[key] = cells.text(raw.get(key))
        if v[key] and len(v[key]) > limit:
            errors.append(f"{_header_for(key)}: at most {limit} characters.")

    existing = None
    if v["gstin"] and ("gstin", v["gstin"]) in ctx.by_alt_key:
        existing = ctx.by_alt_key[("gstin", v["gstin"])]
    elif v["name"] and not v["gstin"]:
        existing = ctx.by_key.get(v["name"].lower())
    row.existing_id = existing
    dedupe_key = ("gstin", v["gstin"]) if v["gstin"] else ("name", (v["name"] or "").lower())
    earlier = ctx.seen.get(dedupe_key)
    if earlier and v["name"]:
        errors.append(f"Also on row {earlier}.")
    ctx.seen.setdefault(dedupe_key, row.row_number)


def _save_customer(row: RowResult, ctx: ImportContext) -> None:
    v = row.values
    if row.existing_id:
        customer = Customer.query.filter_by(id=row.existing_id, tenant_id=ctx.tenant.id).one()
    else:
        customer = Customer(tenant_id=ctx.tenant.id)
        db.session.add(customer)
    for key in ("name", "gstin", "state_code", "address_line1", "address_line2", "city", "pincode", "phone", "email"):
        setattr(customer, key, v[key])
    customer.state_name = STATE_NAME_BY_CODE.get(v["state_code"], "")
    customer.is_active = True


def _export_customers(tenant) -> list[dict]:
    return [
        {
            "name": c.name,
            "gstin": c.gstin,
            "state": c.state_name or c.state_code,
            "address_line1": c.address_line1,
            "address_line2": c.address_line2,
            "city": c.city,
            "pincode": c.pincode,
            "phone": c.phone,
            "email": c.email,
        }
        for c in Customer.query.filter_by(tenant_id=tenant.id, is_active=True).order_by(Customer.name).all()
    ]


# --- Suppliers (Purchases module) --------------------------------------------

SUPPLIER_COLUMNS = [
    Column("name", "Supplier name", "Required. Matches an existing supplier (by GSTIN, else name) to update it.", "Sri Balaji Distributors", required=True),
    Column("gstin", "GSTIN", "Optional. 15 characters; blank for an unregistered supplier.", "33BBBBB1111B1Z5", as_text=True),
    Column("state", "State", "State name or 2-digit code. Blank = taken from the GSTIN.", "Tamil Nadu"),
    Column("address_line1", "Address", "Optional.", ""),
    Column("city", "City", "Optional.", "Chennai"),
    Column("pincode", "PIN code", "Optional.", "600001", as_text=True),
    Column("contact_person", "Contact person", "Optional.", ""),
    Column("phone", "Phone", "Optional.", "", as_text=True),
    Column("email", "Email", "Optional.", ""),
    Column("payment_terms_days", "Credit days", "Optional. Days you get to pay, e.g. 30.", "30"),
    Column("opening_balance", "Opening balance", "Optional. What you owed them when you started using ARFA.", "0"),
]


def _supplier_context(tenant) -> ImportContext:
    ctx = ImportContext(tenant=tenant)
    for s in Supplier.query.filter_by(tenant_id=tenant.id, is_active=True).all():
        ctx.by_key.setdefault(s.name.strip().lower(), s.id)
        if s.gstin:
            ctx.by_alt_key[("gstin", s.gstin)] = s.id
    return ctx


def _parse_supplier(raw: dict, ctx: ImportContext, row: RowResult) -> None:
    errors = row.errors
    v = row.values
    v["name"] = cells.text(raw.get("name"))
    if not v["name"]:
        errors.append("Supplier name is required.")
    elif len(v["name"]) > 255:
        errors.append("Supplier name: at most 255 characters.")
    v["gstin"] = normalize_gstin(cells.text(raw.get("gstin")))
    if v["gstin"] and not _GSTIN_RE.match(v["gstin"]):
        errors.append(f"GSTIN '{v['gstin']}' isn't a valid 15-character GSTIN.")
    state_raw = raw.get("state")
    state_code = _state_code(state_raw)
    if cells.text(state_raw) and not state_code:
        errors.append(f"State '{cells.text(state_raw)}' isn't a state name or code.")
    if not state_code and v["gstin"] and v["gstin"][:2] in STATE_NAME_BY_CODE:
        state_code = v["gstin"][:2]
    if not state_code and not errors:
        errors.append("State is required (or give a GSTIN).")
    v["state_code"] = state_code
    for key, limit in (("address_line1", 255), ("city", 100), ("pincode", 10), ("contact_person", 100), ("phone", 20), ("email", 255)):
        v[key] = cells.text(raw.get(key))
        if v[key] and len(v[key]) > limit:
            errors.append(f"{_header_for(key)}: at most {limit} characters.")
    days = cells.text(raw.get("payment_terms_days"))
    v["payment_terms_days"] = None
    if days:
        try:
            v["payment_terms_days"] = int(Decimal(days))
            if v["payment_terms_days"] < 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            errors.append("Credit days must be a whole number.")
    balance = cells.text(raw.get("opening_balance"))
    v["opening_balance"] = Decimal("0")
    if balance:
        try:
            v["opening_balance"] = Decimal(balance.replace(",", "")).quantize(Decimal("0.01"))
        except InvalidOperation:
            errors.append("Opening balance must be a number.")

    existing = None
    if v["gstin"] and ("gstin", v["gstin"]) in ctx.by_alt_key:
        existing = ctx.by_alt_key[("gstin", v["gstin"])]
    elif v["name"] and not v["gstin"]:
        existing = ctx.by_key.get(v["name"].lower())
    row.existing_id = existing
    dedupe_key = ("gstin", v["gstin"]) if v["gstin"] else ("name", (v["name"] or "").lower())
    earlier = ctx.seen.get(dedupe_key)
    if earlier and v["name"]:
        errors.append(f"Also on row {earlier}.")
    ctx.seen.setdefault(dedupe_key, row.row_number)


def _save_supplier(row: RowResult, ctx: ImportContext) -> None:
    v = row.values
    if row.existing_id:
        supplier = Supplier.query.filter_by(id=row.existing_id, tenant_id=ctx.tenant.id).one()
    else:
        supplier = Supplier(tenant_id=ctx.tenant.id)
        db.session.add(supplier)
    for key in ("name", "gstin", "state_code", "address_line1", "city", "pincode", "contact_person", "phone", "email",
                "payment_terms_days", "opening_balance"):
        setattr(supplier, key, v[key])
    supplier.state_name = STATE_NAME_BY_CODE.get(v["state_code"], "")
    supplier.is_active = True


def _export_suppliers(tenant) -> list[dict]:
    return [
        {
            "name": s.name, "gstin": s.gstin, "state": s.state_name or s.state_code, "address_line1": s.address_line1,
            "city": s.city, "pincode": s.pincode, "contact_person": s.contact_person, "phone": s.phone, "email": s.email,
            "payment_terms_days": s.payment_terms_days, "opening_balance": s.opening_balance,
        }
        for s in Supplier.query.filter_by(tenant_id=tenant.id, is_active=True).order_by(Supplier.name).all()
    ]


SPECS = {
    "products": ImportSpec(
        kind="products",
        label="Items",
        sheet_title="Items",
        columns=PRODUCT_COLUMNS,
        parse_row=_parse_product,
        save_row=_save_product,
        export_rows=_export_products,
        make_context=_product_context,
    ),
    "customers": ImportSpec(
        kind="customers",
        label="Customers",
        sheet_title="Customers",
        columns=CUSTOMER_COLUMNS,
        parse_row=_parse_customer,
        save_row=_save_customer,
        export_rows=_export_customers,
        make_context=_customer_context,
    ),
    "suppliers": ImportSpec(
        kind="suppliers",
        label="Suppliers",
        sheet_title="Suppliers",
        columns=SUPPLIER_COLUMNS,
        parse_row=_parse_supplier,
        save_row=_save_supplier,
        export_rows=_export_suppliers,
        make_context=_supplier_context,
        feature="purchases",
    ),
}

for _spec in SPECS.values():
    for _col in _spec.columns:
        _HEADERS.setdefault(_col.key, _col.header)
