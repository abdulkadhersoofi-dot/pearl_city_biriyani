"""Receipts, refund vouchers and supplier payments: creating them, setting
them against bills, cheque status, voiding. Balances themselves are never
stored - see app.accounts.receivables / payables.

Every voucher is numbered from the row-locked allocator (receipt RCT,
advance receipt voucher RV, refund voucher RFV, payment voucher PAY),
never deleted - voided with a reason - and the routes audit-log each
step. GST on an advance comes from app.utils.advance_gst, nowhere else."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from app.accounts import payables, receivables
from app.extensions import db
from app.models.accounts import (
    ChequeStatus,
    CustomerReceipt,
    MoneyMode,
    PaymentAllocation,
    ReceiptAllocation,
    RefundVoucher,
    SupplierPayment,
    VoucherStatus,
)
from app.models.customer import Customer
from app.models.invoice_series import DocumentType
from app.models.mixins import utcnow
from app.models.purchase import Supplier
from app.models.tenant import Gstin, RegistrationType
from app.utils.advance_gst import advance_portion, compute_advance_gst
from app.utils.financial_year import financial_year_for
from app.utils.numbering import allocate_number, get_or_create_series

ZERO = Decimal("0.00")
PAISA = Decimal("0.01")


class AccountsError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def money(value, field_name="Amount") -> Decimal:
    try:
        amount = Decimal(str(value).replace(",", "").strip() or "0")
    except (InvalidOperation, TypeError):
        raise AccountsError(f"{field_name} must be a number.")
    if amount != amount.quantize(PAISA):
        raise AccountsError(f"{field_name} can have at most 2 decimal places.")
    return amount


@dataclass
class Allocation:
    kind: str  # "opening" | "invoice" | "pos_bill" | "purchase_bill" | "expense"
    item_id: int | None
    amount: Decimal


@dataclass
class VoucherInput:
    party_id: int
    voucher_date: date
    amount: Decimal
    mode: MoneyMode
    reference: str | None = None
    cheque_no: str | None = None
    cheque_date: date | None = None
    bank_name: str | None = None
    notes: str | None = None
    allocations: list[Allocation] = field(default_factory=list)
    gstin_id: int | None = None
    # Advance receipt voucher only
    is_advance: bool = False
    for_services: bool = False
    gst_rate: Decimal = ZERO
    place_of_supply_state_code: str | None = None


def default_gstin(tenant, gstin_id: int | None = None) -> Gstin:
    query = Gstin.query.filter_by(tenant_id=tenant.id, is_active=True)
    gstin = query.filter_by(id=gstin_id).first() if gstin_id else None
    gstin = gstin or query.order_by(Gstin.is_primary.desc(), Gstin.id).first()
    if gstin is None:
        raise AccountsError("Add a GSTIN / place of business in Settings first.")
    return gstin


def charges_gst_on_advances(tenant, gstin: Gstin) -> bool:
    """Only a regular registration with a GSTIN pays GST on an advance for
    services; composition / unregistered businesses issue the voucher
    without tax."""
    return tenant.registration_type == RegistrationType.REGULAR and bool(gstin.gstin)


def _number(tenant, gstin, doc_type, on: date):
    series = get_or_create_series(tenant.id, gstin.id, doc_type, financial_year_for(on))
    return series, allocate_number(series)


def _cheque_fields(data: VoucherInput) -> dict:
    if data.mode == MoneyMode.CHEQUE:
        if not (data.cheque_no or "").strip():
            raise AccountsError("Enter the cheque number.")
        return {"cheque_no": data.cheque_no.strip()[:30], "cheque_date": data.cheque_date or data.voucher_date,
                "bank_name": (data.bank_name or "").strip()[:100] or None, "cheque_status": ChequeStatus.PENDING}
    return {"cheque_no": None, "cheque_date": None, "bank_name": None, "cheque_status": None}


def _check_amount(amount: Decimal):
    if amount <= 0:
        raise AccountsError("The amount must be more than 0.")
    if amount != amount.quantize(PAISA):
        raise AccountsError("The amount can have at most 2 decimal places.")


# --- receipts from customers ------------------------------------------------------

def _customer(tenant, customer_id) -> Customer:
    customer = Customer.query.filter_by(id=customer_id, tenant_id=tenant.id).first() if customer_id else None
    if customer is None:
        raise AccountsError("Pick a customer.")
    return customer


def receipt_used(receipt: CustomerReceipt) -> Decimal:
    """How much of a receipt is already set against bills or refunded."""
    allocated = sum((Decimal(a.amount) for a in receipt.allocations), ZERO)
    refunded = sum((Decimal(r.amount) for r in receipt.refunds if r.status == VoucherStatus.POSTED), ZERO)
    return allocated + refunded


def receipt_unused(receipt: CustomerReceipt) -> Decimal:
    return Decimal(receipt.amount) - receipt_used(receipt)


def _apply_receipt_allocations(tenant, receipt: CustomerReceipt, allocations: list[Allocation], on: date):
    allocations = [a for a in allocations if a.amount]
    if not allocations:
        return
    if receipt.status != VoucherStatus.POSTED or receipt.cheque_status == ChequeStatus.BOUNCED:
        raise AccountsError("This receipt can't be set against bills.")
    if on < receipt.voucher_date:
        raise AccountsError("A receipt can't be set against a bill before the date it was received.")
    account = receivables.customer_account(tenant, receipt.customer, with_entries=False)
    outstanding = {(i.kind, i.id or None): i.outstanding for i in account.items}
    total = ZERO
    for a in allocations:
        _check_amount(a.amount)
        if a.kind not in ("opening", "invoice", "pos_bill"):
            raise AccountsError("Unknown bill.")
        key = (a.kind, a.item_id if a.kind != "opening" else None)
        left = outstanding.get(key)
        if left is None:
            raise AccountsError("That bill isn't open on this customer's account.")
        if a.amount > left:
            raise AccountsError(f"Only Rs. {left:.2f} is outstanding on that bill.")
        outstanding[key] = left - a.amount
        total += a.amount
    if total > receipt_unused(receipt):
        raise AccountsError(f"Only Rs. {receipt_unused(receipt):.2f} of this receipt is left to set against bills.")
    for a in allocations:
        receipt.allocations.append(ReceiptAllocation(
            invoice_id=a.item_id if a.kind == "invoice" else None,
            pos_bill_id=a.item_id if a.kind == "pos_bill" else None,
            is_opening=a.kind == "opening", amount=a.amount, allocated_on=on,
        ))
    db.session.flush()


def create_receipt(tenant, user, data: VoucherInput) -> CustomerReceipt:
    customer = _customer(tenant, data.party_id)
    amount = Decimal(data.amount)
    _check_amount(amount)
    gstin = default_gstin(tenant, data.gstin_id)
    tax = {"taxable_value": ZERO, "cgst_amount": ZERO, "sgst_amount": ZERO, "igst_amount": ZERO}
    rate, pos, for_services = ZERO, None, False
    if data.is_advance:
        if data.allocations:
            raise AccountsError("An advance is set against the invoice once it is issued, not now.")
        pos = data.place_of_supply_state_code or customer.state_code or gstin.state_code
        for_services = bool(data.for_services) and charges_gst_on_advances(tenant, gstin)
        rate = Decimal(data.gst_rate or 0) if for_services else ZERO
        if for_services and rate <= 0:
            raise AccountsError("Pick the GST rate of the service the advance is for.")
        computed = compute_advance_gst(amount, rate, pos == gstin.state_code, for_services)
        tax = {k: computed[k] for k in tax}
    doc_type = DocumentType.RECEIPT_VOUCHER if data.is_advance else DocumentType.PAYMENT_RECEIPT
    series, number = _number(tenant, gstin, doc_type, data.voucher_date)
    receipt = CustomerReceipt(
        tenant_id=tenant.id, gstin_id=gstin.id, invoice_series_id=series.id, number=number, customer_id=customer.id,
        voucher_date=data.voucher_date, amount=amount, mode=data.mode, reference=(data.reference or "").strip()[:100] or None,
        notes=(data.notes or "").strip() or None, status=VoucherStatus.POSTED, created_by_id=user.id,
        is_advance=bool(data.is_advance), for_services=for_services, place_of_supply_state_code=pos, gst_rate=rate,
        **tax, **_cheque_fields(data),
    )
    db.session.add(receipt)
    db.session.flush()
    _apply_receipt_allocations(tenant, receipt, data.allocations, data.voucher_date)
    return receipt


def allocate_receipt(tenant, receipt: CustomerReceipt, allocations: list[Allocation], on: date) -> None:
    _apply_receipt_allocations(tenant, receipt, allocations, on)


def remove_receipt_allocation(receipt: CustomerReceipt, allocation_id: int) -> ReceiptAllocation:
    alloc = next((a for a in receipt.allocations if a.id == allocation_id), None)
    if alloc is None:
        raise AccountsError("That allocation isn't on this receipt.")
    receipt.allocations.remove(alloc)
    db.session.flush()
    return alloc


def release_allocations(tenant_id: int, invoice_id: int | None = None, pos_bill_id: int | None = None) -> int:
    """A voided invoice / bill owes nothing - what had been received against
    it goes back on account (to be set against another bill or refunded)."""
    query = ReceiptAllocation.query.join(CustomerReceipt).filter(CustomerReceipt.tenant_id == tenant_id)
    if invoice_id:
        query = query.filter(ReceiptAllocation.invoice_id == invoice_id)
    elif pos_bill_id:
        query = query.filter(ReceiptAllocation.pos_bill_id == pos_bill_id)
    else:
        return 0
    rows = query.all()
    for row in rows:
        row.receipt.allocations.remove(row)
    db.session.flush()
    return len(rows)


def _set_cheque_status(voucher, status: ChequeStatus, on: date):
    if voucher.mode != MoneyMode.CHEQUE:
        raise AccountsError("This wasn't paid by cheque.")
    if voucher.status != VoucherStatus.POSTED:
        raise AccountsError("This voucher is void.")
    if voucher.cheque_status != ChequeStatus.PENDING:
        raise AccountsError(f"The cheque is already marked {voucher.cheque_status.value}.")
    if status not in (ChequeStatus.CLEARED, ChequeStatus.BOUNCED):
        raise AccountsError("Mark the cheque cleared or bounced.")
    voucher.cheque_status = status
    voucher.cleared_on = on


def set_receipt_cheque_status(receipt: CustomerReceipt, status: ChequeStatus, on: date) -> None:
    if status == ChequeStatus.BOUNCED and any(r.status == VoucherStatus.POSTED for r in receipt.refunds):
        raise AccountsError("Void the refund made from this receipt first.")
    _set_cheque_status(receipt, status, on)


def void_receipt(receipt: CustomerReceipt, user, reason: str) -> None:
    if receipt.status == VoucherStatus.VOIDED:
        raise AccountsError("This receipt is already void.")
    if not (reason or "").strip():
        raise AccountsError("A reason is required to void a receipt.")
    if any(r.status == VoucherStatus.POSTED for r in receipt.refunds):
        raise AccountsError("Void the refund voucher made from this receipt first.")
    receipt.status = VoucherStatus.VOIDED
    receipt.void_reason = reason.strip()
    receipt.voided_at = utcnow()
    receipt.voided_by_id = user.id


def advance_usage(receipt: CustomerReceipt) -> list[dict]:
    """How an advance was used up, in order - each adjustment against an
    invoice and each refund, with the taxable value and GST in that part
    (GSTR-1 table 11B). Cumulative, so the parts add up to exactly the tax
    paid on the advance."""
    events = [(a.allocated_on, 0, a.id, "adjusted", a, Decimal(a.amount)) for a in receipt.allocations]
    events += [(r.voucher_date, 1, r.id, "refunded", r, Decimal(r.amount)) for r in receipt.refunds
               if r.status == VoucherStatus.POSTED]
    events.sort(key=lambda e: e[:3])
    receipt_tax = {"taxable_value": Decimal(receipt.taxable_value), "cgst_amount": Decimal(receipt.cgst_amount),
                   "sgst_amount": Decimal(receipt.sgst_amount), "igst_amount": Decimal(receipt.igst_amount)}
    receipt_tax["tax"] = receipt_tax["cgst_amount"] + receipt_tax["sgst_amount"] + receipt_tax["igst_amount"]
    is_intra = receipt.place_of_supply_state_code == receipt.gstin.state_code
    used, out = ZERO, []
    for on, _, _, kind, obj, amount in events:
        part = advance_portion(receipt.amount, receipt_tax, used, amount, receipt.gst_rate, is_intra, receipt.for_services)
        used += amount
        out.append({"date": on, "kind": kind, "object": obj, **part})
    return out


# --- refund vouchers ---------------------------------------------------------------

def create_refund(tenant, user, receipt: CustomerReceipt, amount: Decimal, on: date, mode: MoneyMode,
                  reference: str | None, reason: str) -> RefundVoucher:
    if receipt.status != VoucherStatus.POSTED or receipt.cheque_status == ChequeStatus.BOUNCED:
        raise AccountsError("Nothing can be refunded from this receipt.")
    if receipt.mode == MoneyMode.CHEQUE and receipt.cheque_status != ChequeStatus.CLEARED:
        raise AccountsError("Wait for the cheque to clear before refunding it.")
    amount = Decimal(amount)
    _check_amount(amount)
    if not (reason or "").strip():
        raise AccountsError("Say why the money is being given back.")
    left = receipt_unused(receipt)
    if amount > left:
        raise AccountsError(f"Only Rs. {left:.2f} of this receipt hasn't been used against a bill.")
    if on < receipt.voucher_date:
        raise AccountsError("A refund can't be dated before the receipt.")
    tax = {"taxable_value": ZERO, "cgst_amount": ZERO, "sgst_amount": ZERO, "igst_amount": ZERO}
    if receipt.is_advance:
        receipt_tax = {k: Decimal(getattr(receipt, k)) for k in tax}
        receipt_tax["tax"] = receipt_tax["cgst_amount"] + receipt_tax["sgst_amount"] + receipt_tax["igst_amount"]
        part = advance_portion(receipt.amount, receipt_tax, receipt_used(receipt), amount, receipt.gst_rate,
                               receipt.place_of_supply_state_code == receipt.gstin.state_code, receipt.for_services)
        tax = {k: part[k] for k in tax}
    gstin = db.session.get(Gstin, receipt.gstin_id)
    series, number = _number(tenant, gstin, DocumentType.REFUND_VOUCHER, on)
    refund = RefundVoucher(
        tenant_id=tenant.id, gstin_id=gstin.id, invoice_series_id=series.id, number=number, receipt_id=receipt.id,
        customer_id=receipt.customer_id, voucher_date=on, amount=amount, mode=mode,
        reference=(reference or "").strip()[:100] or None, reason=reason.strip(), status=VoucherStatus.POSTED,
        created_by_id=user.id, **tax,
    )
    db.session.add(refund)
    receipt.refunds.append(refund)
    db.session.flush()
    return refund


def void_refund(refund: RefundVoucher, user, reason: str) -> None:
    if refund.status == VoucherStatus.VOIDED:
        raise AccountsError("This refund voucher is already void.")
    if not (reason or "").strip():
        raise AccountsError("A reason is required to void a refund voucher.")
    refund.status = VoucherStatus.VOIDED
    refund.void_reason = reason.strip()
    refund.voided_at = utcnow()
    refund.voided_by_id = user.id


# --- payments to suppliers ---------------------------------------------------------

def _supplier(tenant, supplier_id) -> Supplier:
    supplier = Supplier.query.filter_by(id=supplier_id, tenant_id=tenant.id).first() if supplier_id else None
    if supplier is None:
        raise AccountsError("Pick a supplier.")
    return supplier


def payment_unused(payment: SupplierPayment) -> Decimal:
    return Decimal(payment.amount) - sum((Decimal(a.amount) for a in payment.allocations), ZERO)


def _apply_payment_allocations(tenant, payment: SupplierPayment, allocations: list[Allocation], on: date):
    allocations = [a for a in allocations if a.amount]
    if not allocations:
        return
    if payment.status != VoucherStatus.POSTED or payment.cheque_status == ChequeStatus.BOUNCED:
        raise AccountsError("This payment can't be set against bills.")
    account = payables.supplier_account(tenant, payment.supplier, with_entries=False)
    outstanding = {(i.kind, i.id or None): i.outstanding for i in account.items}
    total = ZERO
    for a in allocations:
        _check_amount(a.amount)
        if a.kind not in ("opening", "purchase_bill", "expense"):
            raise AccountsError("Unknown bill.")
        key = (a.kind, a.item_id if a.kind != "opening" else None)
        left = outstanding.get(key)
        if left is None:
            raise AccountsError("That bill isn't open on this supplier's account.")
        if a.amount > left:
            raise AccountsError(f"Only Rs. {left:.2f} is outstanding on that bill.")
        outstanding[key] = left - a.amount
        total += a.amount
    if total > payment_unused(payment):
        raise AccountsError(f"Only Rs. {payment_unused(payment):.2f} of this payment is left to set against bills.")
    for a in allocations:
        payment.allocations.append(PaymentAllocation(
            purchase_bill_id=a.item_id if a.kind == "purchase_bill" else None,
            expense_id=a.item_id if a.kind == "expense" else None, is_opening=a.kind == "opening", amount=a.amount,
        ))
    db.session.flush()


def create_payment(tenant, user, data: VoucherInput) -> SupplierPayment:
    supplier = _supplier(tenant, data.party_id)
    amount = Decimal(data.amount)
    _check_amount(amount)
    gstin = default_gstin(tenant, data.gstin_id)
    series, number = _number(tenant, gstin, DocumentType.SUPPLIER_PAYMENT, data.voucher_date)
    payment = SupplierPayment(
        tenant_id=tenant.id, gstin_id=gstin.id, invoice_series_id=series.id, number=number, supplier_id=supplier.id,
        voucher_date=data.voucher_date, amount=amount, mode=data.mode,
        reference=(data.reference or "").strip()[:100] or None, notes=(data.notes or "").strip() or None,
        status=VoucherStatus.POSTED, created_by_id=user.id, **_cheque_fields(data),
    )
    db.session.add(payment)
    db.session.flush()
    _apply_payment_allocations(tenant, payment, data.allocations, data.voucher_date)
    return payment


def allocate_payment(tenant, payment: SupplierPayment, allocations: list[Allocation], on: date) -> None:
    _apply_payment_allocations(tenant, payment, allocations, on)


def remove_payment_allocation(payment: SupplierPayment, allocation_id: int) -> PaymentAllocation:
    alloc = next((a for a in payment.allocations if a.id == allocation_id), None)
    if alloc is None:
        raise AccountsError("That allocation isn't on this payment.")
    payment.allocations.remove(alloc)
    db.session.flush()
    return alloc


def release_payment_allocations(tenant_id: int, purchase_bill_id: int | None = None, expense_id: int | None = None) -> int:
    query = PaymentAllocation.query.join(SupplierPayment).filter(SupplierPayment.tenant_id == tenant_id)
    if purchase_bill_id:
        query = query.filter(PaymentAllocation.purchase_bill_id == purchase_bill_id)
    elif expense_id:
        query = query.filter(PaymentAllocation.expense_id == expense_id)
    else:
        return 0
    rows = query.all()
    for row in rows:
        row.payment.allocations.remove(row)
    db.session.flush()
    return len(rows)


def set_payment_cheque_status(payment: SupplierPayment, status: ChequeStatus, on: date) -> None:
    _set_cheque_status(payment, status, on)


def void_payment(payment: SupplierPayment, user, reason: str) -> None:
    if payment.status == VoucherStatus.VOIDED:
        raise AccountsError("This payment is already void.")
    if not (reason or "").strip():
        raise AccountsError("A reason is required to void a payment.")
    payment.status = VoucherStatus.VOIDED
    payment.void_reason = reason.strip()
    payment.voided_at = utcnow()
    payment.voided_by_id = user.id
