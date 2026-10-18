"""Restaurant mode (flag restaurant_mode): order types, tables with
running orders, kitchen order tickets (KOT), packing and service charges,
"sold out today".

A dine-in table's running order is a HELD counter bill carrying the
table_id; "Send KOT" saves the cart as that order and sends the kitchen
only what changed since the last ticket (a lower quantity goes as a CANCEL
line). Billing the table checks out an ordinary counter bill, marks the
table's tickets billed and frees the table."""

from datetime import date
from decimal import Decimal

from sqlalchemy import func

from app.extensions import db
from app.models.business import DiningTable, KitchenTicket, KotStatus
from app.models.pos_bill import POSBill, POSBillStatus
from app.models.tenant import Tenant
from app.utils.gst import round_money
from app.utils.module_settings import ORDER_TYPES, dec, module_settings

ZERO = Decimal("0")


class RestaurantError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def settings(tenant) -> dict:
    return module_settings(tenant, "restaurant")


def order_types(tenant) -> list[dict]:
    s = settings(tenant)
    return [{"value": k, "label": ORDER_TYPES[k], "price_list_id": s["price_lists"].get(k)}
            for k in s["order_types"] if k in ORDER_TYPES]


def price_list_for(tenant, order_type: str | None) -> int | None:
    if not order_type or not tenant.has_feature("restaurant_mode"):
        return None
    return settings(tenant)["price_lists"].get(order_type)


def is_sold_out(product, today: date | None = None) -> bool:
    return product.sold_out_on is not None and product.sold_out_on == (today or date.today())


# --- packing & service charge ------------------------------------------------------------

def charge_lines(tenant, cart, food_lines: list, charge_gst: bool):
    """The packing / service charge lines for this cart (CartLineInput),
    from the restaurant settings. food_lines are the computed item lines
    (the service charge is a % of their taxable value)."""
    from app.pos.services import CartLineInput

    if not tenant.has_feature("restaurant_mode") or not cart.order_type:
        return []
    s = settings(tenant)
    rate = dec(s["charge_gst_rate"]) if charge_gst else ZERO
    out = []
    packing = dec(s["packing_charge"])
    if packing > 0 and cart.order_type in s["packing_on"]:
        if s["packing_per"] == "item":
            units = sum((Decimal(1) if l["input"].unit_factor != 1 or _loose(l["input"]) else Decimal(l["input"].qty)
                         for l in food_lines), ZERO)
        else:
            units = Decimal(1)
        if units > 0:
            out.append(CartLineInput(description="Packing charge", hsn_or_sac_code=_sac(), qty=units, rate=packing,
                                     gst_rate=rate, unit="NOS", charge_kind="packing"))
    pct = dec(s["service_charge_percent"])
    if cart.service_charge and pct > 0 and cart.order_type == "dine_in":
        base = sum((l["taxable_value"] for l in food_lines), ZERO)
        amount = round_money(base * pct / 100)
        if amount > 0:
            out.append(CartLineInput(description=f"Service charge ({pct.normalize():f}%)", hsn_or_sac_code=_sac(),
                                     qty=Decimal(1), rate=amount, gst_rate=rate, unit="NOS", charge_kind="service"))
    return out


def _sac():
    from app.utils.gst_config import RESTAURANT_SERVICE_SAC

    return RESTAURANT_SERVICE_SAC


def _loose(line) -> bool:
    return Decimal(line.qty) != Decimal(line.qty).to_integral_value()


# --- tables & running orders --------------------------------------------------------------

def running_order(tenant_id: int, table_id: int) -> POSBill | None:
    return (POSBill.query.filter_by(tenant_id=tenant_id, table_id=table_id, status=POSBillStatus.HELD)
            .order_by(POSBill.id.desc()).first())


def open_tickets(tenant_id: int, table_id: int):
    return (KitchenTicket.query.filter_by(tenant_id=tenant_id, table_id=table_id, status=KotStatus.SENT)
            .order_by(KitchenTicket.id).all())


def overview(tenant) -> list[dict]:
    tables = (DiningTable.query.filter_by(tenant_id=tenant.id, is_active=True)
              .order_by(DiningTable.sort_order, DiningTable.name).all())
    orders = {b.table_id: b for b in POSBill.query.filter(POSBill.tenant_id == tenant.id, POSBill.status == POSBillStatus.HELD,
                                                          POSBill.table_id.isnot(None))}
    kots = dict(db.session.query(KitchenTicket.table_id, func.count(KitchenTicket.id))
                .filter(KitchenTicket.tenant_id == tenant.id, KitchenTicket.status == KotStatus.SENT,
                        KitchenTicket.table_id.isnot(None)).group_by(KitchenTicket.table_id).all())
    return [{"table": t, "order": orders.get(t.id), "kots": kots.get(t.id, 0)} for t in tables]


def _key(product_id, description, note):
    return f"{product_id or ''}|{description}|{note or ''}"


def sent_quantities(tenants_tickets) -> dict:
    sent: dict[str, dict] = {}
    for kot in tenants_tickets:
        for item in kot.items:
            k = _key(item.get("product_id"), item["name"], item.get("note"))
            entry = sent.setdefault(k, {"product_id": item.get("product_id"), "name": item["name"],
                                        "note": item.get("note") or "", "qty": ZERO})
            entry["qty"] += Decimal(str(item["qty"]))
    return sent


def _next_kot_no(tenant_id: int, on: date) -> int:
    # Serialise ticket numbering per client (not a tax series, but two
    # counters must still never print the same number).
    db.session.query(Tenant).filter_by(id=tenant_id).with_for_update().one()
    last = (db.session.query(func.max(KitchenTicket.kot_no))
            .filter(KitchenTicket.tenant_id == tenant_id, KitchenTicket.kot_date == on).scalar())
    return (last or 0) + 1


def _diff_items(cart_lines, already_sent: dict) -> list[dict]:
    wanted: dict[str, dict] = {}
    for line in cart_lines:
        if line.charge_kind:
            continue
        k = _key(line.product_id, line.description, line.note)
        entry = wanted.setdefault(k, {"product_id": line.product_id, "name": line.description,
                                      "note": line.note or "", "qty": ZERO, "unit": line.unit})
        entry["qty"] += Decimal(line.qty)
    items = []
    for k, w in wanted.items():
        delta = w["qty"] - already_sent.get(k, {}).get("qty", ZERO)
        if delta != 0:
            items.append({"product_id": w["product_id"], "name": w["name"], "note": w["note"], "qty": str(delta.normalize())})
    for k, s in already_sent.items():
        if k not in wanted and s["qty"] > 0:
            items.append({"product_id": s["product_id"], "name": s["name"], "note": s["note"],
                          "qty": str((-s["qty"]).normalize())})
    return items


def create_ticket(tenant, user, order_type: str, items: list[dict], table_id=None, order_ref=None,
                  status=KotStatus.SENT, pos_bill_id=None) -> KitchenTicket:
    today = date.today()
    kot = KitchenTicket(tenant_id=tenant.id, kot_date=today, kot_no=_next_kot_no(tenant.id, today),
                        order_type=order_type, table_id=table_id, order_ref=(order_ref or "")[:40] or None, items=items,
                        status=status, pos_bill_id=pos_bill_id, created_by_id=user.id)
    db.session.add(kot)
    db.session.flush()
    return kot


def send_kot(tenant, user, table: DiningTable, cart) -> KitchenTicket | None:
    """Save the table's order as it now stands and send the kitchen what
    changed. Returns the new ticket, or None if nothing changed."""
    from app.pos.services import apply_counter_rules, hold_cart

    if not cart.lines:
        raise RestaurantError("Add something to the order first.")
    apply_counter_rules(tenant, user, cart)  # refuses sold-out items, prices from the item master
    already = sent_quantities(open_tickets(tenant.id, table.id))
    items = _diff_items(cart.lines, already)
    for bill in POSBill.query.filter_by(tenant_id=tenant.id, table_id=table.id, status=POSBillStatus.HELD).all():
        db.session.delete(bill)
    db.session.flush()
    cart.order_type = "dine_in"
    cart.table_id = table.id
    hold_cart(tenant, user, cart, f"Table {table.name}")
    if not items:
        return None
    return create_ticket(tenant, user, "dine_in", items, table_id=table.id)


def after_checkout(tenant, user, bill, cart) -> KitchenTicket | None:
    """A billed table: its tickets are billed and its running order gone.
    A takeaway / delivery / app order: one ticket for the whole order."""
    if not tenant.has_feature("restaurant_mode") or not cart.order_type:
        return None
    if cart.table_id:
        for kot in open_tickets(tenant.id, cart.table_id):
            kot.status = KotStatus.BILLED
            kot.pos_bill_id = bill.id
        for held in POSBill.query.filter_by(tenant_id=tenant.id, table_id=cart.table_id, status=POSBillStatus.HELD).all():
            db.session.delete(held)
        return None
    items = [{"product_id": l.product_id, "name": l.description, "note": l.note or "", "qty": str(Decimal(l.qty).normalize())}
             for l in cart.lines if not l.charge_kind]
    if not items:
        return None
    return create_ticket(tenant, user, cart.order_type, items, status=KotStatus.BILLED, pos_bill_id=bill.id)


def move_table(tenant, from_table: DiningTable, to_table: DiningTable) -> None:
    order = running_order(tenant.id, from_table.id)
    if order is None:
        raise RestaurantError(f"Table {from_table.name} has no running order.")
    if running_order(tenant.id, to_table.id) is not None:
        raise RestaurantError(f"Table {to_table.name} already has an order - merge them instead.")
    order.table_id = to_table.id
    order.hold_label = f"Table {to_table.name}"
    for kot in open_tickets(tenant.id, from_table.id):
        kot.table_id = to_table.id


def merge_tables(tenant, user, from_table: DiningTable, into_table: DiningTable) -> None:
    """Both orders become one on `into_table` - lines, tickets and all."""
    from app.pos.services import CartInput, hold_cart

    a = running_order(tenant.id, from_table.id)
    b = running_order(tenant.id, into_table.id)
    if a is None or b is None:
        raise RestaurantError("Both tables need a running order to merge.")
    lines = held_lines(b) + held_lines(a)
    for kot in open_tickets(tenant.id, from_table.id):
        kot.table_id = into_table.id
    customer_id = b.customer_id or a.customer_id
    db.session.delete(a)
    db.session.delete(b)
    db.session.flush()
    hold_cart(tenant, user, CartInput(lines=lines, customer_id=customer_id, order_type="dine_in", table_id=into_table.id,
                                      price_list_id=b.price_list_id),
              f"Table {into_table.name}")


def held_lines(bill: POSBill):
    from app.pos.services import CartLineInput

    return [CartLineInput(description=l.description, hsn_or_sac_code=l.hsn_or_sac_code or "", qty=l.qty, rate=l.rate,
                          gst_rate=l.gst_rate, product_id=l.product_id, unit=l.unit,
                          price_includes_tax=l.price_includes_tax, discount_kind=l.line_discount_kind,
                          discount_value=l.line_discount_value or Decimal("0"),
                          discount_percent=l.line_discount_value if l.line_discount_kind == "percent" else Decimal("0"),
                          note=l.note or "", unit_factor=l.unit_factor or Decimal("1"))
            for l in bill.lines if not l.charge_kind]


def cancel_order(tenant, table: DiningTable, reason: str) -> None:
    if not reason or not reason.strip():
        raise RestaurantError("Give a reason to cancel the order.")
    order = running_order(tenant.id, table.id)
    if order is None:
        raise RestaurantError(f"Table {table.name} has no running order.")
    for kot in open_tickets(tenant.id, table.id):
        kot.status = KotStatus.CANCELLED
    db.session.delete(order)


# --- the ARFA Counter app (counter app, stage 3) ----------------------------------------------

MAX_KOT_ITEMS = 100


def tables_state(tenant) -> list[dict]:
    """Every table as the phone shows it: its running order (lines with
    quantity and price) and what the kitchen has had of it (open tickets),
    keyed like a ticket line (product|name|note)."""
    from app.einvoice.providers.common import IST

    out = []
    for tile in overview(tenant):
        table, order = tile["table"], tile["order"]
        sent = sent_quantities(open_tickets(tenant.id, table.id)) if order or tile["kots"] else {}
        out.append({
            "id": table.id, "name": table.name, "section": table.section or "",
            "busy": order is not None, "kots": tile["kots"],
            "total": float(order.grand_total) if order else 0,
            "since": order.created_at.astimezone(IST).strftime("%H:%M") if order else "",
            "lines": [{"product_id": l.product_id, "name": l.description, "note": l.note or "",
                       "qty": float(l.qty), "rate": float(l.rate)}
                      for l in (order.lines if order else []) if not l.charge_kind],
            "sent": {k: float(v["qty"]) for k, v in sent.items()},
        })
    return out


def apply_kot(tenant, user, payload: dict) -> tuple[KitchenTicket | None, bool]:
    """A KOT from the app: what changed on one table since that phone last
    sent, each item a + or - quantity. The change is added to the table's
    running order as it stands on the server now - so a phone that was
    offline adds its items to what the counter or another phone sent
    meanwhile, never replaces them. A quantity never goes below nothing.

    Returns (ticket, new): the ticket is None when nothing changed; new is
    False when this KOT (its uuid) was booked before - it is never booked
    twice."""
    from app.models.product import Product
    from app.pos.services import CartInput, CartLineInput, hold_cart

    uuid = str(payload.get("uuid") or "")[:36]
    if len(uuid) < 8:
        raise RestaurantError("The KOT has no id.")
    existing = KitchenTicket.query.filter_by(tenant_id=tenant.id, client_uuid=uuid).first()
    if existing is not None:
        return existing, False
    try:
        table_id = int(payload.get("table_id"))
    except (TypeError, ValueError):
        raise RestaurantError("The KOT has no table.")
    table = DiningTable.query.filter_by(id=table_id, tenant_id=tenant.id).first()
    if table is None or not table.is_active:
        raise RestaurantError("That table isn't in use any more - ask the owner, or send the order on another table.")
    raw = payload.get("items")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_KOT_ITEMS:
        raise RestaurantError("The KOT has no items.")
    wanted = []
    for item in raw:
        try:
            pid = int(item.get("product_id"))
            qty = Decimal(str(item.get("qty")))
        except (TypeError, ValueError, AttributeError, ArithmeticError):
            raise RestaurantError("An item on the KOT can't be read.")
        if not qty.is_finite() or qty == 0 or abs(qty) > 10000:
            raise RestaurantError("An item on the KOT has a quantity that can't be right.")
        wanted.append((pid, qty.quantize(Decimal("0.001")), str(item.get("note") or "").strip()[:100]))
    ids = {pid for pid, _, _ in wanted}
    products = {p.id: p for p in Product.query.filter(Product.tenant_id == tenant.id, Product.id.in_(ids))}
    if len(products) != len(ids):
        raise RestaurantError("An item on the KOT isn't on your menu any more.")

    order = running_order(tenant.id, table.id)
    lines = held_lines(order) if order else []
    sent = []
    for pid, qty, note in wanted:
        line = next((l for l in lines if l.product_id == pid and (l.note or "") == note), None)
        if line is None:
            if qty < 0:
                continue  # nothing of it on the table to take off
            p = products[pid]
            line = CartLineInput(description=p.name, hsn_or_sac_code=p.hsn_or_sac_code or "", qty=ZERO,
                                 rate=p.sale_price, gst_rate=p.gst_rate, product_id=p.id, unit=p.unit,
                                 price_includes_tax=p.price_includes_tax, note=note)
            lines.append(line)
        before = Decimal(line.qty)
        after = max(before + qty, ZERO)
        if after == before:
            continue
        line.qty = after
        sent.append({"product_id": pid, "name": line.description, "note": note, "qty": str((after - before).normalize())})
    if not sent:
        return None, True

    keep = [l for l in lines if Decimal(l.qty) > 0]
    customer_id = order.customer_id if order else None
    price_list_id = order.price_list_id if order else None
    for held in POSBill.query.filter_by(tenant_id=tenant.id, table_id=table.id, status=POSBillStatus.HELD).all():
        db.session.delete(held)
    db.session.flush()
    if keep:
        hold_cart(tenant, user, CartInput(lines=keep, customer_id=customer_id, order_type="dine_in", table_id=table.id,
                                          price_list_id=price_list_id), f"Table {table.name}")
    kot = create_ticket(tenant, user, "dine_in", sent, table_id=table.id)
    kot.client_uuid = uuid
    kot.device_ref = str(payload.get("ref") or "").strip()[:20] or None
    if not keep:
        # Everything taken off: the table is free, its tickets cancelled.
        for ticket in open_tickets(tenant.id, table.id):
            ticket.status = KotStatus.CANCELLED
    return kot, True


def settle_offline_bill(tenant, user, bill, payload: dict) -> None:
    """A bill made on a phone without internet, now booked. A table's bill
    takes what it billed off the table's running order; the table is free
    (its tickets billed) once nothing is left on it - anything added
    meanwhile from another counter stays on the table to be billed. A
    takeaway / delivery bill gets its kitchen ticket, as online."""
    from app.pos.services import CartInput, hold_cart

    if not tenant.has_feature("restaurant_mode"):
        return
    billed = {}
    for line in bill.lines:
        if line.product_id:
            billed[line.product_id] = billed.get(line.product_id, ZERO) + Decimal(line.qty)
    if not bill.table_id:
        if bill.order_type:
            items = [{"product_id": l.product_id, "name": l.description, "note": "", "qty": str(Decimal(l.qty).normalize())}
                     for l in bill.lines if not l.charge_kind]
            if items:
                kot = create_ticket(tenant, user, bill.order_type, items, status=KotStatus.BILLED, pos_bill_id=bill.id)
                kot.device_ref = (bill.bill_number or "")[:20] or None
        return
    order = running_order(tenant.id, bill.table_id)
    left = []
    for line in held_lines(order) if order else []:
        take = min(Decimal(line.qty), billed.get(line.product_id, ZERO)) if line.product_id else ZERO
        if take:
            billed[line.product_id] -= take
            line.qty = Decimal(line.qty) - take
        if line.qty > 0:
            left.append(line)
    if order is not None:
        customer_id, price_list_id = order.customer_id, order.price_list_id
        for held in POSBill.query.filter_by(tenant_id=tenant.id, table_id=bill.table_id, status=POSBillStatus.HELD).all():
            db.session.delete(held)
        db.session.flush()
        if left:
            table = db.session.get(DiningTable, bill.table_id)
            hold_cart(tenant, user, CartInput(lines=left, customer_id=customer_id, order_type="dine_in",
                                              table_id=bill.table_id, price_list_id=price_list_id),
                      f"Table {table.name}")
            return
    for ticket in open_tickets(tenant.id, bill.table_id):
        ticket.status = KotStatus.BILLED
        ticket.pos_bill_id = bill.id
