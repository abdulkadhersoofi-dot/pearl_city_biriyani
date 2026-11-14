"""Fetching purchase orders from SAP and making them ARFA purchase orders.

  pull()    asks SAP for the orders changed since the last fetch, keeps each
            as it came (SapOrder), notes every vendor code and material
            number it has not seen (SapMapping) and maps the ones it can:
            a supplier by GSTIN (when SAP's Business Partner service gives
            it), a material by its number being the ARFA item's SKU.
  apply()   makes or updates the ARFA purchase order of every fetched order
            whose supplier is mapped. An item whose material isn't mapped
            comes in by SAP's own text and waits on the "to map" list;
            nothing is guessed.

An order fetched again is updated, never doubled. ARFA never writes to SAP.
What ARFA has billed against an order is never lost to a change in SAP: a
quantity isn't taken below what is billed, and an order with bills isn't
cancelled from here.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from flask import current_app

from app.einvoice import vault
from app.extensions import db
from app.integrations.sap import client as sap
from app.models.invoice_series import DocumentType
from app.models.mixins import utcnow
from app.models.product import Product
from app.models.purchase import PurchaseOrder, PurchaseOrderLine, PurchaseOrderStatus, Supplier
from app.models.sap import (
    MAP_MATERIAL, MAP_SUPPLIER, ORDER_IMPORTED, ORDER_SKIPPED, ORDER_WAITING, SapCall, SapConnection, SapMapping,
    SapOrder,
)
from app.models.tenant import Gstin
from app.utils.financial_year import financial_year_for
from app.utils.numbering import get_or_create_series

import requests

log = logging.getLogger(__name__)
transport = requests            # what carries the requests to SAP (tests put a stand-in here)
ZERO = Decimal("0")
OVERLAP = timedelta(minutes=2)      # fetched again each time, so a change made mid-fetch isn't missed
# SAP's unit codes -> the GST unit codes ARFA keeps (others pass through).
UNITS = {"EA": "NOS", "ST": "PCS", "PC": "PCS", "PCE": "PCS", "KG": "KGS", "G": "GMS", "L": "LTR", "M": "MTR",
         "TO": "TON", "BT": "BTL", "CS": "BOX", "CAR": "BOX", "PAK": "PAC", "DZ": "DOZ"}


class SyncError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# --- the connection ----------------------------------------------------------------------------------

def connection(tenant) -> SapConnection | None:
    return SapConnection.query.filter_by(tenant_id=tenant.id).first()


def _hosts() -> tuple:
    return tuple(h.strip() for h in str(current_app.config.get("SAP_ALLOWED_HOSTS") or "").split(",") if h.strip())


def settings_of(conn: SapConnection) -> sap.Settings:
    secrets = vault.unseal(conn.secrets) if conn.secrets else {}
    return sap.Settings(
        base_url=conn.base_url, api=conn.api, auth=conn.auth, username=conn.username or "",
        secret=secrets.get("secret", ""), token_url=conn.token_url or "", sap_client=conn.sap_client or "",
        company_code=conn.company_code or "", purchasing_org=conn.purchasing_org or "", plant=conn.plant or "",
        timeout=float(current_app.config.get("SAP_TIMEOUT") or 30), allowed_hosts=_hosts(),
        allow_private=bool(current_app.config.get("SAP_ALLOW_PRIVATE")))


def save_connection(tenant, user, form) -> SapConnection:
    """The connection form. The password box left empty keeps the saved one."""
    conn = connection(tenant) or SapConnection(tenant_id=tenant.id, base_url="")
    get = lambda key: (form.get(key) or "").strip()  # noqa: E731
    try:
        base_url = sap.check_url(get("base_url"), _hosts(), bool(current_app.config.get("SAP_ALLOW_PRIVATE")))
        api = get("api") if get("api") in ("v2", "v4") else "v2"
        auth = get("auth") if get("auth") in ("basic", "oauth") else "basic"
        token_url = ""
        if auth == "oauth":
            token_url = sap.check_url(get("token_url"), (), bool(current_app.config.get("SAP_ALLOW_PRIVATE")),
                                      "token address")
    except sap.SapError as exc:
        raise SyncError(exc.message)
    if not get("username"):
        raise SyncError("Enter the SAP user (or the OAuth client id) ARFA signs in as.")
    secret = form.get("secret") or ""
    if not secret and not conn.secrets:
        raise SyncError("Enter the password (or the OAuth client secret).")
    sap_client = get("sap_client")
    if sap_client and not (sap_client.isdigit() and len(sap_client) == 3):
        raise SyncError("The SAP client is a three-digit number, such as 100.")
    gstin = Gstin.query.filter_by(id=form.get("gstin_id", type=int), tenant_id=tenant.id).first() \
        if hasattr(form, "get") and form.get("gstin_id") else None
    gstin = gstin or tenant.gstins.order_by(Gstin.is_primary.desc(), Gstin.id).first()
    since = None
    if get("since"):
        try:
            since = date.fromisoformat(get("since"))
        except ValueError:
            raise SyncError("\"Orders from\" isn't a date.")
    moved = bool(conn.id) and (conn.base_url != base_url or conn.api != api or conn.company_code != (get("company_code") or None))
    conn.base_url, conn.api, conn.auth, conn.token_url = base_url, api, auth, token_url or None
    conn.username, conn.sap_client = get("username")[:120], sap_client or None
    conn.company_code, conn.purchasing_org, conn.plant = (get("company_code")[:10] or None,
                                                          get("purchasing_org")[:10] or None, get("plant")[:10] or None)
    conn.since, conn.gstin_id = since, gstin.id if gstin else None
    conn.lookup_partners = bool(form.get("lookup_partners"))
    conn.enabled = bool(form.get("enabled"))
    conn.updated_by_id = user.id
    if secret:
        conn.secrets = vault.seal({"secret": secret})
    if moved:
        conn.cursor = None              # another system or company: start from the beginning
    db.session.add(conn)
    return conn


def _log_calls(tenant, user, client: sap.Client) -> None:
    for call in client.calls:
        db.session.add(SapCall(tenant_id=tenant.id, user_id=user.id if user is not None else None,
                               purpose=call.purpose, path=call.path[:300], status=call.status, ms=call.ms,
                               records=call.records, error=(call.error or None) and call.error[:300]))
    client.calls.clear()


def test(tenant, user, http=None) -> dict:
    conn = connection(tenant)
    if conn is None:
        raise SyncError("Save the connection first.")
    try:
        client = sap.Client(settings_of(conn), http=http if http is not None else transport)
    except (sap.SapError, vault.VaultError) as exc:
        raise SyncError(getattr(exc, "message", str(exc)))
    try:
        return client.check()
    except sap.SapError as exc:
        raise SyncError(exc.message)
    finally:
        _log_calls(tenant, user, client)


# --- mapping SAP's codes --------------------------------------------------------------------------------

def _mapping(tenant, kind: str, code: str, name: str = "") -> SapMapping:
    row = SapMapping.query.filter_by(tenant_id=tenant.id, kind=kind, sap_code=code).first()
    if row is None:
        row = SapMapping(tenant_id=tenant.id, kind=kind, sap_code=code, sap_name=name[:255] or None)
        db.session.add(row)
        db.session.flush()
    elif name and not row.sap_name:
        row.sap_name = name[:255]
    return row


def _map_supplier(tenant, row: SapMapping, client: sap.Client | None, lookup: bool) -> None:
    if row.mapped:
        return
    if client is not None and lookup and not row.sap_gstin:
        found = client.supplier(row.sap_code)
        row.sap_name = (found.get("name") or row.sap_name or "")[:255] or None
        row.sap_gstin = (found.get("gstin") or "")[:15] or None
    if row.sap_gstin:
        hit = Supplier.query.filter_by(tenant_id=tenant.id, gstin=row.sap_gstin).order_by(Supplier.id).first()
        if hit is not None:
            row.supplier_id, row.how = hit.id, "gstin"


def _map_material(tenant, row: SapMapping) -> None:
    if row.mapped or not row.sap_code:
        return
    hits = Product.query.filter(Product.tenant_id == tenant.id,
                                db.func.upper(Product.sku).in_({row.sap_code.upper(), row.sap_code.upper().zfill(18)})) \
        .limit(2).all()
    if len(hits) == 1:
        row.product_id, row.how = hits[0].id, "sku"


def set_mapping(tenant, user, mapping_id: int, target_id: int | None) -> SapMapping:
    """A person says what an SAP code is in ARFA (or clears it)."""
    row = SapMapping.query.filter_by(id=mapping_id, tenant_id=tenant.id).first()
    if row is None:
        raise SyncError("That SAP code is no longer on the list.")
    if row.kind == MAP_SUPPLIER:
        target = Supplier.query.filter_by(id=target_id, tenant_id=tenant.id).first() if target_id else None
        row.supplier_id = target.id if target else None
    else:
        target = Product.query.filter_by(id=target_id, tenant_id=tenant.id).first() if target_id else None
        row.product_id = target.id if target else None
    if target_id and target is None:
        raise SyncError("Pick one of your own suppliers or items.")
    row.how, row.mapped_by_id = ("person" if target else None), (user.id if target else None)
    return row


def waiting(tenant) -> dict:
    """What a person still has to map, with how many fetched orders wait on each."""
    rows = SapMapping.query.filter_by(tenant_id=tenant.id).order_by(SapMapping.kind.desc(), SapMapping.sap_code).all()
    held: dict[str, int] = {}
    for order in SapOrder.query.filter_by(tenant_id=tenant.id, status=ORDER_WAITING).all():
        held[order.supplier_code or ""] = held.get(order.supplier_code or "", 0) + 1
    return {"suppliers": [r for r in rows if r.kind == MAP_SUPPLIER and not r.mapped],
            "materials": [r for r in rows if r.kind == MAP_MATERIAL and not r.mapped],
            "mapped": [r for r in rows if r.mapped], "held": held}


# --- fetching ------------------------------------------------------------------------------------------

def pull(tenant, user=None, http=None, max_pages: int = sap.MAX_PAGES) -> dict:
    """One fetch. Returns what happened in numbers; raises SyncError when
    SAP couldn't be read (the reason is kept on the connection too)."""
    conn = connection(tenant)
    if conn is None or not conn.base_url:
        raise SyncError("The SAP link isn't set up yet.")
    counts = {"fetched": 0, "new": 0, "changed": 0}
    client = None
    try:
        client = sap.Client(settings_of(conn), http=http if http is not None else transport)
        since = conn.cursor - OVERLAP if conn.cursor else (
            datetime.combine(conn.since, datetime.min.time(), tzinfo=timezone.utc) if conn.since else None)
        latest = conn.cursor
        seen_suppliers, seen_materials = {}, {}
        for order in client.orders(since, max_pages=max_pages):
            counts["fetched"] += 1
            order_date = date.fromisoformat(order["date"]) if order["date"] else None
            if conn.since and order_date and order_date < conn.since:
                continue
            changed = datetime.fromisoformat(order["changed"]) if order["changed"] else None
            row = SapOrder.query.filter_by(tenant_id=tenant.id, number=order["number"]).first()
            if row is None:
                row = SapOrder(tenant_id=tenant.id, number=order["number"][:20], status=ORDER_WAITING, payload=order)
                db.session.add(row)
                counts["new"] += 1
            elif row.payload != order:
                counts["changed"] += 1
            row.payload, row.supplier_code, row.order_date = order, order["supplier"][:40], order_date
            row.changed_at, row.deleted = changed, bool(order["deleted"])
            if changed and (latest is None or changed > latest):
                latest = changed
            if order["supplier"]:
                seen_suppliers.setdefault(order["supplier"], order.get("supplier_name") or "")
            for item in order["items"]:
                if item["material"]:
                    seen_materials.setdefault(item["material"], item["text"])
        for code, name in seen_suppliers.items():
            _map_supplier(tenant, _mapping(tenant, MAP_SUPPLIER, code, name), client, conn.lookup_partners)
        for code, name in seen_materials.items():
            _map_material(tenant, _mapping(tenant, MAP_MATERIAL, code, name))
        conn.cursor = latest
        db.session.flush()
        counts.update(apply(tenant, user))
        conn.last_pull_ok, conn.last_pull_message = True, None
    except (sap.SapError, vault.VaultError) as exc:
        db.session.rollback()
        conn = connection(tenant)
        message = getattr(exc, "message", None) or "The saved SAP login can't be read - enter the password again."
        conn.last_pull_ok, conn.last_pull_message = False, message[:500]
        conn.last_pull_at = utcnow()
        if client is not None:
            _log_calls(tenant, user, client)
        db.session.commit()
        log.warning("sap pull failed for tenant %s: %s", tenant.id, message)
        raise SyncError(message)
    conn.last_pull_at, conn.last_pull_counts = utcnow(), counts
    _log_calls(tenant, user, client)
    return counts


# --- making the orders ------------------------------------------------------------------------------------

def _unit(sap_unit: str, product) -> str:
    if product is not None and product.unit:
        return product.unit
    return UNITS.get(sap_unit, sap_unit or "NOS")[:20]


def _actor(tenant, user):
    """Orders the schedule brings in are entered in the owner's name."""
    if user is not None:
        return user
    from app.models.user import User, UserRole

    return User.query.filter_by(tenant_id=tenant.id, role=UserRole.CLIENT_ADMIN).order_by(User.id).first()


def apply(tenant, user=None) -> dict:
    """Every fetched order whose supplier is mapped becomes (or updates) its
    ARFA purchase order."""
    from app.purchases import matching

    conn = connection(tenant)
    actor = _actor(tenant, user)
    suppliers = {m.sap_code: m.supplier_id for m in SapMapping.query.filter_by(tenant_id=tenant.id, kind=MAP_SUPPLIER)
                 if m.supplier_id}
    materials = {m.sap_code: m.product for m in SapMapping.query.filter_by(tenant_id=tenant.id, kind=MAP_MATERIAL)
                 if m.product_id}
    counts = {"imported": 0, "updated": 0, "waiting": 0, "cancelled": 0}
    gstin_id = (conn.gstin_id if conn is not None else None) or \
        getattr(tenant.gstins.order_by(Gstin.is_primary.desc(), Gstin.id).first(), "id", None)
    for row in SapOrder.query.filter_by(tenant_id=tenant.id).order_by(SapOrder.id).all():
        payload = row.payload or {}
        items = [i for i in payload.get("items") or [] if not i.get("deleted") and Decimal(i.get("qty") or "0") > 0]
        order = row.purchase_order
        gone = row.deleted or not items
        if order is None:
            if gone:
                row.status, row.note = ORDER_SKIPPED, "Deleted in SAP." if row.deleted else "No items to order."
                continue
            supplier_id = suppliers.get(row.supplier_code or "")
            if not supplier_id or actor is None or gstin_id is None:
                row.status = ORDER_WAITING
                row.note = f"Waiting for SAP supplier {row.supplier_code} to be matched to one of yours."
                counts["waiting"] += 1
                continue
            on = row.order_date or date.today()
            series = get_or_create_series(tenant.id, gstin_id, DocumentType.PURCHASE_ORDER, financial_year_for(on))
            if PurchaseOrder.query.filter_by(tenant_id=tenant.id, number=row.number).first() is not None:
                row.status, row.note = ORDER_WAITING, f"An order numbered {row.number} already exists in ARFA."
                counts["waiting"] += 1
                continue
            order = PurchaseOrder(tenant_id=tenant.id, gstin_id=gstin_id, invoice_series_id=series.id,
                                  number=row.number, supplier_id=supplier_id, order_date=on, source="sap",
                                  external_ref=row.number, created_by_id=actor.id, estimated_total=ZERO)
            db.session.add(order)
            db.session.flush()
            row.purchase_order_id = order.id
            counts["imported"] += 1
        else:
            counts["updated"] += 1
        if gone:
            # Deleted in SAP: cancelled here too, unless bills already stand against it.
            if order.status == PurchaseOrderStatus.OPEN:
                order.status, order.cancel_reason = PurchaseOrderStatus.CANCELLED, "Deleted in SAP."
                counts["cancelled"] += 1
            elif order.status == PurchaseOrderStatus.PARTLY:
                order.status, order.cancel_reason = PurchaseOrderStatus.CONVERTED, "Closed short: deleted in SAP."
            row.status, row.note = ORDER_IMPORTED, "Deleted in SAP."
            order.external_synced_at = utcnow()
            continue
        if order.status == PurchaseOrderStatus.CANCELLED and order.cancel_reason == "Deleted in SAP.":
            order.status, order.cancel_reason = PurchaseOrderStatus.OPEN, None      # put back in SAP
        supplier_id = suppliers.get(row.supplier_code or "")
        if supplier_id and not order.bills:
            order.supplier_id = supplier_id
        if row.order_date:
            order.order_date = row.order_date
        mine = {l.external_ref: l for l in order.lines if l.external_ref}
        unmapped = 0
        for item in items:
            product = materials.get(item["material"]) if item["material"] else None
            if item["material"] and product is None:
                unmapped += 1
            line = mine.pop(item["item"], None)
            if line is None:
                line = PurchaseOrderLine(order_id=order.id, external_ref=item["item"][:10], description="", qty=ZERO)
                db.session.add(line)
                order.lines.append(line)
            qty = Decimal(item["qty"])
            line.external_code = item["material"][:40] or None
            line.product_id = product.id if product is not None else None
            line.description = ((product.name if product is not None else item["text"]) or item["material"]
                                or f"Item {item['item']}")[:255]
            line.hsn_or_sac_code = (product.hsn_or_sac_code if product is not None else None) or line.hsn_or_sac_code
            line.qty = max(qty, Decimal(line.billed_qty or 0))
            line.unit = _unit(item["unit"], product)
            line.rate = Decimal(item["rate"])
            line.gst_rate = product.gst_rate if product is not None else (line.gst_rate or ZERO)
        for line in mine.values():          # an item taken off the order in SAP
            if Decimal(line.billed_qty or 0) > 0:
                line.qty = line.billed_qty
            else:
                order.lines.remove(line)
        order.estimated_total = sum((Decimal(l.qty) * Decimal(l.rate) * (1 + Decimal(l.gst_rate or 0) / 100)
                                     for l in order.lines), ZERO).quantize(Decimal("0.01"))
        order.external_synced_at = utcnow()
        db.session.flush()
        closed_short = order.status == PurchaseOrderStatus.CONVERTED and \
            (order.cancel_reason or "").startswith("Closed short")
        if not closed_short:                # closed here by a person: a change in SAP doesn't reopen it
            matching.refresh_status(order)
        row.status = ORDER_IMPORTED
        row.note = f"{unmapped} item(s) wait to be matched to yours." if unmapped else None
    return counts


def pull_all(max_pages: int = sap.MAX_PAGES) -> list[tuple[int, str]]:
    """The schedule: every business with the link switched on. One
    business's failure doesn't stop the next."""
    from app.models.tenant import Tenant

    out = []
    for conn in SapConnection.query.filter_by(enabled=True).order_by(SapConnection.id).all():
        tenant = db.session.get(Tenant, conn.tenant_id)
        if tenant is None or not tenant.has_feature("sap_link") or not tenant.has_feature("purchases"):
            continue
        try:
            counts = pull(tenant, None, max_pages=max_pages)
            db.session.commit()
            out.append((tenant.id, f"ok {counts}"))
        except SyncError as exc:
            out.append((tenant.id, f"failed: {exc.message}"))
        except Exception:  # noqa: BLE001 - the next business still gets its turn
            db.session.rollback()
            log.exception("sap pull crashed for tenant %s", tenant.id)
            out.append((tenant.id, "failed: error"))
    return out
