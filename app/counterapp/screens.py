"""What the ARFA Counter app's phone screens show (counter app, stage 2).

The Bill screen is the plain counter: tap items, see the total, take cash,
UPI or card in one tap. It bills through the counter's own checkout
(/pos/checkout) - same numbering, GST, stock and audit - and, with offline
billing on, through the same offline queue (pos_offline.js) in this
phone's own series. Anything that needs more than that (serial numbers,
margin-scheme goods, jewellery, prescriptions, discounts, customers on
credit) is billed on the full counter screen, which the app links to.
"""

from datetime import date

from flask import url_for
from flask_wtf.csrf import generate_csrf

from app.models.pos_bill import PAYMENT_MODE_LABELS, PaymentMode
from app.models.product import Product
from app.models.tenant import RegistrationType
from app.utils.features import current_tenant_has
from app.utils.tenant_scope import tenant_query

# The phone takes the whole bill one way - the modes a customer pays at a
# counter in one tap.
APP_MODES = (PaymentMode.CASH, PaymentMode.UPI, PaymentMode.CARD)


def _items(tenant, user, products) -> list[dict]:
    from app.pos.catalog import build_category_tree, favourite_products
    from app.pos.offline import blocked_products
    from app.pos.services import counter_price, resolve_price_list

    price_list = resolve_price_list(tenant, user, None)
    prices = price_list.price_map() if price_list else None
    top = {}
    if current_tenant_has("categories"):
        groups, _ = build_category_tree(products)
        for group in groups:
            for p in group.loose_products:
                top[p.id] = group.id
            for sub in group.subcategories:
                for p in sub.products:
                    top[p.id] = group.id
    favourites = ({p.id for p in favourite_products(tenant.id, products)}
                  if current_tenant_has("favourites") else set())
    counter_only = set(blocked_products(tenant, products))
    today = date.today()
    return [{
        "id": p.id, "name": p.name, "rate": float(counter_price(p, price_list, prices)),
        "incl": bool(p.price_includes_tax), "gst_rate": float(p.gst_rate), "unit": p.unit,
        "hsn": p.hsn_or_sac_code or "", "sku": p.sku or "", "barcode": p.barcode or "",
        "image": url_for("main.media", image_id=p.image_id) if p.image_id else "",
        "cat": top.get(p.id), "fav": p.id in favourites, "loose": bool(p.sells_loose),
        "sold_out": p.sold_out_on is not None and p.sold_out_on == today,
        "counter_only": p.id in counter_only,
    } for p in products]


def _categories(products) -> list[dict]:
    from app.pos.catalog import build_category_tree

    if not current_tenant_has("categories"):
        return []
    groups, _ = build_category_tree(products)
    return [{"id": g.id, "name": g.name} for g in groups]


def _order_types(tenant) -> list[dict]:
    """A restaurant's counter order types (takeaway, delivery...). Dine-in
    is billed from the table it was served at."""
    if not current_tenant_has("restaurant_mode"):
        return []
    from app.restaurant.services import order_types

    return [t for t in order_types(tenant) if t["value"] != "dine_in"]


def _packing_on(tenant, order_types) -> list[str]:
    """Order types that carry a packing charge - added by the server, so
    not on a bill made offline (the screen says so)."""
    if not order_types:
        return []
    from app.restaurant.services import settings
    from app.utils.module_settings import dec

    s = settings(tenant)
    return list(s["packing_on"]) if dec(s["packing_charge"]) > 0 else []


def offline_config(tenant, user) -> dict | None:
    """The counter's offline set-up (pos.routes._module_context), with the
    app's own service worker so the app's screens open without internet."""
    if not current_tenant_has("offline_pos"):
        return None
    from app.pos.offline import OFFLINE_MODES
    from app.pos.services import enabled_payment_modes

    gstin = tenant.gstins.filter_by(is_active=True).first()
    return {
        "tenant_id": tenant.id, "user_id": user.id,
        "register_url": url_for("pos.offline_register"), "state_url": url_for("pos.offline_state"),
        "sync_url": url_for("pos.offline_sync"), "sw_url": url_for("counterapp.service_worker"),
        "sw_scope": url_for("counterapp.index"), "csrf": generate_csrf(),
        "receipt": {"name": tenant.display_name, "gstin": (gstin.gstin or "") if gstin else ""},
        "charges_gst": tenant.registration_type == RegistrationType.REGULAR,
        "modes": [m.value for m in enabled_payment_modes(tenant) if m in OFFLINE_MODES], "blocked_ids": [],
    }


def bill_context(tenant, user) -> dict:
    from app.pos import cashiers
    from app.pos.services import enabled_payment_modes
    from app.pos.sessions import current_session, day_close_on

    products = (tenant_query(Product).filter_by(is_active=True).filter(Product.has_variants.is_(False))
                .order_by(Product.name).all())
    items = _items(tenant, user, products)
    order_types = _order_types(tenant)
    offline = offline_config(tenant, user)
    if offline:
        offline["blocked_ids"] = [i["id"] for i in items if i["counter_only"]]
    cashier = None
    if cashiers.uses_pins(tenant, user):
        current = cashiers.current_cashier(tenant, user)
        cashier = {"current": {"id": current.id, "name": current.name} if current else None,
                   "unlock_url": url_for("pos.cashier_unlock"), "lock_url": url_for("pos.cashier_lock")}
    needs_day = day_close_on(tenant) and current_session(tenant, user) is None
    default_type = next((t["value"] for t in order_types if t["value"] == "takeaway"),
                        order_types[0]["value"] if order_types else "")
    config = {
        "items": items,
        "categories": _categories(products),
        "favourites": any(i["fav"] for i in items),
        "modes": [{"value": m.value, "label": PAYMENT_MODE_LABELS[m]}
                  for m in enabled_payment_modes(tenant) if m in APP_MODES],
        "charges_gst": tenant.registration_type == RegistrationType.REGULAR,
        "order_types": order_types, "order_type": default_type, "packing_on": _packing_on(tenant, order_types),
        # Prices that depend on more than the item (a restaurant's order
        # type and its charges, wholesale slabs) come from the server.
        "quote_url": url_for("pos.quote") if (order_types or current_tenant_has("wholesale")) else "",
        "checkout_url": url_for("pos.checkout_route"),
        "ping_url": url_for("counterapp.ping"),
        "resume_url": url_for("counterapp.resume"),
        "receipt_json_url": url_for("pos.receipt_json", bill_id=0),
        "receipt_pdf_url": url_for("pos.receipt_pdf", bill_id=0),
        "share_url": url_for("pos.share_bill", bill_id=0),
        "upi_qr_url": url_for("pos.upi_qr") if tenant.upi_vpa else "",
        "receipt_format": tenant.default_receipt_format,
        "csrf": generate_csrf(),
        "offline": offline,
        "cashier": cashier,
        "user_id": user.id, "tenant_id": tenant.id,
        "business": tenant.display_name, "branch": user.name,
        "view": "bill",
        # Tables & KOT (stage 3), for a restaurant.
        "tables_on": current_tenant_has("restaurant_mode"),
        "tables_url": url_for("counterapp.tables") if current_tenant_has("restaurant_mode") else "",
        "tables_state_url": url_for("counterapp.tables_state") if current_tenant_has("restaurant_mode") else "",
        "kot_url": url_for("counterapp.send_kots") if current_tenant_has("restaurant_mode") else "",
    }
    return {"config": config, "needs_day": needs_day, "counter_url": url_for("pos.terminal")}
