from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.models.customer import Customer
from app.models.invoice_series import DocumentType
from app.models.mixins import utcnow
from app.models.pos_bill import (
    DiscountKind,
    PaymentMode,
    POSBill,
    POSBillLine,
    POSBillStatus,
    POSPayment,
)
from app.models.pos_return import POSReturn, POSReturnStatus
from app.models.price_list import PriceList
from app.models.product import Product
from app.models.tenant import RegistrationType, Tenant
from app.utils.financial_year import financial_year_for
from app.utils.form_arrays import posted_flag
from app.utils.gst import (
    allocate_amount,
    compute_invoice_totals,
    compute_line,
    line_gross,
    price_basis_amount,
    round_money,
)
from app.utils.numbering import allocate_number, get_or_create_series
from app.utils.permissions import max_discount_percent, user_can

_MAX_DESCRIPTION_LENGTH = 255
_MAX_HSN_LENGTH = 8
_MAX_UNIT_LENGTH = 20
_MAX_REFERENCE_LENGTH = 100
QTY_PLACES = Decimal("0.001")
ZERO = Decimal("0")


class POSValidationError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class CartLineInput:
    description: str
    hsn_or_sac_code: str
    qty: Decimal
    rate: Decimal
    gst_rate: Decimal
    product_id: int | None = None
    unit: str = "pcs"
    discount_percent: Decimal = Decimal("0")
    price_includes_tax: bool = False
    # The line's own discount as the cashier entered it. discount_kind None
    # with discount_percent set is the older "% only" shape, still accepted.
    discount_kind: str | None = None
    discount_value: Decimal = Decimal("0")
    # Filled in by apply_counter_rules: rupees off on the line's price basis
    # (its own rupee discount + its share of the bill discount).
    discount_flat: Decimal = Decimal("0")


@dataclass
class CartInput:
    lines: list[CartLineInput] = field(default_factory=list)
    customer_id: int | None = None
    bill_discount_kind: str | None = None
    bill_discount_value: Decimal = Decimal("0")
    price_list_id: int | None = None


@dataclass
class PaymentInput:
    mode: PaymentMode
    amount: Decimal | None = None  # None = "the whole bill" (single mode)
    reference: str | None = None
    # EXCHANGE only: the sales-return number whose credit pays for this.
    return_number: str | None = None


def _to_decimal(value: str, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, AttributeError):
        raise POSValidationError(f"'{field_name}' must be a number.")
    if not result.is_finite():
        raise POSValidationError(f"'{field_name}' must be a number.")
    return result


def _bounded(value: str, field_name: str, max_length: int) -> str:
    if len(value) > max_length:
        raise POSValidationError(f"'{field_name}' can be at most {max_length} characters.")
    return value


def _discount_kind(value) -> str | None:
    value = (value or "").strip().lower()
    if not value:
        return None
    if value not in (DiscountKind.PERCENT.value, DiscountKind.AMOUNT.value):
        raise POSValidationError("Pick % or Rs. for a discount.")
    return value


def _at(seq, i, default=""):
    return seq[i] if i < len(seq) and seq[i] is not None else default


def parse_cart_lines(
    descriptions, hsns, qtys, rates, gst_rates, product_ids, units, incls=(), discounts=(),
    discount_kinds=(), discount_values=(),
) -> list[CartLineInput]:
    lines = []
    for i, description in enumerate(descriptions):
        description = (description or "").strip()
        if not description:
            continue
        hsn = _at(hsns, i).strip()
        unit = (_at(units, i) or "pcs").strip()
        kind = _discount_kind(_at(discount_kinds, i))
        value = _to_decimal(_at(discount_values, i) or "0", "discount") if kind else Decimal("0")
        percent = value if kind == DiscountKind.PERCENT.value else Decimal("0")
        if kind is None and _at(discounts, i):
            percent = _to_decimal(_at(discounts, i), "discount")
            if percent:
                kind, value = DiscountKind.PERCENT.value, percent
        product_id = _at(product_ids, i)
        try:
            product_id = int(product_id) if product_id else None
        except ValueError:
            raise POSValidationError("An item in the cart isn't in your catalog - remove it and add it again.")
        lines.append(
            CartLineInput(
                description=_bounded(description, "Item name", _MAX_DESCRIPTION_LENGTH),
                hsn_or_sac_code=_bounded(hsn, "HSN/SAC code", _MAX_HSN_LENGTH),
                qty=_to_decimal(_at(qtys, i) or "0", "quantity"),
                rate=_to_decimal(_at(rates, i) or "0", "rate"),
                gst_rate=_to_decimal(_at(gst_rates, i) or "0", "GST rate"),
                product_id=product_id,
                unit=_bounded(unit, "Unit", _MAX_UNIT_LENGTH),
                discount_percent=percent,
                price_includes_tax=posted_flag(incls, i),
                discount_kind=kind,
                discount_value=value,
            )
        )
    if not lines:
        raise POSValidationError("Cart is empty.")
    return lines


def parse_bill_discount(kind, value) -> tuple[str | None, Decimal]:
    kind = _discount_kind(kind)
    if not kind:
        return None, Decimal("0")
    amount = _to_decimal(value or "0", "bill discount")
    if amount == 0:
        return None, Decimal("0")
    return kind, amount


def _charge_gst(tenant: Tenant) -> bool:
    # Retail POS never asks the cashier about place of supply or GST
    # jargon - a store sale is always intra-state (CGST+SGST), and a
    # composition/unregistered client never charges GST at all, same as
    # a Bill of Supply in the Invoicing module.
    return tenant.registration_type == RegistrationType.REGULAR


def resolve_price_list(tenant: Tenant, user, requested_id: int | None) -> PriceList | None:
    """Which price list this sale bills from. Lists only exist for a client
    with "multi_price_list" on; a counter uses its assigned list, and only
    a login that may change prices can switch to another one."""
    if not tenant.has_feature("multi_price_list"):
        return None
    default_id = getattr(user, "price_list_id", None) if getattr(user, "is_staff", False) else None
    list_id = requested_id or default_id
    if not list_id:
        return None
    if requested_id and requested_id != default_id and not user_can(user, "edit_price"):
        raise POSValidationError("This login bills from its own price list only.")
    price_list = PriceList.query.filter_by(id=list_id, tenant_id=tenant.id, is_active=True).first()
    if price_list is None:
        if not requested_id:
            return None  # the counter's own list was switched off - normal prices
        raise POSValidationError("That price list isn't available any more - reload the page.")
    return price_list


def counter_price(product: Product, price_list: PriceList | None, list_prices: dict | None = None) -> Decimal:
    if price_list is not None:
        prices = list_prices if list_prices is not None else price_list.price_map()
        if product.id in prices:
            return prices[product.id]
    return product.sale_price


def apply_counter_rules(tenant: Tenant, user, lines_or_cart) -> PriceList | None:
    """The server's word on every cart line, whatever the terminal sent.

    A catalog item's tax treatment always comes from the item master (a
    cashier can't turn an inclusive menu price exclusive), and its price
    too - the item's price, or its price on the counter's price list -
    unless this login may change prices. Item ids must belong to this
    client. Discounts (a line's own % or rupees, and a bill discount
    spread over the lines) are capped at this login's limit, and turned
    into what the tax is computed on: discount_percent + discount_flat.
    """
    cart = lines_or_cart if isinstance(lines_or_cart, CartInput) else CartInput(lines=lines_or_cart)
    lines = cart.lines
    price_list = resolve_price_list(tenant, user, cart.price_list_id)
    list_prices = price_list.price_map() if price_list else None

    product_ids = {line.product_id for line in lines if line.product_id}
    products = {}
    if product_ids:
        products = {
            p.id: p
            for p in Product.query.filter(Product.id.in_(product_ids), Product.tenant_id == tenant.id).all()
        }
    may_edit_price = user_can(user, "edit_price")
    discount_cap = max_discount_percent(user)
    charge_gst = _charge_gst(tenant)

    for line in lines:
        if line.product_id:
            product = products.get(line.product_id)
            if product is None:
                raise POSValidationError("An item in the cart isn't in your catalog - remove it and add it again.")
            line.price_includes_tax = product.price_includes_tax
            if not may_edit_price:
                line.rate = counter_price(product, price_list, list_prices)
        line.qty = line.qty.quantize(QTY_PLACES)
        if line.qty <= 0:
            raise POSValidationError(f"Quantity for {line.description} must be more than 0.")
        if line.rate < 0:
            raise POSValidationError(f"The price of {line.description} can't be negative.")

        line.discount_flat = Decimal("0")
        if line.discount_kind == DiscountKind.AMOUNT.value:
            line.discount_percent = Decimal("0")
            if line.discount_value < 0:
                raise POSValidationError("A discount can't be negative.")
            line.discount_flat = price_basis_amount(line.discount_value, line.gst_rate, line.price_includes_tax, charge_gst)
        elif line.discount_kind == DiscountKind.PERCENT.value:
            line.discount_percent = line.discount_value
        if line.discount_percent < 0 or line.discount_percent > 100:
            raise POSValidationError("A discount must be between 0% and 100%.")
        gross = line_gross(line.qty, line.rate)
        if line.discount_flat > gross - round_money(gross * line.discount_percent / 100):
            raise POSValidationError(f"The discount on {line.description} is more than its price.")

    # Bill discount: a rupee amount (or % of the bill) spread over the
    # lines by what each line costs the customer, then put on each line's
    # own price basis - tax is always worked out line by line.
    if cart.bill_discount_kind:
        if cart.bill_discount_value < 0:
            raise POSValidationError("A discount can't be negative.")
        payables = [_payable(line, charge_gst) for line in lines]
        bill_total = sum(payables, Decimal("0"))
        if cart.bill_discount_kind == DiscountKind.PERCENT.value:
            if cart.bill_discount_value > 100:
                raise POSValidationError("A discount must be between 0% and 100%.")
            bill_amount = round_money(bill_total * cart.bill_discount_value / 100)
        else:
            bill_amount = round_money(cart.bill_discount_value)
        if bill_amount > bill_total:
            raise POSValidationError("The bill discount is more than the bill.")
        for line, part in zip(lines, allocate_amount(payables, bill_amount)):
            line.discount_flat += price_basis_amount(part, line.gst_rate, line.price_includes_tax, charge_gst)

    for line in lines:
        gross = line_gross(line.qty, line.rate)
        given = round_money(gross * line.discount_percent / 100) + line.discount_flat
        if given <= 0:
            continue
        if discount_cap == 0:
            raise POSValidationError("This login isn't allowed to give discounts.")
        # A paisa of slack for a bill discount's rounding over the lines.
        if given > round_money(gross * discount_cap / 100) + Decimal("0.01"):
            raise POSValidationError(f"This login can give at most {discount_cap.normalize():f}% discount.")
    return price_list


def _payable(line: CartLineInput, charge_gst: bool) -> Decimal:
    return compute_line(
        qty=line.qty,
        rate=line.rate,
        discount_percent=line.discount_percent,
        gst_rate=line.gst_rate,
        is_intra_state=True,
        charge_gst=charge_gst,
        price_includes_tax=line.price_includes_tax,
        discount_flat=line.discount_flat,
    )["line_total"]


def _compute_lines(tenant: Tenant, lines: list[CartLineInput]) -> list[dict]:
    charge_gst = _charge_gst(tenant)
    computed = []
    for line in lines:
        result = compute_line(
            qty=line.qty,
            rate=line.rate,
            discount_percent=line.discount_percent,
            gst_rate=line.gst_rate,
            is_intra_state=True,
            charge_gst=charge_gst,
            price_includes_tax=line.price_includes_tax,
            discount_flat=line.discount_flat,
        )
        computed.append({**result, "input": line})
    return computed


def _persist_lines(bill: POSBill, computed_lines: list[dict], charge_gst: bool) -> None:
    for order, item in enumerate(computed_lines):
        line_in = item["input"]
        db.session.add(
            POSBillLine(
                bill_id=bill.id,
                product_id=line_in.product_id,
                description=line_in.description,
                hsn_or_sac_code=line_in.hsn_or_sac_code,
                qty=line_in.qty,
                unit=line_in.unit,
                rate=line_in.rate,
                discount_percent=line_in.discount_percent,
                discount_flat=line_in.discount_flat,
                line_discount_kind=line_in.discount_kind,
                line_discount_value=line_in.discount_value,
                discount_amount=item["discount_amount"],
                taxable_value=item["taxable_value"],
                gst_rate=line_in.gst_rate if charge_gst else Decimal("0"),
                price_includes_tax=line_in.price_includes_tax,
                cgst_amount=item["cgst_amount"],
                sgst_amount=item["sgst_amount"],
                igst_amount=item["igst_amount"],
                line_total=item["line_total"],
                sort_order=order,
            )
        )


def _active_gstin(tenant: Tenant):
    gstin = tenant.gstins.filter_by(is_active=True).first()
    if not gstin:
        raise POSValidationError(
            "This client has no business location on file. Ask the firm to add one from Clients -> this client."
        )
    return gstin


def _customer(tenant: Tenant, customer_id: int | None) -> Customer | None:
    if not customer_id:
        return None
    customer = Customer.query.filter_by(id=customer_id, tenant_id=tenant.id).first()
    if not customer:
        raise POSValidationError("Select a valid customer.")
    return customer


def customer_snapshot(customer: Customer | None) -> dict | None:
    if customer is None:
        return None
    return {
        "name": customer.name,
        "gstin": customer.gstin,
        "phone": customer.phone,
        "state_code": customer.state_code,
        "state_name": customer.state_name,
    }


def hold_cart(tenant: Tenant, created_by, cart: CartInput, label: str | None) -> POSBill:
    gstin = _active_gstin(tenant)
    customer = _customer(tenant, cart.customer_id)
    price_list = apply_counter_rules(tenant, created_by, cart)
    computed_lines = _compute_lines(tenant, cart.lines)
    totals = compute_invoice_totals(computed_lines)

    bill = POSBill(
        tenant_id=tenant.id,
        gstin_id=gstin.id,
        customer_id=customer.id if customer else None,
        status=POSBillStatus.HELD,
        hold_label=(label or "").strip()[:100] or None,
        created_by_id=created_by.id,
        bill_discount_kind=cart.bill_discount_kind,
        bill_discount_value=cart.bill_discount_value if cart.bill_discount_kind else Decimal("0"),
        price_list_id=price_list.id if price_list else None,
        **totals,
    )
    db.session.add(bill)
    db.session.flush()
    _persist_lines(bill, computed_lines, _charge_gst(tenant))
    return bill


def discard_held_bill(bill: POSBill) -> None:
    if bill.status != POSBillStatus.HELD:
        raise POSValidationError("Only a held bill can be discarded.")
    db.session.delete(bill)


def enabled_payment_modes(tenant: Tenant) -> list[PaymentMode]:
    """The modes this client's counters offer, in order. Credit only while
    the credit-sales module is on."""
    modes = []
    for value in tenant.pos_payment_modes or ["cash", "upi", "card"]:
        try:
            mode = PaymentMode(value)
        except ValueError:
            continue
        if mode in (PaymentMode.EXCHANGE, PaymentMode.MIXED):
            continue
        if mode == PaymentMode.CREDIT and not tenant.has_feature("credit_sales"):
            continue
        if mode not in modes:
            modes.append(mode)
    return modes or [PaymentMode.CASH]


def _settle_payments(
    tenant: Tenant, payments: list[PaymentInput], grand_total: Decimal, customer: Customer | None
) -> list[tuple[PaymentInput, Decimal, POSReturn | None]]:
    if not payments:
        raise POSValidationError("Select a payment mode.")
    allowed = enabled_payment_modes(tenant)
    settled = []
    for p in payments:
        if p.mode == PaymentMode.MIXED or (p.mode != PaymentMode.EXCHANGE and p.mode not in allowed):
            raise POSValidationError("That payment mode isn't switched on for this counter.")
        if p.mode == PaymentMode.CREDIT and customer is None:
            raise POSValidationError("A credit (pay later) sale needs a customer - add one to the bill first.")
        amount = grand_total if (p.amount is None and len(payments) == 1) else p.amount
        if amount is None or amount <= 0:
            raise POSValidationError("Each payment needs an amount more than 0.")
        amount = round_money(amount)
        reference = (p.reference or "").strip()[:_MAX_REFERENCE_LENGTH] or None
        exchange_return = None
        if p.mode == PaymentMode.EXCHANGE:
            exchange_return = (
                POSReturn.query.filter_by(tenant_id=tenant.id, return_number=(p.return_number or "").strip())
                .with_for_update()
                .first()
            )
            if exchange_return is None or exchange_return.refund_mode != PaymentMode.EXCHANGE:
                raise POSValidationError("That return slip number isn't an exchange credit.")
            if exchange_return.status != POSReturnStatus.COMPLETED:
                raise POSValidationError("That return was voided - its credit can't be used.")
            if amount > exchange_return.exchange_balance:
                raise POSValidationError(
                    f"Only Rs. {exchange_return.exchange_balance:.2f} of credit is left on {exchange_return.return_number}."
                )
            reference = exchange_return.return_number
        settled.append((PaymentInput(p.mode, amount, reference, p.return_number), amount, exchange_return))
    paid = sum((amount for _, amount, _ in settled), Decimal("0"))
    if paid != grand_total:
        raise POSValidationError(f"Payments add up to Rs. {paid:.2f} but the bill is Rs. {grand_total:.2f}.")
    return settled


def checkout(
    tenant: Tenant,
    created_by,
    cart: CartInput,
    payment,
    cash_tendered: Decimal | None = None,
    cash_session=None,
) -> POSBill:
    """Completes a sale: numbers the bill and records how it was paid.

    `payment` is a PaymentMode (the whole bill, one way - the original
    3-tap flow) or a list of PaymentInput (split). `cash_session` is the
    counter's open day when day open/close is on (app.pos.sessions)."""
    gstin = _active_gstin(tenant)
    customer = _customer(tenant, cart.customer_id)
    price_list = apply_counter_rules(tenant, created_by, cart)
    charge_gst = _charge_gst(tenant)
    computed_lines = _compute_lines(tenant, cart.lines)
    totals = compute_invoice_totals(computed_lines)

    payments = [PaymentInput(payment)] if isinstance(payment, PaymentMode) else list(payment)
    settled = _settle_payments(tenant, payments, totals["grand_total"], customer)

    cash_paid = sum((amount for p, amount, _ in settled if p.mode == PaymentMode.CASH), Decimal("0"))
    change_due = None
    if cash_tendered is not None:
        cash_tendered = round_money(cash_tendered)
        if cash_paid == 0:
            cash_tendered = None
        elif cash_tendered < cash_paid:
            raise POSValidationError(f"Cash received is less than the Rs. {cash_paid:.2f} due in cash.")
        else:
            change_due = cash_tendered - cash_paid

    series = get_or_create_series(tenant.id, gstin.id, DocumentType.POS_BILL, financial_year_for(date.today()))
    bill_number = allocate_number(series)
    modes = {p.mode for p, _, _ in settled}

    bill = POSBill(
        tenant_id=tenant.id,
        gstin_id=gstin.id,
        invoice_series_id=series.id,
        bill_number=bill_number,
        customer_id=customer.id if customer else None,
        customer_snapshot=customer_snapshot(customer),
        status=POSBillStatus.COMPLETED,
        payment_mode=settled[0][0].mode if len(modes) == 1 else PaymentMode.MIXED,
        created_by_id=created_by.id,
        completed_at=utcnow(),
        bill_discount_kind=cart.bill_discount_kind,
        bill_discount_value=cart.bill_discount_value if cart.bill_discount_kind else Decimal("0"),
        price_list_id=price_list.id if price_list else None,
        cash_session_id=cash_session.id if cash_session is not None else None,
        cash_tendered=cash_tendered,
        change_due=change_due,
        **totals,
    )
    db.session.add(bill)
    db.session.flush()
    _persist_lines(bill, computed_lines, charge_gst)
    for p, amount, exchange_return in settled:
        bill.payments.append(
            POSPayment(
                tenant_id=tenant.id,
                mode=p.mode,
                amount=amount,
                reference=p.reference,
                pos_return_id=exchange_return.id if exchange_return else None,
            )
        )
        if exchange_return is not None:
            exchange_return.exchange_used = (exchange_return.exchange_used or 0) + amount
    db.session.flush()
    return bill


def refund_bill(
    bill: POSBill, refunded_by, amount: Decimal, reason: str, refund_mode: PaymentMode | None = None, cash_session=None
) -> None:
    """The original amount-only refund - kept as it was for old bills and
    quick goodwill refunds; goods coming back go through a sales return
    (app.pos.returns) instead."""
    if bill.status != POSBillStatus.COMPLETED:
        raise POSValidationError("Only a completed bill can be refunded.")
    if bill.returns and any(r.status == POSReturnStatus.COMPLETED for r in bill.returns):
        raise POSValidationError("Items on this bill were already returned - use Return items for the rest.")
    if not reason or not reason.strip():
        raise POSValidationError("A reason is required to refund a bill.")
    if amount <= 0 or amount > bill.grand_total:
        raise POSValidationError(f"Refund amount must be between 0 and Rs. {bill.grand_total}.")

    bill.status = POSBillStatus.REFUNDED
    bill.refunded_amount = amount
    bill.refund_reason = reason.strip()
    bill.refunded_at = utcnow()
    bill.refunded_by_id = refunded_by.id
    bill.refund_mode = refund_mode or (bill.payment_mode if bill.payment_mode != PaymentMode.MIXED else PaymentMode.CASH)
    bill.refund_session_id = cash_session.id if cash_session is not None else None


def void_bill(bill: POSBill, voided_by, reason: str, today: date | None = None) -> None:
    """Cancels a bill billed by mistake - same day only (or, with day
    open/close on, only while its counter day is still open), and only if
    nothing has been returned against it. The number stays used and the
    bill stays on record as cancelled."""
    if bill.status != POSBillStatus.COMPLETED:
        raise POSValidationError("Only a completed bill can be voided.")
    if not reason or not reason.strip():
        raise POSValidationError("A reason is required to void a bill.")
    if any(r.status == POSReturnStatus.COMPLETED for r in bill.returns):
        raise POSValidationError("Items on this bill were already returned - it can't be voided.")
    if bill.cash_session_id:
        from app.models.cash_session import CashSession

        session = db.session.get(CashSession, bill.cash_session_id)
        if session is not None and not session.is_open:
            raise POSValidationError("That day is closed - return the items instead of voiding the bill.")
    else:
        today = today or date.today()
        billed_on = bill.completed_at.astimezone().date() if bill.completed_at else None
        if billed_on != today:
            raise POSValidationError("Only today's bills can be voided - return the items instead.")

    for payment in bill.payments:
        if payment.mode == PaymentMode.EXCHANGE and payment.pos_return is not None:
            payment.pos_return.exchange_used = (payment.pos_return.exchange_used or 0) - payment.amount
    bill.status = POSBillStatus.VOIDED
    bill.void_reason = reason.strip()
    bill.voided_at = utcnow()
    bill.voided_by_id = voided_by.id


def _day_bounds(on_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(on_date, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def z_report(tenant_id: int, on_date: date) -> dict:
    start, end = _day_bounds(on_date)
    bills = (
        POSBill.query.filter(
            POSBill.tenant_id == tenant_id,
            POSBill.status.in_([POSBillStatus.COMPLETED, POSBillStatus.REFUNDED, POSBillStatus.VOIDED]),
            POSBill.completed_at >= start,
            POSBill.completed_at < end,
        )
        .order_by(POSBill.completed_at)
        .all()
    )
    returns = (
        POSReturn.query.filter(
            POSReturn.tenant_id == tenant_id,
            POSReturn.status == POSReturnStatus.COMPLETED,
            POSReturn.returned_at >= start,
            POSReturn.returned_at < end,
        )
        .order_by(POSReturn.returned_at)
        .all()
    )
    summary = summarize_pos_activity([b for b in bills if b.status != POSBillStatus.VOIDED], returns)
    return {
        "date": on_date,
        "bills": bills,
        "returns": returns,
        "voided_count": sum(1 for b in bills if b.status == POSBillStatus.VOIDED),
        **summary,
    }


def summarize_pos_activity(bills, returns, refunded_bills=None) -> dict:
    """Totals for a Z-report: sales by payment mode - read from the bill's
    actual payment rows, so a split bill counts under each of its modes -
    and returns by how they were paid back.

    By date (refunded_bills=None), the same rules as before split payments
    existed: a refunded bill among `bills` counts under refunds, not
    sales. For one counter day (app.pos.sessions), every bill of the day
    is a sale and `refunded_bills` are the refunds paid out that day,
    whichever day those bills were from."""
    by_mode = {mode: {"count": 0, "total": Decimal("0")} for mode in PaymentMode if mode != PaymentMode.MIXED}
    refunds_by_mode = {mode: Decimal("0") for mode in PaymentMode if mode != PaymentMode.MIXED}
    total_sales = Decimal("0")
    total_tax = Decimal("0")
    total_refunds = Decimal("0")
    bill_count = 0
    by_date = refunded_bills is None
    for bill in refunded_bills if not by_date else [b for b in bills if b.status == POSBillStatus.REFUNDED]:
        total_refunds += bill.refunded_amount or Decimal("0")
        refunds_by_mode[bill.refund_mode or PaymentMode.CASH] += bill.refunded_amount or Decimal("0")
    for bill in bills:
        if by_date and bill.status == POSBillStatus.REFUNDED:
            continue
        bill_count += 1
        total_sales += bill.grand_total
        total_tax += bill.total_cgst + bill.total_sgst + bill.total_igst
        if bill.payments:
            for payment in bill.payments:
                by_mode[payment.mode]["count"] += 1
                by_mode[payment.mode]["total"] += payment.amount
        elif bill.payment_mode and bill.payment_mode in by_mode:
            by_mode[bill.payment_mode]["count"] += 1
            by_mode[bill.payment_mode]["total"] += bill.grand_total

    total_returns = Decimal("0")
    for ret in returns:
        total_returns += ret.grand_total
        refunds_by_mode[ret.refund_mode] += ret.grand_total

    return {
        "bill_count": bill_count,
        "total_sales": total_sales,
        "total_tax": total_tax,
        "total_refunds": total_refunds,
        "total_returns": total_returns,
        "net_sales": total_sales - total_refunds - total_returns,
        "by_mode": by_mode,
        "refunds_by_mode": refunds_by_mode,
    }
