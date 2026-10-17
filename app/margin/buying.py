"""Buying second-hand goods for resale under the margin scheme (module
"margin_scheme").

The dealer's own **purchase voucher** (series SHP/<FY>/0001) for goods
bought from:
  - a person with no GST registration (someone selling their old phone):
    no invoice of theirs, so the voucher is the document - with their
    address and ID proof, and how they were paid; no GST on it;
  - a registered dealer: their invoice number, date and the GST they
    charged are recorded on the voucher - and still no input tax credit
    is taken (Rule 32(5) needs that), so what was paid, GST included, is
    the purchase price the margin is worked from.

Underneath it is an ordinary purchase bill (kind "margin_voucher"), so
stock, serial/IMEI units, the margin lots, the purchase register, the
supplier's account and voiding all work as for any purchase.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.inventory import ledger
from app.models.product import Product
from app.models.purchase import ItcEligibility, PurchaseBill, PurchaseStatus, Supplier
from app.models.tenant import Gstin
from app.utils.gst import GSTIN_REGEX, normalize_gstin
from app.utils.indian_states import STATE_NAME_BY_CODE

ID_TYPES = {
    "aadhaar": "Aadhaar",
    "pan": "PAN",
    "dl": "Driving licence",
    "voter": "Voter ID",
    "passport": "Passport",
    "other": "Other",
}
PAID_BY = {"cash": "Cash", "upi": "UPI", "bank": "Bank transfer", "cheque": "Cheque"}
VOUCHER = "margin_voucher"


class BuyError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class Seller:
    name: str
    phone: str = ""
    address: str = ""
    state_code: str = ""
    id_type: str = ""
    id_no: str = ""
    gstin: str = ""
    invoice_no: str = ""            # a registered dealer's own invoice
    invoice_date: date | None = None


@dataclass
class BuyLine:
    qty: Decimal
    price: Decimal                   # per unit, before any GST the seller charged
    product_id: int | None = None
    # A used item not in the catalogue yet - created as a margin-scheme item.
    new_name: str = ""
    new_hsn: str = ""
    new_gst_rate: Decimal = Decimal("18")
    seller_gst_rate: Decimal = Decimal("0")   # GST on the seller's invoice (0 from a person)
    serials: list = field(default_factory=list)


def _primary_gstin(tenant) -> Gstin:
    gstin = (tenant.gstins.filter(Gstin.is_active.is_(True), Gstin.gstin.isnot(None))
             .order_by(Gstin.is_primary.desc(), Gstin.id).first())
    if gstin is None:
        raise BuyError("The margin scheme needs a GST registration - add the business's GSTIN first.")
    return gstin


def seller_supplier(tenant, seller: Seller) -> Supplier:
    """The seller as a supplier record - the same one again for a repeat
    seller (by GSTIN, else by name and phone)."""
    name = (seller.name or "").strip()
    if not name:
        raise BuyError("Enter who you bought from.")
    gstin = normalize_gstin(seller.gstin or "") or None
    if gstin and not re.match(GSTIN_REGEX, gstin):
        raise BuyError(f"{gstin} isn't a valid GSTIN.")
    phone = (seller.phone or "").strip()[:20]
    query = Supplier.query.filter_by(tenant_id=tenant.id)
    found = (query.filter_by(gstin=gstin).first() if gstin
             else query.filter(db.func.lower(Supplier.name) == name.lower(), Supplier.gstin.is_(None),
                               Supplier.phone == (phone or None)).first())
    state = (gstin[:2] if gstin else seller.state_code) or _primary_gstin(tenant).state_code
    if found is None:
        found = Supplier(tenant_id=tenant.id, name=name[:255], gstin=gstin, phone=phone or None)
        db.session.add(found)
    found.state_code = state
    found.state_name = STATE_NAME_BY_CODE.get(state, "")
    if seller.address:
        found.address_line1 = seller.address.strip()[:255]
    db.session.flush()
    return found


def new_used_item(tenant, name: str, hsn: str, gst_rate, track_serial: bool) -> Product:
    """A second-hand item made from the voucher: sold under the margin
    scheme, price GST-inclusive (the buyer pays the price)."""
    from app.utils.gst_config import GST_RATE_SLABS

    name, hsn = (name or "").strip(), (hsn or "").strip()
    if not name:
        raise BuyError("Name the item.")
    if not hsn.isdigit() or not 4 <= len(hsn) <= 8:
        raise BuyError(f"{name}: the HSN code is 4 to 8 digits.")
    rate = Decimal(str(gst_rate))
    if rate not in {Decimal(str(s)) for s in GST_RATE_SLABS}:
        raise BuyError(f"{name}: {rate}% isn't a GST rate.")
    product = Product(tenant_id=tenant.id, name=name[:255], hsn_or_sac_code=hsn, gst_rate=rate, unit="PCS",
                      sale_price=Decimal("0"), price_includes_tax=True, is_margin_scheme=True,
                      track_serial=bool(track_serial), allow_negative_stock=False)
    db.session.add(product)
    db.session.flush()
    return product


def buy(tenant, user, seller: Seller, lines: list[BuyLine], paid_by: str = "cash", notes: str = "",
        bill_date: date | None = None) -> PurchaseBill:
    """Record a purchase of second-hand goods. Raises BuyError."""
    from app.margin.services import margin_on
    from app.purchases import services as ps
    from app.utils.module_settings import module_settings

    if not margin_on(tenant):
        raise BuyError("The margin scheme is for a regular GST registration with the module on.")
    if not lines:
        raise BuyError("Add at least one item you bought.")
    registered = bool(normalize_gstin(seller.gstin or ""))
    if not registered:
        settings = module_settings(tenant, "margin")
        if settings.get("id_proof_required") and not (seller.id_type and (seller.id_no or "").strip()):
            raise BuyError("Enter the seller's ID proof - type and number.")
        if seller.id_type and seller.id_type not in ID_TYPES:
            raise BuyError("Pick the kind of ID proof.")
    if paid_by not in PAID_BY:
        raise BuyError("Pick how the seller was paid.")
    supplier = seller_supplier(tenant, seller)
    gstin = _primary_gstin(tenant)
    serials_on = tenant.has_feature("serial_imei")
    purchase_lines = []
    for line in lines:
        if line.product_id:
            product = Product.query.filter_by(id=line.product_id, tenant_id=tenant.id).first()
            if product is None:
                raise BuyError("An item picked isn't in your catalogue.")
            if not product.is_margin_scheme:
                raise BuyError(f"{product.name} isn't a second-hand (margin scheme) item - buy it on a purchase "
                               "bill, or mark it as second-hand on its item page.")
        else:
            product = new_used_item(tenant, line.new_name, line.new_hsn, line.new_gst_rate,
                                    track_serial=serials_on and bool(line.serials))
        if line.qty <= 0 or line.price <= 0:
            raise BuyError(f"{product.name}: enter the quantity and the price paid.")
        seller_rate = Decimal(line.seller_gst_rate or 0) if registered else Decimal("0")
        purchase_lines.append(ps.PurchaseLineInput(
            product_id=product.id, description=product.name, hsn_or_sac_code=product.hsn_or_sac_code,
            qty=Decimal(line.qty), unit=product.unit, rate=Decimal(line.price), gst_rate=seller_rate,
            itc_eligibility=ItcEligibility.INELIGIBLE, serials=list(line.serials or [])))
    details = {"address": (seller.address or "").strip()[:255], "id_type": seller.id_type or "",
               "id_no": (seller.id_no or "").strip()[:40], "paid_by": paid_by,
               "phone": (seller.phone or "").strip()[:20], "registered": registered}
    try:
        bill = ps.create_purchase_bill(tenant, user, ps.PurchaseInput(
            supplier_id=supplier.id, gstin_id=gstin.id,
            supplier_invoice_no=(seller.invoice_no or "").strip() if registered else "",
            supplier_invoice_date=seller.invoice_date if registered else None,
            location_id=ledger.default_location(tenant.id).id, lines=purchase_lines, bill_date=bill_date,
            notes=notes, kind=VOUCHER, seller_details=details))
    except ps.PurchaseError as exc:
        raise BuyError(exc.message)
    return bill


def vouchers(tenant_id: int):
    return (PurchaseBill.query.filter_by(tenant_id=tenant_id, kind=VOUCHER)
            .order_by(PurchaseBill.bill_date.desc(), PurchaseBill.id.desc()))


def live(bill) -> bool:
    return bill.status == PurchaseStatus.POSTED
