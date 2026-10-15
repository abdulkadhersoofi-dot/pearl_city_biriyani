"""Gross profit by item / category, and the daily sales summary (feature
"business_reports").

Item gross profit: what each item sold for (excluding GST; a margin-scheme
item at its full selling price) less what the units cost - the
weighted-average cost the stock ledger valued them at when they went out
(sales less sales returns). Lines typed in without a catalogue item, and
services, have no stock cost and are shown as such.

Daily summary (any day or range): counter sales by payment mode net of
refunds paid back the same way, sales by hour, by cashier, bills with a
discount, and every void, return and refund with its reason."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.extensions import db
from app.finance.pnl import NOT_SALES, Places, ZERO, day_start, money, places
from app.models.inventory import MovementType, StockMovement
from app.models.invoice import Invoice, InvoiceLine, InvoiceStatus
from app.models.note import CreditDebitNote, NoteStatus
from app.models.pos_bill import PAYMENT_MODE_LABELS, PaymentMode, POSBill, POSBillStatus
from app.models.pos_return import POSReturn, POSReturnStatus
from app.models.product import Product
from app.models.user import User

UNCATALOGUED = "Items not in the catalogue"


@dataclass
class ItemRow:
    key: object
    name: str
    category: str
    qty: Decimal = ZERO
    sales: Decimal = ZERO
    cost: Decimal = ZERO
    has_cost: bool = True

    @property
    def gross_profit(self) -> Decimal:
        return self.sales - self.cost

    @property
    def gp_pct(self):
        return (self.gross_profit * 100 / self.sales).quantize(Decimal("0.1")) if self.sales else None


def _line_value(line, margin: bool) -> Decimal:
    return Decimal(line.line_total) if margin else Decimal(line.taxable_value)


def item_gross_profit(tenant, start: date, end: date, location_id: int | None = None) -> dict:
    p: Places = places(tenant)
    at_store = location_id is None or location_id == p.default_id
    lo, hi = day_start(start), day_start(end + timedelta(days=1))
    rows: dict = {}
    products = {}

    def row(product_id, description):
        key = product_id or UNCATALOGUED
        if key not in rows:
            if product_id:
                prod = products.get(product_id) or db.session.get(Product, product_id)
                products[product_id] = prod
                category = prod.category.name if prod is not None and prod.category else "Uncategorised"
                rows[key] = ItemRow(key, prod.name if prod else description, category)
            else:
                rows[key] = ItemRow(key, UNCATALOGUED, "-", has_cost=False)
        return rows[key]

    def take(lines, sign, margin, ratio=Decimal("1")):
        for line in lines:
            r = row(line.product_id, line.description)
            r.qty += sign * Decimal(line.qty) * ratio
            r.sales += sign * money(_line_value(line, margin) * ratio)

    if at_store:
        for inv in Invoice.query.filter(Invoice.tenant_id == tenant.id, Invoice.status == InvoiceStatus.ISSUED,
                                        Invoice.document_type.notin_(NOT_SALES), Invoice.invoice_date >= start,
                                        Invoice.invoice_date <= end):
            take(inv.lines, 1, inv.document_type.value == "margin_scheme_invoice")
        for note in CreditDebitNote.query.filter(CreditDebitNote.tenant_id == tenant.id,
                                                 CreditDebitNote.status == NoteStatus.ISSUED,
                                                 CreditDebitNote.note_date >= start, CreditDebitNote.note_date <= end):
            take(note.lines, 1 if note.document_type.value == "debit_note" else -1, False)
    for bill in POSBill.query.filter(POSBill.tenant_id == tenant.id,
                                     POSBill.status.in_([POSBillStatus.COMPLETED, POSBillStatus.REFUNDED]),
                                     POSBill.completed_at >= lo, POSBill.completed_at < hi):
        if location_id is None or p.for_user(bill.created_by_id) == location_id:
            take(bill.lines, 1, bill.is_margin_scheme)
    for bill in POSBill.query.filter(POSBill.tenant_id == tenant.id, POSBill.status == POSBillStatus.REFUNDED,
                                     POSBill.refunded_at >= lo, POSBill.refunded_at < hi):
        if bill.grand_total and (location_id is None or p.for_user(bill.created_by_id) == location_id):
            ratio = min(Decimal(bill.refunded_amount or 0) / Decimal(bill.grand_total), Decimal("1"))
            take(bill.lines, -1, bill.is_margin_scheme, ratio)
    for ret in POSReturn.query.filter(POSReturn.tenant_id == tenant.id, POSReturn.status == POSReturnStatus.COMPLETED,
                                      POSReturn.returned_at >= lo, POSReturn.returned_at < hi):
        if location_id is None or p.for_user(ret.bill.created_by_id if ret.bill else None) == location_id:
            take(ret.lines, -1, bool(ret.bill and ret.bill.is_margin_scheme))

    # Cost: what the stock ledger valued the units at when they went out.
    moves = StockMovement.query.filter(
        StockMovement.tenant_id == tenant.id, StockMovement.moved_at >= lo, StockMovement.moved_at < hi,
        StockMovement.movement_type.in_([MovementType.SALE, MovementType.SALE_RETURN]))
    if location_id is not None:
        moves = moves.filter(StockMovement.location_id == location_id)
    for m in moves:
        r = row(m.product_id, "")
        r.cost += -Decimal(m.qty) * Decimal(m.unit_cost)
    for r in rows.values():
        r.cost = money(r.cost)
        if r.key != UNCATALOGUED:
            prod = products.get(r.key)
            r.has_cost = bool(prod is not None and not prod.is_service and r.cost)

    items = sorted((r for r in rows.values() if r.sales or r.cost or r.qty),
                   key=lambda r: (-r.gross_profit, r.name.lower()))
    by_category: dict = {}
    for r in items:
        c = by_category.setdefault(r.category, ItemRow(r.category, r.category, r.category))
        c.qty += r.qty
        c.sales += r.sales
        c.cost += r.cost
    totals = ItemRow("total", "Total", "")
    for r in items:
        totals.sales += r.sales
        totals.cost += r.cost
    return {"start": start, "end": end, "rows": items,
            "categories": sorted(by_category.values(), key=lambda r: -r.gross_profit), "totals": totals}


# --- daily sales summary ------------------------------------------------------------

def daily_summary(tenant, start: date, end: date) -> dict:
    lo, hi = day_start(start), day_start(end + timedelta(days=1))
    bills = POSBill.query.filter(POSBill.tenant_id == tenant.id,
                                 POSBill.status.in_([POSBillStatus.COMPLETED, POSBillStatus.REFUNDED]),
                                 POSBill.completed_at >= lo, POSBill.completed_at < hi).order_by(POSBill.completed_at).all()
    users = {u.id: u.name for u in User.query.filter_by(tenant_id=tenant.id)}

    modes = defaultdict(lambda: {"sales": ZERO, "refunds": ZERO})
    hours = defaultdict(lambda: {"bills": 0, "total": ZERO})
    cashiers = defaultdict(lambda: {"bills": 0, "total": ZERO, "discount": ZERO, "returns": ZERO, "voids": 0})
    discounted = []
    for bill in bills:
        payments = [(p.mode, Decimal(p.amount)) for p in bill.payments] or (
            [(bill.payment_mode, Decimal(bill.grand_total))] if bill.payment_mode else [])
        for mode, amount in payments:
            modes[mode]["sales"] += amount
        hour = bill.completed_at.astimezone().hour
        hours[hour]["bills"] += 1
        hours[hour]["total"] += Decimal(bill.grand_total)
        c = cashiers[bill.created_by_id]
        c["bills"] += 1
        c["total"] += Decimal(bill.grand_total)
        c["discount"] += Decimal(bill.total_discount)
        if bill.total_discount:
            discounted.append(bill)

    refunds = POSBill.query.filter(POSBill.tenant_id == tenant.id, POSBill.status == POSBillStatus.REFUNDED,
                                   POSBill.refunded_at >= lo, POSBill.refunded_at < hi).all()
    for bill in refunds:
        modes[bill.refund_mode or PaymentMode.CASH]["refunds"] += Decimal(bill.refunded_amount or 0)
        cashiers[bill.refunded_by_id or bill.created_by_id]["returns"] += Decimal(bill.refunded_amount or 0)
    returns = POSReturn.query.filter(POSReturn.tenant_id == tenant.id, POSReturn.returned_at >= lo,
                                     POSReturn.returned_at < hi).order_by(POSReturn.returned_at).all()
    for ret in returns:
        if ret.status != POSReturnStatus.COMPLETED:
            continue
        modes[ret.refund_mode]["refunds"] += Decimal(ret.grand_total)
        cashiers[ret.created_by_id]["returns"] += Decimal(ret.grand_total)
    voids = POSBill.query.filter(POSBill.tenant_id == tenant.id, POSBill.status == POSBillStatus.VOIDED,
                                 POSBill.voided_at >= lo, POSBill.voided_at < hi).order_by(POSBill.voided_at).all()
    for bill in voids:
        cashiers[bill.voided_by_id or bill.created_by_id]["voids"] += 1

    invoices = Invoice.query.filter(Invoice.tenant_id == tenant.id, Invoice.status == InvoiceStatus.ISSUED,
                                    Invoice.document_type.notin_(NOT_SALES), Invoice.invoice_date >= start,
                                    Invoice.invoice_date <= end).all()
    mode_rows = [{"mode": m, "label": PAYMENT_MODE_LABELS.get(m, str(m)), **v, "net": v["sales"] - v["refunds"]}
                 for m, v in sorted(modes.items(), key=lambda kv: kv[0].value)]
    return {
        "start": start, "end": end,
        "bill_count": len(bills), "bill_total": sum((Decimal(b.grand_total) for b in bills), ZERO),
        "invoice_count": len(invoices), "invoice_total": sum((Decimal(i.grand_total) for i in invoices), ZERO),
        "modes": mode_rows,
        "hours": [{"hour": h, **hours[h]} for h in sorted(hours)],
        "cashiers": sorted(({"name": users.get(uid, "-"), **v} for uid, v in cashiers.items()),
                           key=lambda r: -r["total"]),
        "discounted": discounted, "discount_total": sum((Decimal(b.total_discount) for b in discounted), ZERO),
        "voids": voids, "returns": returns, "refunds": refunds, "users": users,
    }
