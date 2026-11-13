"""New sale: one form, and ARFA works out the paperwork.

The person selling says who is buying and what. Where each item is decides
what is made:

    every item is here, in stock          an invoice now - stock leaves now
    every item is at a warehouse          a sales order to that warehouse; it
                                          scans the serials, the invoice follows
    some here, some at a warehouse        both: an invoice for what is here and
                                          an order for the rest - one sale
    items in two warehouses               one order a warehouse
    not in stock anywhere                 an order that waits for stock
    "only a price for now"                a quotation or proforma - nothing moves
    "send goods without a bill"           a delivery challan
    sold on EMI                           one order, never split (the EMI rules)

"Here" is where the seller's own stock is: a branch login's showroom, or for
the owner the place picked in "Selling from" (the main store by default).

    plan = decide(tenant, user, here, lines, ...)     nothing saved; says in
                                                       words what will happen
    result = save(tenant, user, header, plan)          makes the documents, in
                                                       the caller's transaction

No new kind of document: save() calls invoicing.services.create_invoice and
sales.services.create_sales_document, so numbering, GST, stock, holds and the
warehouse flow are exactly what they are from the older forms. The parts of
one sale share a `sale_ref`.

Each line can be pinned by hand: "here", or a warehouse. A serial-numbered
item billed here needs its serial numbers (one a unit); without them in
hand, pin the line to a warehouse and it is picked there.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field, replace
from decimal import Decimal

from app.extensions import db
from app.inventory import ledger
from app.invoicing.services import InvoiceInput, LineInput, create_invoice
from app.models.inventory import StockLocation
from app.models.invoice_series import DocumentType
from app.models.product import Product
from app.models.sales import ChallanPurpose, SalesDocumentKind
from app.sales import services as sales_services
from app.warehouse import dispatch

ZERO = Decimal("0")
AUTO, HERE = "auto", "here"
SALE, QUOTE, PROFORMA, CHALLAN = "sale", "quote", "proforma", "challan"
MODES = (SALE, QUOTE, PROFORMA, CHALLAN)
INVOICE, ORDER, WAITING = "invoice", "order", "waiting"


class SellError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class SaleLine:
    line: LineInput
    source: str = AUTO            # "auto", "here", or a warehouse's id


@dataclass
class Part:
    kind: str                     # invoice | order | waiting | quote | proforma | challan
    lines: list[LineInput]
    location: StockLocation | None = None      # where it leaves from


@dataclass
class LineNote:
    """What the form shows beside a line."""
    here: Decimal | None = None                # free here (None: not a stocked item)
    elsewhere: list[tuple[str, Decimal]] = field(default_factory=list)     # (warehouse, free there)
    here_qty: Decimal = ZERO                   # billed now
    order_qty: Decimal = ZERO                  # ordered from a warehouse
    waiting_qty: Decimal = ZERO                # waits for stock
    needs_serials: int = 0                     # serial numbers still to scan to bill it here


@dataclass
class Plan:
    here: StockLocation | None
    mode: str
    parts: list[Part]
    notes: list[LineNote]
    problems: list[str] = field(default_factory=list)
    finance: bool = False

    @property
    def sentence(self) -> str:
        return _sentence(self)

    @property
    def button(self) -> str:
        return _button(self)


# --- where "here" is ----------------------------------------------------------------------------------

def seller_location(tenant, user, chosen_id=None) -> StockLocation | None:
    """Where this person sells from. A branch login: its own place, always.
    The owner: the place chosen on the form, else the main store. None when
    the business keeps no stock in ARFA."""
    if not tenant.has_feature("inventory"):
        return None
    if getattr(user, "is_staff", False):
        return ledger.location_for_user(tenant.id, user)
    if chosen_id:
        try:
            found = StockLocation.query.filter_by(id=int(chosen_id), tenant_id=tenant.id, is_active=True).first()
        except (TypeError, ValueError):
            found = None
        if found is not None:
            return found
    return ledger.default_location(tenant.id)


def selling_places(tenant) -> list[StockLocation]:
    if not tenant.has_feature("inventory"):
        return []
    return StockLocation.query.filter_by(tenant_id=tenant.id, is_active=True).order_by(StockLocation.name).all()


def _needs_stock(tenant, product: Product | None) -> bool:
    """Must actually be in stock to be sold (the rule warehouse holds use)."""
    return (tenant.has_feature("inventory") and ledger.sells_from_stock(product)
            and (product.track_serial or not product.allow_negative_stock))


def _needs_serials(tenant, product: Product | None) -> bool:
    return bool(product is not None and product.track_serial and tenant.has_feature("serial_imei")
                and tenant.has_feature("inventory"))


def can_order(tenant, user) -> bool:
    """May this login make sales orders? The owner with Quotes & orders on; a
    branch login when the warehouses pick orders."""
    if not tenant.has_feature("sales_documents"):
        return False
    return dispatch.is_on(tenant) if getattr(user, "is_staff", False) else True


# --- deciding -----------------------------------------------------------------------------------------

def decide(tenant, user, here: StockLocation | None, lines: list[SaleLine], *, mode: str = SALE,
           together: bool = False, finance: bool = False, pincode: str | None = None) -> Plan:
    """What saving these lines would make. Saves nothing."""
    if mode not in MODES:
        mode = SALE
    if not lines:
        raise SellError("Add at least one item.")
    notes = [LineNote() for _ in lines]
    if mode != SALE:
        return Plan(here, mode, [Part(mode, [l.line for l in lines])], notes)

    ids = {l.line.product_id for l in lines if l.line.product_id}
    products = {p.id: p for p in Product.query.filter(Product.tenant_id == tenant.id, Product.id.in_(ids))} if ids else {}
    orders_ok = can_order(tenant, user)
    dispatch_on = dispatch.is_on(tenant) and orders_ok
    stocked = [pid for pid in ids if _needs_stock(tenant, products.get(pid))]

    # What is free here, and at each warehouse that isn't "here".
    free_here: dict[int, Decimal] = {}
    if here is not None and stocked:
        on_hand = ledger.stock_on_hand(tenant.id, stocked, here.id)
        holds = dispatch.held(tenant.id, stocked, location_id=here.id)
        free_here = {pid: on_hand.get(pid, ZERO) - holds.get((pid, here.id), ZERO) for pid in stocked}
    warehouses = [r for r in dispatch.free_by_warehouse(tenant.id, stocked)
                  if here is None or r["location"].id != here.id] if dispatch_on else []
    by_id = {str(r["location"].id): r["location"] for r in warehouses}

    invoice_lines: list[LineInput] = []
    pinned: dict[str, list[LineInput]] = {}          # warehouse id -> its lines
    auto_order: list[tuple[int, LineInput]] = []     # (form row, line) for ARFA to place
    loose: list[LineInput] = []                      # services, free text: go with whatever is made
    problems: list[str] = []
    left_here = dict(free_here)

    for i, sale_line in enumerate(lines):
        line, pid = sale_line.line, sale_line.line.product_id
        product = products.get(pid)
        if line.qty <= 0:
            raise SellError(f"Quantity for '{line.description}' must be more than 0.")
        if not _needs_stock(tenant, product):
            loose.append(line)
            continue
        note = notes[i]
        note.here = free_here.get(pid, ZERO)
        note.elsewhere = [(r["location"].name, r["items"][pid]["free"]) for r in warehouses
                          if r["items"][pid]["free"] > 0]
        source = sale_line.source or AUTO
        if (finance or together) and orders_ok:
            auto_order.append((i, line))
            note.order_qty = line.qty
            continue
        if source in by_id:
            pinned.setdefault(source, []).append(line)
            note.order_qty = line.qty
            continue
        if source == HERE or not orders_ok:
            take = line.qty
        else:
            take = min(line.qty, max(left_here.get(pid, ZERO), ZERO))
            if product.track_serial:
                take = Decimal(int(take))              # whole units only
        if take > 0:
            left_here[pid] = left_here.get(pid, ZERO) - take
            given = [s for s in line.serials if s]
            if _needs_serials(tenant, product):
                missing = int(take) - len(given)
                if missing > 0:
                    note.needs_serials = missing
                    problems.append(f"{line.description}: scan {missing} serial number"
                                    f"{'s' if missing != 1 else ''} to bill it now - or order it from a warehouse.")
                given = given[:int(take)]
            invoice_lines.append(replace(line, qty=take, serials=given))
            note.here_qty = take
        if line.qty - take > 0:
            rest = replace(line, qty=line.qty - take, serials=[])
            auto_order.append((i, rest))
            note.order_qty = line.qty - take

    parts: list[Part] = []
    order_parts: list[Part] = []
    for warehouse_id, its_lines in pinned.items():
        order_parts.append(Part(ORDER, its_lines, by_id[warehouse_id]))
    if auto_order:
        order_parts += _place(tenant, user, [l for _, l in auto_order], [i for i, _ in auto_order], notes,
                              dispatch_on, pincode, finance)
    order_parts = _merged(order_parts)
    # Services and free-text lines ride with the invoice when there is one.
    if loose:
        if invoice_lines or not order_parts:
            invoice_lines += loose
        else:
            order_parts[0].lines = order_parts[0].lines + loose
    if finance and orders_ok:
        waiting = [p for p in order_parts if p.kind == WAITING]
        if waiting or len(order_parts) > 1:
            problems.append("An EMI sale is one order from one warehouse with every unit - no single warehouse "
                            "has all of this free.")
    if invoice_lines:
        parts.append(Part(INVOICE, invoice_lines, here))
    parts += order_parts
    return Plan(here, mode, parts, notes, problems, finance=bool(finance and orders_ok))


def _merged(parts: list[Part]) -> list[Part]:
    """One order a warehouse: a line pinned to a warehouse joins what ARFA
    sends there anyway."""
    out: list[Part] = []
    for part in parts:
        same = next((o for o in out if o.kind == ORDER and part.kind == ORDER and o.location is not None
                     and part.location is not None and o.location.id == part.location.id), None)
        if same is not None:
            same.lines = same.lines + part.lines
        else:
            out.append(part)
    return out


def _place(tenant, user, lines, rows, notes, dispatch_on, pincode, finance) -> list[Part]:
    """Where ARFA would send these order lines: the nearest warehouse with
    it all, else spread over warehouses nearest first, else waiting."""
    if not dispatch_on:
        # No warehouses picking orders: a plain sales order, as before.
        return [Part(ORDER, lines, None)]
    from app.warehouse import nearest

    home = ledger.location_for_user(tenant.id, user)
    suggestion = nearest.suggest_warehouse(tenant, pincode, lines, fallback=home.default_dispatch_location)
    best = suggestion.location
    if best is not None:
        try:
            dispatch.check_free(tenant, best, lines)
            return [Part(ORDER, lines, best)]
        except dispatch.DispatchError:
            pass
    plan = None if finance else nearest.plan_split(tenant, suggestion.ranked, lines)
    if plan:
        return [Part(ORDER, [replace(lines[i], qty=Decimal(q)) for i, q in sorted(shares.items()) if q > 0], location)
                for location, shares in plan]
    for row, line in zip(rows, lines):
        notes[row].waiting_qty, notes[row].order_qty = line.qty, ZERO
    return [Part(WAITING, lines, None)]


# --- saying it in words -------------------------------------------------------------------------------

def _items(n: int) -> str:
    return f"{n} item" + ("" if n == 1 else "s")


def _sentence(plan: Plan) -> str:
    if plan.mode == QUOTE:
        return "This will save a quotation. Nothing is billed and no stock moves."
    if plan.mode == PROFORMA:
        return "This will save a proforma invoice. Nothing is billed and no stock moves."
    if plan.mode == CHALLAN:
        return "This will make a delivery challan: the goods go out, and nothing is billed yet."
    said = []
    for part in plan.parts:
        n = _items(len(part.lines))
        if part.kind == INVOICE:
            where = f" from {part.location.name}" if part.location is not None and len(plan.parts) > 1 else ""
            said.append(f"bill {n} now{where}")
        elif part.kind == ORDER:
            said.append(f"order {n} from {part.location.name}" if part.location is not None
                        else f"take an order for {n}")
        elif part.kind == WAITING:
            said.append(f"take an order for {n} that waits for stock - no warehouse has it free")
    if not said:
        return ""
    text = said[0] if len(said) == 1 else ", ".join(said[:-1]) + ", and " + said[-1]
    if plan.finance:
        text += " (EMI: the invoice is made at the showroom once every unit is locked)"
    return f"This will {text}."


def _button(plan: Plan) -> str:
    if plan.mode == QUOTE:
        return "Save quote"
    if plan.mode == PROFORMA:
        return "Save proforma"
    if plan.mode == CHALLAN:
        return "Send goods"
    invoices = [p for p in plan.parts if p.kind == INVOICE]
    orders = [p for p in plan.parts if p.kind == ORDER]
    waiting = [p for p in plan.parts if p.kind == WAITING]
    if plan.finance:
        return "Take EMI order"
    if invoices and not orders and not waiting:
        return "Bill now"
    if invoices:
        ordered = sum(len(p.lines) for p in orders + waiting)
        return f"Bill {len(invoices[0].lines)} now, order {ordered}"
    if orders and not waiting:
        if len(orders) == 1:
            return (f"Send order to {orders[0].location.name}" if orders[0].location is not None else "Take the order")
        return f"Send {len(orders)} orders"
    return "Take the order"


# --- saving -------------------------------------------------------------------------------------------

@dataclass
class Header:
    gstin_id: int
    customer_id: int
    place_of_supply_state_code: str
    on: object                              # the date
    document_type: DocumentType
    notes: str | None = None
    terms: str | None = None
    ship_to: dict | None = None
    valid_until: object = None
    challan_purpose: ChallanPurpose | None = None
    finance: dict = field(default_factory=dict)       # is_finance_sale, financier_id, loan_ref, ...


@dataclass
class Result:
    ref: str | None
    invoice: object = None
    documents: list = field(default_factory=list)     # sales documents made (orders, quote, challan)

    @property
    def everything(self) -> list:
        return ([self.invoice] if self.invoice is not None else []) + list(self.documents)


def new_ref() -> str:
    return secrets.token_hex(6)


def save(tenant, user, header: Header, plan: Plan) -> Result:
    """Makes what the plan says, all or nothing (the caller commits, or rolls
    back on SellError). Each document is made by the service that owns it."""
    if plan.problems:
        raise SellError(plan.problems[0])
    if not plan.parts:
        raise SellError("Add at least one item.")
    result = Result(new_ref() if len(plan.parts) > 1 else None)
    common = dict(gstin_id=header.gstin_id, customer_id=header.customer_id,
                  place_of_supply_state_code=header.place_of_supply_state_code, doc_date=header.on,
                  invoice_document_type=header.document_type, notes=header.notes, terms=header.terms)
    try:
        for part in plan.parts:
            if part.kind == INVOICE:
                _check_serials(tenant, part)
                invoice = create_invoice(tenant, user, InvoiceInput(
                    gstin_id=header.gstin_id, document_type=header.document_type, customer_id=header.customer_id,
                    place_of_supply_state_code=header.place_of_supply_state_code, invoice_date=header.on,
                    notes=header.notes, lines=part.lines,
                    dispatch_location_id=part.location.id if part.location is not None else None))
                invoice.sale_ref = result.ref
                result.invoice = invoice
            elif part.kind in (ORDER, WAITING):
                doc = sales_services.create_sales_document(tenant, user, sales_services.SalesDocInput(
                    kind=SalesDocumentKind.SALES_ORDER, lines=part.lines, ship_to=header.ship_to,
                    dispatch_location_id=part.location.id if part.location is not None else None,
                    await_stock=part.kind == WAITING, split=False, **common,
                    **(header.finance if plan.finance else {})))
                doc.sale_ref = result.ref
                result.documents.append(doc)
            else:
                kind = {QUOTE: SalesDocumentKind.QUOTATION, PROFORMA: SalesDocumentKind.PROFORMA,
                        CHALLAN: SalesDocumentKind.DELIVERY_CHALLAN}[part.kind]
                doc = sales_services.create_sales_document(tenant, user, sales_services.SalesDocInput(
                    kind=kind, lines=part.lines, valid_until=header.valid_until,
                    challan_purpose=header.challan_purpose, **common))
                result.documents.append(doc)
    except sales_services.SalesError as exc:
        raise SellError(exc.message)
    except Exception as exc:
        from app.invoicing.services import InvoiceValidationError

        if isinstance(exc, (InvoiceValidationError, ledger.StockError)):
            raise SellError(exc.message)
        raise
    db.session.flush()
    return result


def _check_serials(tenant, part: Part) -> None:
    """The serial numbers typed for a line billed here: one a unit, in stock,
    and here (the same check the counter makes)."""
    from app.inventory import hooks

    for line in part.lines:
        if not line.serials or not line.product_id:
            continue
        product = db.session.get(Product, line.product_id)
        if product is None or not _needs_serials(tenant, product):
            line.serials = []
            continue
        hooks._check_serials(tenant.id, product, line, part.location.id if part.location is not None else None)


def siblings(tenant_id: int, sale_ref: str | None) -> dict:
    """The invoice and orders made together as one sale."""
    if not sale_ref:
        return {"invoices": [], "documents": []}
    from app.models.invoice import Invoice
    from app.models.sales import SalesDocument

    return {"invoices": Invoice.query.filter_by(tenant_id=tenant_id, sale_ref=sale_ref).order_by(Invoice.id).all(),
            "documents": SalesDocument.query.filter_by(tenant_id=tenant_id, sale_ref=sale_ref)
            .order_by(SalesDocument.id).all()}
