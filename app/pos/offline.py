"""Offline billing (feature "offline_pos").

When the internet drops, the counter keeps billing: the POS screen and
the items are cached on the device (the service worker, pos_sw.js), and
each bill is numbered by the device itself in that device's OWN series -
prefix O1, O2... per counter device, "O1/2026-27/0001" - so the number
printed on the customer's paper bill is final and never collides with the
counter's online series or another device's. (The owner chose this over
a temporary number renumbered later: under Rule 46 the number on the
customer's bill is the one that must be filed.)

Back online, the device sends its queued bills here (sync). Each is
booked exactly once (its client_uuid), under the same row lock as every
other number (numbering.claim_number), and what the device sent is kept
in OfflineBill whatever happens - a bill the server can't book (an item
deleted meanwhile, ...) waits there for the Client Admin, its number still
accounted for, to be retried or cancelled with a reason.

What can be billed offline is deliberately plain: catalog items at the
price the device had, one payment by cash, UPI or card. No discounts,
credit, returns, loyalty, serial / IMEI items, margin-scheme goods,
jewellery or Schedule H drugs - those need the server's checks, so the
counter says so and waits for the connection. Stock is taken off as
sold (the sale already happened), even past zero."""

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.models.invoice_series import DocumentType
from app.models.mixins import utcnow
from app.models.platform import CounterDevice, OfflineBill, OfflineBillStatus
from app.models.pos_bill import PaymentMode, POSBill, POSBillStatus, POSPayment
from app.models.product import Product
from app.utils.financial_year import financial_year_for
from app.utils.gst import compute_invoice_totals, round_money
from app.utils.numbering import claim_number, device_series, format_number

MAX_DEVICES = 99
OFFLINE_MODES = (PaymentMode.CASH, PaymentMode.UPI, PaymentMode.CARD)
MAX_LINES = 200
FY_RE = re.compile(r"^\d{4}-\d{2}$")


class OfflineError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- devices ---------------------------------------------------------------------------------

def register_device(tenant, user, name: str, platform: str = "web") -> tuple[CounterDevice, str]:
    """A new counter device with its own series code. Returns the device
    and its secret (shown to the device once, stored only as a hash)."""
    from app.models.tenant import Tenant

    # Two counters registering at the same moment must not both get O1:
    # the client's row is locked while a code is picked.
    Tenant.query.filter_by(id=tenant.id).with_for_update().one()
    used = {d.code for d in CounterDevice.query.filter_by(tenant_id=tenant.id)}
    code = next((f"O{n}" for n in range(1, MAX_DEVICES + 1) if f"O{n}" not in used), None)
    if code is None:
        raise OfflineError("This client already has the most counter devices allowed - switch an old one off and "
                           "ask support to free its code.")
    token = secrets.token_urlsafe(32)
    device = CounterDevice(tenant_id=tenant.id, user_id=user.id, code=code,
                           name=(name or "").strip()[:60] or f"Counter {code}",
                           platform=platform if platform in ("android", "app") else "web", token_hash=_hash(token),
                           last_seen_at=utcnow())
    db.session.add(device)
    db.session.flush()
    return device, token


def authenticate(tenant, user, device_id, token: str | None) -> CounterDevice:
    try:
        device_id = int(device_id)
    except (TypeError, ValueError):
        raise OfflineError("This device isn't registered for offline billing.")
    device = CounterDevice.query.filter_by(id=device_id, tenant_id=tenant.id).first()
    if device is None or not token or not hmac.compare_digest(device.token_hash, _hash(token)):
        raise OfflineError("This device isn't registered for offline billing.")
    if device.user_id != user.id:
        raise OfflineError("This device bills for another login - sign in as that login to send its bills.")
    if not device.is_active:
        raise OfflineError("This device was switched off for offline billing by the Client Admin.")
    device.last_seen_at = utcnow()
    return device


def device_state(tenant, device, on=None) -> dict:
    """What the device numbers its next offline bill from."""
    from app.pos.services import _active_gstin

    on = on or datetime.now().date()
    fy = financial_year_for(on)
    gstin = _active_gstin(tenant)
    series = device_series(tenant.id, gstin.id, device, DocumentType.POS_BILL, fy)
    return {"device_id": device.id, "code": device.code, "name": device.name, "fy": fy,
            "next_number": series.next_number, "active": device.is_active}


def blocked_products(tenant, products) -> list[int]:
    """Items that can't be billed offline (they need the server's checks)."""
    blocked = []
    for p in products:
        if (p.has_variants or (tenant.has_feature("serial_imei") and p.track_serial)
                or (tenant.has_feature("margin_scheme") and p.is_margin_scheme)
                or (tenant.has_feature("jewellery") and p.is_jewellery)
                or (tenant.has_feature("pharmacy") and p.drug_schedule)):
            blocked.append(p.id)
    return blocked


# --- sync ------------------------------------------------------------------------------------

def _dec(value, label: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise OfflineError(f"{label} isn't a number.")


def _parse_time(value) -> datetime:
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise OfflineError("The bill's date and time can't be read.")
    if when.tzinfo is None:
        raise OfflineError("The bill's time has no time zone.")
    if when > utcnow() + timedelta(minutes=10):
        raise OfflineError("The bill is dated in the future - check the date and time on this device.")
    return when


def _number_of(device, payload) -> tuple[str, int, str]:
    fy = str(payload.get("fy") or "")
    try:
        seq = int(payload.get("seq"))
    except (TypeError, ValueError):
        raise OfflineError("The bill has no number.")
    if not FY_RE.match(fy) or seq < 1:
        raise OfflineError("The bill number can't be read.")
    number = format_number(device.code, fy, seq)
    if payload.get("number") != number:
        raise OfflineError(f"The bill number {payload.get('number')!r} isn't in this device's series.")
    return fy, seq, number


def sync_bill(tenant, user, device, payload: dict) -> tuple[OfflineBill, bool]:
    """Book one queued bill - idempotent on its uuid. Returns the
    OfflineBill record (status SYNCED or FAILED) and whether it is new
    (False = sent before); raises OfflineError only for a payload that
    can't even be recorded (no uuid / number)."""
    uuid = str(payload.get("uuid") or "")[:36]
    if len(uuid) < 8:
        raise OfflineError("The bill has no id.")
    existing = OfflineBill.query.filter_by(tenant_id=tenant.id, client_uuid=uuid).first()
    if existing is not None:
        return existing, False  # sent before - never booked twice
    fy, seq, number = _number_of(device, payload)
    clash = OfflineBill.query.filter_by(tenant_id=tenant.id, bill_number=number).first()
    if clash is not None:
        raise OfflineError(f"Bill {number} was already sent as a different bill - this device's numbering is out of "
                           "step; ask support before billing offline again.")
    # A bill with an unreadable or future time (the device's clock is wrong)
    # is still recorded - its number is on a customer's paper bill - and
    # waits for the Client Admin.
    try:
        billed_at, time_error = _parse_time(payload.get("billed_at")), None
    except OfflineError as exc:
        billed_at, time_error = utcnow(), exc.message
    record = OfflineBill(tenant_id=tenant.id, device_id=device.id, client_uuid=uuid, bill_number=number,
                         billed_at=billed_at, payload=payload, status=OfflineBillStatus.FAILED, error=time_error)
    db.session.add(record)
    db.session.flush()
    if time_error is None:
        _try_book(tenant, user, device, record, fy, seq)
    device.last_synced_at = utcnow()
    return record, True


def retry(tenant, user, record: OfflineBill) -> OfflineBill:
    if record.status != OfflineBillStatus.FAILED:
        raise OfflineError("Only a bill that couldn't be booked can be tried again.")
    device = record.device
    fy, seq, _ = _number_of(device, record.payload)
    record.attempts = (record.attempts or 1) + 1
    _try_book(tenant, user, device, record, fy, seq, booked_by=user)
    return record


def _try_book(tenant, user, device, record, fy, seq, booked_by=None) -> None:
    savepoint = db.session.begin_nested()
    try:
        bill = _book(tenant, device, record, fy, seq)
    except OfflineError as exc:
        savepoint.rollback()
        record.status = OfflineBillStatus.FAILED
        record.error = exc.message
        return
    savepoint.commit()
    record.status = OfflineBillStatus.SYNCED
    record.error = None
    record.pos_bill_id = bill.id
    if booked_by is not None:
        record.resolved_by_id = booked_by.id
        record.resolved_at = utcnow()
        record.resolution_note = "Booked on retry"


def _book(tenant, device, record, fy, seq) -> POSBill:
    from app.inventory import hooks as inventory_hooks
    from app.inventory import ledger as inventory_ledger
    from app.pos.services import (
        CartLineInput,
        POSValidationError,
        _active_gstin,
        _charge_gst,
        _compute_lines,
        _persist_lines,
    )
    from app.pos.sessions import current_session, day_close_on

    payload = record.payload
    online = POSBill.query.filter_by(tenant_id=tenant.id, checkout_uuid=record.client_uuid).first()
    if online is not None and online.status != POSBillStatus.VOIDED:
        raise OfflineError(f"This sale reached the server online as {online.bill_number} just before the connection "
                           "dropped - cancel this offline copy (or void the online bill and try again).")
    raw_lines = payload.get("lines") or []
    if not raw_lines or len(raw_lines) > MAX_LINES:
        raise OfflineError("The bill has no items.")
    try:
        mode = PaymentMode(payload.get("payment_mode"))
    except ValueError:
        mode = None
    if mode not in OFFLINE_MODES:
        raise OfflineError("An offline bill is paid by cash, UPI or card.")

    ids = []
    for raw in raw_lines:
        try:
            ids.append(int(raw.get("product_id")))
        except (TypeError, ValueError):
            raise OfflineError("An item on the bill isn't in the catalog.")
    products = {p.id: p for p in Product.query.filter(Product.tenant_id == tenant.id, Product.id.in_(ids))}
    blocked = set(blocked_products(tenant, products.values()))
    lines = []
    for raw, pid in zip(raw_lines, ids):
        product = products.get(pid)
        if product is None:
            raise OfflineError("An item on the bill is no longer in the catalog - add it back, then try again.")
        if pid in blocked:
            raise OfflineError(f"{product.name} can't be billed offline (it needs a serial number, batch rules or "
                               "a prescription) - cancel this bill and bill it again online.")
        qty = _dec(raw.get("qty"), "A quantity").quantize(Decimal("0.001"))
        rate = _dec(raw.get("rate"), "A price")
        if qty <= 0 or rate < 0:
            raise OfflineError(f"The quantity or price of {product.name} isn't valid.")
        # The price the device billed at is what the customer paid; the
        # tax treatment always comes from the item master.
        lines.append(CartLineInput(description=product.name, hsn_or_sac_code=product.hsn_or_sac_code or "", qty=qty,
                                   rate=round_money(rate), gst_rate=product.gst_rate, product_id=product.id,
                                   unit=product.unit, price_includes_tax=product.price_includes_tax))

    try:
        gstin = _active_gstin(tenant)
    except POSValidationError as exc:
        raise OfflineError(exc.message)
    charge_gst = _charge_gst(tenant)
    computed = _compute_lines(tenant, lines)
    totals = compute_invoice_totals(computed)
    try:
        device_total = round_money(_dec(payload.get("total"), "The total"))
    except OfflineError:
        device_total = None
    record.total_mismatch = device_total is not None and device_total != totals["grand_total"]

    cash_tendered = change_due = None
    if mode == PaymentMode.CASH and payload.get("cash_tendered") not in (None, ""):
        cash_tendered = round_money(_dec(payload.get("cash_tendered"), "Cash received"))
        if cash_tendered < totals["grand_total"]:
            cash_tendered = None
        else:
            change_due = cash_tendered - totals["grand_total"]

    user = device.user
    series = device_series(tenant.id, gstin.id, device, DocumentType.POS_BILL, fy)
    bill_number = claim_number(series, seq)
    if POSBill.query.filter_by(tenant_id=tenant.id, bill_number=bill_number).first():
        raise OfflineError(f"Bill number {bill_number} is already on the books.")
    customer = payload.get("customer") or {}
    snapshot = None
    if (customer.get("name") or "").strip():
        snapshot = {"name": customer["name"].strip()[:100], "phone": (customer.get("phone") or "").strip()[:20]}
    cashier_id = _cashier_id(tenant, user, payload.get("cashier_id"))
    session = current_session(tenant, user) if day_close_on(tenant) else None

    bill = POSBill(
        tenant_id=tenant.id, gstin_id=gstin.id, invoice_series_id=series.id, bill_number=bill_number,
        customer_snapshot=snapshot, status=POSBillStatus.COMPLETED, payment_mode=mode, created_by_id=user.id,
        completed_at=record.billed_at, bill_discount_value=Decimal("0"), cash_tendered=cash_tendered,
        change_due=change_due, cash_session_id=session.id if session else None, device_id=device.id,
        cashier_id=cashier_id,
        # The device printed the original; every print from here is a copy.
        original_printed_at=record.billed_at,
        **totals,
    )
    db.session.add(bill)
    db.session.flush()
    _persist_lines(bill, computed, charge_gst)
    bill.payments.append(POSPayment(tenant_id=tenant.id, mode=mode, amount=totals["grand_total"],
                                    reference=(payload.get("reference") or "")[:100] or None))
    db.session.flush()
    from app.models.pos_bill import POSBillLine

    saved = POSBillLine.query.filter_by(bill_id=bill.id).order_by(POSBillLine.sort_order).all()
    location = inventory_ledger.location_for_user(tenant.id, user)
    # No stock check - the goods have already left the counter.
    try:
        inventory_hooks.post_sale(
            tenant, location.id,
            [inventory_hooks.SaleLine(s.product_id, Decimal(s.qty), s.id, None) for s in saved if s.product_id],
            "pos_bill", bill.id, user.id, pos_bill=bill,
        )
    except inventory_ledger.StockError as exc:
        raise OfflineError(exc.message)
    return bill


def _cashier_id(tenant, user, value):
    from app.models.platform import Cashier

    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    cashier = Cashier.query.filter_by(id=value, tenant_id=tenant.id, branch_user_id=user.id).first()
    return cashier.id if cashier else None


def void_record(tenant, user, record: OfflineBill, reason: str) -> POSBill:
    """A bill the server couldn't book, cancelled by the Client Admin: its
    number goes on the books as a cancelled bill (zero value, the reason
    kept), so the device's series has no gap."""
    reason = (reason or "").strip()
    if record.status != OfflineBillStatus.FAILED:
        raise OfflineError("Only a bill that couldn't be booked can be cancelled here - a booked bill is voided "
                           "from the Day-book like any other.")
    if not reason:
        raise OfflineError("A reason is required to cancel a bill.")
    from app.pos.services import POSValidationError, _active_gstin

    device = record.device
    fy, seq, _ = _number_of(device, record.payload)
    try:
        gstin = _active_gstin(tenant)
    except POSValidationError as exc:
        raise OfflineError(exc.message)
    series = device_series(tenant.id, gstin.id, device, DocumentType.POS_BILL, fy)
    number = claim_number(series, seq)
    if POSBill.query.filter_by(tenant_id=tenant.id, bill_number=number).first():
        raise OfflineError(f"Bill number {number} is already on the books.")
    now = utcnow()
    bill = POSBill(tenant_id=tenant.id, gstin_id=gstin.id, invoice_series_id=series.id, bill_number=number,
                   status=POSBillStatus.VOIDED, created_by_id=device.user_id, completed_at=record.billed_at,
                   voided_at=now, voided_by_id=user.id, void_reason=f"Offline bill not booked: {reason}",
                   device_id=device.id, original_printed_at=record.billed_at)
    db.session.add(bill)
    db.session.flush()
    record.status = OfflineBillStatus.VOIDED
    record.pos_bill_id = bill.id
    record.resolved_by_id = user.id
    record.resolved_at = now
    record.resolution_note = reason[:500]
    return bill
