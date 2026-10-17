"""e-Way bills (flag eway_bill) for goods leaving with an invoice, a
delivery challan (job work, approval, exhibition...), an invoice billed to
an owned branch (a challan), or a stock transfer between the client's own
locations - needed when the consignment is worth more than
EWAY_BILL_VALUE_THRESHOLD.

Part A comes from the document; the user adds Part B (vehicle or
transporter) on the form. Then either:
  - download the JSON for the e-way bill portal's bulk-generation tool,
    upload it there and record the EWB number it gives back, or
  - generate it through the client's provider: from the IRN when the
    invoice has one (IRP "EWB by IRN"), standalone otherwise.
Part B (vehicle) can be updated, and the bill cancelled within
EWAY_BILL_CANCEL_HOURS - through the API or recorded as done on the
portal. Never deleted."""

import re
from datetime import datetime
from decimal import Decimal

from app.einvoice import rules
from app.einvoice.providers.common import IST, parse_dt
from app.einvoice.services import EInvoiceError, call, credential_for
from app.models.einvoice import EWayBill, EWayBillStatus
from app.models.invoice import Invoice, InvoiceStatus
from app.models.invoice_series import DocumentType
from app.models.inventory import StockDocStatus, StockTransfer
from app.models.mixins import utcnow
from app.models.purchase import PurchaseBill, PurchaseStatus
from app.models.sales import ChallanPurpose, SalesDocument, SalesDocumentKind, SalesDocumentStatus
from app.utils.audit import record_audit
from app.utils.gst_config import EWAY_BILL_CANCEL_HOURS, EWAY_BILL_MAX_DISTANCE_KM, UQC_CODES

# Bulk-generation tool JSON format version (the e-way bill portal's
# "Bulk Generation" tool, v1.0.0621 - update if the portal asks for another).
BULK_VERSION = "1.0.0621"
SOURCE_TYPES = ("invoice", "sales_document", "stock_transfer", "purchase_bill")

SUB_SUPPLY = {"1": "Supply", "3": "Export", "4": "Job work", "5": "For own use", "6": "Job work returns",
              "7": "Sales return", "8": "Others", "9": "SKD / CKD / lots", "10": "Line sales",
              "11": "Recipient not known", "12": "Exhibition or fairs"}
TRANS_MODES = {"1": "Road", "2": "Rail", "3": "Air", "4": "Ship"}
VEHICLE_TYPES = {"R": "Regular", "O": "Over-dimensional cargo (ODC)"}
CANCEL_REASONS = {"1": "Duplicate", "2": "Order cancelled", "3": "Data entry mistake", "4": "Others"}
PART_B_REASONS = {"1": "Vehicle broke down", "2": "Transhipment", "3": "Others", "4": "First time"}
DOC_TYPE_LABELS = {"INV": "Tax invoice", "BIL": "Bill of supply", "CHL": "Delivery challan", "OTH": "Others"}

PIN = re.compile(r"^[1-9][0-9]{5}$")
VEHICLE = re.compile(r"^[A-Z]{2}[0-9A-Z]{4,13}$")
TRANSPORTER = re.compile(r"^[0-9]{2}[0-9A-Z]{13}$")
EWB_NO = re.compile(r"^[0-9]{12}$")
DOC_NO = re.compile(r"^[A-Za-z0-9/-]{1,16}$")
EXPORT_STATE = "99"  # "other country" in the e-way bill system
ZERO = Decimal("0")


# --- sources --------------------------------------------------------------------------

def source_value(source) -> Decimal:
    if isinstance(source, (Invoice, SalesDocument, PurchaseBill)):
        return Decimal(source.grand_total or 0)
    return sum((Decimal(l.qty) * Decimal(l.unit_cost or 0) for l in source.lines), ZERO).quantize(Decimal("0.01"))


def source_number(source) -> str:
    if isinstance(source, PurchaseBill):
        # Goods coming in: the seller's own invoice when they gave one,
        # else the dealer's purchase voucher.
        return source.supplier_invoice_no if (source.seller_details or {}).get("registered") else source.number
    return source.invoice_number if isinstance(source, Invoice) else source.number


def source_date(source):
    if isinstance(source, Invoice):
        return source.invoice_date
    if isinstance(source, PurchaseBill):
        return source.supplier_invoice_date if (source.seller_details or {}).get("registered") else source.bill_date
    return source.doc_date if isinstance(source, SalesDocument) else source.transfer_date


def source_type_of(source) -> str:
    if isinstance(source, Invoice):
        return "invoice"
    if isinstance(source, PurchaseBill):
        return "purchase_bill"
    return "sales_document" if isinstance(source, SalesDocument) else "stock_transfer"


def eligible(source) -> bool:
    """A live document that moves goods."""
    if isinstance(source, Invoice):
        return source.status == InvoiceStatus.ISSUED and source.document_type != DocumentType.POS_BILL
    if isinstance(source, SalesDocument):
        return source.kind == SalesDocumentKind.DELIVERY_CHALLAN and source.status != SalesDocumentStatus.CANCELLED
    if isinstance(source, PurchaseBill):
        # Inward: second-hand goods bought from a seller, carried to the
        # dealer (the dealer, a registered person, makes the e-way bill).
        return source.status == PurchaseStatus.POSTED and source.kind == "margin_voucher"
    return source.status == StockDocStatus.COMPLETED


def active_for(tenant_id: int, source) -> EWayBill | None:
    return (EWayBill.query.filter_by(tenant_id=tenant_id, source_type=source_type_of(source), source_id=source.id)
            .filter(EWayBill.status != EWayBillStatus.CANCELLED).order_by(EWayBill.id.desc()).first())


def offer(tenant, source) -> bool:
    """Show the 'e-Way bill' action on this document."""
    return (tenant.has_feature("eway_bill") and eligible(source) and source.gstin and bool(source.gstin.gstin)
            and rules.eway_bill_needed(source_value(source)))


def _pin_in(text) -> str:
    m = re.search(r"\b([1-9][0-9]{5})\b", text or "")
    return m.group(1) if m else ""


def _own(tenant, gstin, address=None) -> dict:
    addr = (address or gstin.registered_address or "").strip()
    return {"gstin": gstin.gstin, "name": tenant.trade_name or tenant.legal_name, "addr1": addr[:120],
            "addr2": "", "place": gstin.city or "", "pincode": _pin_in(address) or gstin.pincode or "",
            "state_code": gstin.state_code}


def _party(snapshot: dict, export: bool) -> dict:
    if export:
        return {"gstin": "URP", "name": snapshot.get("name") or "", "addr1": snapshot.get("address_line1") or "",
                "addr2": snapshot.get("address_line2") or "", "place": snapshot.get("city") or "",
                "pincode": "999999", "state_code": EXPORT_STATE}
    return {"gstin": (snapshot.get("gstin") or "").strip().upper() or "URP", "name": snapshot.get("name") or "",
            "addr1": snapshot.get("address_line1") or "", "addr2": snapshot.get("address_line2") or "",
            "place": snapshot.get("city") or "", "pincode": str(snapshot.get("pincode") or ""),
            "state_code": snapshot.get("state_code") or ""}


def _unit(unit) -> str:
    u = (unit or "").strip().upper()
    return u if u in UQC_CODES else "OTH"


def _line_items(lines, rcm=False) -> tuple[list, dict]:
    items, totals = [], {"taxable": ZERO, "cgst": ZERO, "sgst": ZERO, "igst": ZERO}
    for line in lines:
        cgst = Decimal((line.rcgst_amount if rcm else line.cgst_amount) or 0)
        sgst = Decimal((line.rsgst_amount if rcm else line.sgst_amount) or 0)
        igst = Decimal((line.rigst_amount if rcm else line.igst_amount) or 0)
        rate = Decimal(line.gst_rate or 0)
        items.append({"name": line.description[:100], "hsn": (line.hsn_or_sac_code or "").strip(),
                      "qty": str(Decimal(line.qty)), "unit": _unit(line.unit),
                      "taxable": str(Decimal(line.taxable_value)),
                      "cgst_rate": str(rate / 2) if cgst else "0", "sgst_rate": str(rate / 2) if sgst else "0",
                      "igst_rate": str(rate) if igst else "0"})
        totals["taxable"] += Decimal(line.taxable_value)
        totals["cgst"] += cgst
        totals["sgst"] += sgst
        totals["igst"] += igst
    return items, totals


def draft_from(tenant, source, user) -> EWayBill:
    """A new (unsaved) e-way bill with Part A filled in from the document."""
    ewb = EWayBill(tenant_id=tenant.id, gstin_id=source.gstin_id, source_type=source_type_of(source),
                   source_id=source.id, document_number=source_number(source), document_date=source_date(source),
                   supply_type="O", created_by_id=user.id, status=EWayBillStatus.DRAFT)
    ewb.from_party = _own(tenant, source.gstin)
    ewb.other_value = ZERO
    if isinstance(source, PurchaseBill):
        return _inward_draft(tenant, source, ewb)
    if isinstance(source, StockTransfer):
        ewb.doc_type, ewb.sub_supply_type = "CHL", "5"
        ewb.from_party = _own(tenant, source.gstin, source.from_location.address)
        to = _own(tenant, source.gstin, source.to_location.address)
        to["name"] = f"{to['name']} - {source.to_location.name}"[:100]
        ewb.to_party = to
        ewb.items = [{"name": l.product.name[:100], "hsn": (l.product.hsn_or_sac_code or "").strip(),
                      "qty": str(Decimal(l.qty)), "unit": _unit(l.product.unit),
                      "taxable": str((Decimal(l.qty) * Decimal(l.unit_cost or 0)).quantize(Decimal("0.01"))),
                      "cgst_rate": "0", "sgst_rate": "0", "igst_rate": "0"} for l in source.lines]
        ewb.total_value = ewb.total_invoice_value = source_value(source)
        ewb.cgst_value = ewb.sgst_value = ewb.igst_value = ZERO
        ewb.vehicle_no = (source.vehicle_no or "").replace(" ", "").upper() or None
        return ewb
    if isinstance(source, SalesDocument):
        ewb.doc_type = "CHL"
        ewb.sub_supply_type = {ChallanPurpose.JOB_WORK: "4", ChallanPurpose.STOCK_TRANSFER: "5"}.get(
            source.challan_purpose, "8")
        if ewb.sub_supply_type == "8":
            ewb.sub_supply_desc = ("Sale on approval" if source.challan_purpose == ChallanPurpose.SALE_ON_APPROVAL
                                   else "Others")
        rcm, export = False, False
    else:
        dt = source.document_type
        export = dt in (DocumentType.EXPORT_INVOICE_IGST, DocumentType.EXPORT_INVOICE_LUT)
        rcm = dt == DocumentType.RCM_INVOICE
        ewb.doc_type = {DocumentType.BILL_OF_SUPPLY: "BIL", DocumentType.DELIVERY_CHALLAN: "CHL"}.get(dt, "INV")
        ewb.sub_supply_type = "3" if export else ("5" if dt == DocumentType.DELIVERY_CHALLAN else "1")
    ewb.to_party = _party(source.customer_snapshot or {}, export)
    ewb.items, totals = _line_items(source.lines, rcm)
    ewb.total_value, ewb.cgst_value = totals["taxable"], totals["cgst"]
    ewb.sgst_value, ewb.igst_value = totals["sgst"], totals["igst"]
    ewb.total_invoice_value = Decimal(source.grand_total or 0)
    if getattr(source, "document_type", None) == DocumentType.MARGIN_SCHEME_INVOICE:
        # Margin scheme: the taxable value is the margin's; the rest of the
        # price is "other value", so taxable + tax + other = invoice value.
        ewb.other_value = max(ewb.total_invoice_value - ewb.total_value - ewb.cgst_value - ewb.sgst_value
                              - ewb.igst_value, ZERO)
    return ewb


def _inward_draft(tenant, bill, ewb: EWayBill) -> EWayBill:
    """Second-hand goods bought (margin-scheme purchase voucher), coming in
    from the seller: supply type Inward, from the seller (URP when they
    have no GSTIN) to the dealer. No GST when bought from a person."""
    seller = bill.supplier_snapshot or {}
    details = bill.seller_details or {}
    registered = bool(details.get("registered"))
    ewb.supply_type, ewb.sub_supply_type = "I", "1"
    ewb.doc_type = "INV" if registered else "BIL"
    address = details.get("address") or seller.get("address") or ""
    ewb.from_party = {"gstin": (seller.get("gstin") or "URP") if registered else "URP", "name": seller.get("name") or "",
                      "addr1": address[:120], "addr2": "", "place": seller.get("city") or "",
                      "pincode": _pin_in(address), "state_code": seller.get("state_code") or bill.gstin.state_code}
    ewb.to_party = _own(tenant, bill.gstin, bill.location.address if bill.location else None)
    items, totals = [], {"taxable": ZERO, "cgst": ZERO, "sgst": ZERO, "igst": ZERO}
    for line in bill.lines:
        rate = Decimal(line.gst_rate or 0)
        items.append({"name": line.description[:100], "hsn": (line.hsn_or_sac_code or "").strip(),
                      "qty": str(Decimal(line.qty)), "unit": _unit(line.unit), "taxable": str(Decimal(line.taxable_value)),
                      "cgst_rate": str(rate / 2) if line.cgst_amount else "0",
                      "sgst_rate": str(rate / 2) if line.sgst_amount else "0",
                      "igst_rate": str(rate) if line.igst_amount else "0"})
        totals["taxable"] += Decimal(line.taxable_value)
        totals["cgst"] += Decimal(line.cgst_amount or 0)
        totals["sgst"] += Decimal(line.sgst_amount or 0)
        totals["igst"] += Decimal(line.igst_amount or 0)
    ewb.items = items
    ewb.total_value, ewb.cgst_value = totals["taxable"], totals["cgst"]
    ewb.sgst_value, ewb.igst_value = totals["sgst"], totals["igst"]
    ewb.total_invoice_value = Decimal(bill.grand_total or 0)
    ewb.other_value = max(ewb.total_invoice_value - totals["taxable"] - totals["cgst"] - totals["sgst"]
                          - totals["igst"], ZERO)
    return ewb


# --- the form -------------------------------------------------------------------------

def apply_form(ewb: EWayBill, form) -> None:
    """Editable parts: sub-supply type, the two addresses, Part B."""
    if ewb.status != EWayBillStatus.DRAFT:
        raise EInvoiceError("Only a draft can be edited - use 'Update vehicle' for Part B.")
    sub = form.get("sub_supply_type") or ewb.sub_supply_type
    if sub not in SUB_SUPPLY:
        raise EInvoiceError("Choose the sub-supply type.")
    ewb.sub_supply_type = sub
    ewb.sub_supply_desc = (form.get("sub_supply_desc") or "").strip()[:20] or None
    for side in ("from", "to"):
        party = dict(getattr(ewb, f"{side}_party") or {})
        for key in ("name", "addr1", "addr2", "place", "pincode"):
            if f"{side}_{key}" in form:
                party[key] = (form.get(f"{side}_{key}") or "").strip()[:120]
        if side == "from" and ewb.supply_type == "I" and "from_state_code" in form:
            party["state_code"] = (form.get("from_state_code") or "").strip()
        if side == "to":
            if "to_gstin" in form:
                party["gstin"] = (form.get("to_gstin") or "").strip().upper() or "URP"
            if "to_state_code" in form:
                party["state_code"] = (form.get("to_state_code") or "").strip()
        setattr(ewb, f"{side}_party", party)
    _apply_part_b(ewb, form)


def _apply_part_b(ewb: EWayBill, form) -> None:
    mode = form.get("trans_mode") or "1"
    if mode not in TRANS_MODES:
        raise EInvoiceError("Choose how the goods travel.")
    ewb.trans_mode = mode
    try:
        ewb.distance_km = int(form.get("distance_km") or 0)
    except ValueError:
        raise EInvoiceError("Distance is a whole number of kilometres.")
    ewb.transporter_id = (form.get("transporter_id") or "").strip().upper() or None
    ewb.transporter_name = (form.get("transporter_name") or "").strip()[:100] or None
    ewb.trans_doc_no = (form.get("trans_doc_no") or "").strip()[:15] or None
    try:
        ewb.trans_doc_date = (datetime.strptime(form["trans_doc_date"], "%Y-%m-%d").date()
                              if form.get("trans_doc_date") else None)
    except ValueError:
        raise EInvoiceError("The transport document date isn't a date.")
    ewb.vehicle_no = re.sub(r"[\s-]", "", form.get("vehicle_no") or "").upper() or None
    vtype = form.get("vehicle_type") or "R"
    ewb.vehicle_type = vtype if vtype in VEHICLE_TYPES else "R"


def problems(ewb: EWayBill) -> list[str]:
    out = []
    if not DOC_NO.match(ewb.document_number or ""):
        out.append(f"The document number '{ewb.document_number}' won't be accepted - up to 16 letters, digits, "
                   "/ and -.")
    for side, label in (("from", "Dispatch-from"), ("to", "Ship-to")):
        p = getattr(ewb, f"{side}_party") or {}
        if not p.get("addr1") or not p.get("place"):
            out.append(f"{label} address and place are needed.")
        if not PIN.match(str(p.get("pincode") or "")) and not (side == "to" and p.get("state_code") == EXPORT_STATE):
            out.append(f"{label} 6-digit PIN code is needed.")
        if not p.get("state_code"):
            out.append(f"{label} state is needed.")
    if ewb.sub_supply_type == "8" and not ewb.sub_supply_desc:
        out.append("Describe the sub-supply type 'Others'.")
    if not 1 <= (ewb.distance_km or 0) <= EWAY_BILL_MAX_DISTANCE_KM:
        out.append(f"Distance must be between 1 and {EWAY_BILL_MAX_DISTANCE_KM} km.")
    if ewb.transporter_id and not TRANSPORTER.match(ewb.transporter_id):
        out.append("The transporter ID is the transporter's 15-character GSTIN or TRANSIN.")
    if ewb.trans_mode == "1":
        if ewb.vehicle_no and not VEHICLE.match(ewb.vehicle_no):
            out.append("The vehicle number should look like TN01AB1234.")
        if not ewb.vehicle_no and not ewb.transporter_id:
            out.append("Give the vehicle number, or the transporter ID so the transporter can add the vehicle.")
    elif not ewb.trans_doc_no or not ewb.trans_doc_date:
        out.append(f"For {TRANS_MODES[ewb.trans_mode].lower()}, the transport document number and date are needed.")
    for i, item in enumerate(ewb.items or [], start=1):
        if not re.match(r"^[0-9]{4,8}$", item.get("hsn") or ""):
            out.append(f"Item {i}: an HSN code is needed.")
    if not ewb.items:
        out.append("There are no items.")
    return out


def _check(ewb):
    found = problems(ewb)
    if found:
        raise EInvoiceError(" ".join(found))


# --- JSON -----------------------------------------------------------------------------

def _d(value) -> str:
    return value.strftime("%d/%m/%Y") if value else ""


def _num(value) -> float:
    return float(Decimal(value or 0).quantize(Decimal("0.01")))


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _part_b(ewb) -> dict:
    return {"transMode": ewb.trans_mode, "transDistance": str(ewb.distance_km or 0),
            "transporterId": ewb.transporter_id or "", "transporterName": ewb.transporter_name or "",
            "transDocNo": ewb.trans_doc_no or "", "transDocDate": _d(ewb.trans_doc_date),
            "vehicleNo": ewb.vehicle_no or "", "vehicleType": ewb.vehicle_type}


def api_payload(ewb: EWayBill) -> dict:
    """GENEWAYBILL request (e-way bill API v1.03)."""
    f, t = ewb.from_party, ewb.to_party
    payload = {
        "supplyType": ewb.supply_type, "subSupplyType": ewb.sub_supply_type, "subSupplyDesc": ewb.sub_supply_desc or "",
        "docType": ewb.doc_type, "docNo": ewb.document_number, "docDate": _d(ewb.document_date),
        "fromGstin": f["gstin"], "fromTrdName": f["name"], "fromAddr1": f["addr1"], "fromAddr2": f.get("addr2", ""),
        "fromPlace": f["place"], "fromPincode": _int(f["pincode"]), "actFromStateCode": _int(f["state_code"]),
        "fromStateCode": _int(f["state_code"]),
        "toGstin": t.get("gstin") or "URP", "toTrdName": t["name"], "toAddr1": t["addr1"],
        "toAddr2": t.get("addr2", ""), "toPlace": t["place"], "toPincode": _int(t["pincode"]),
        "actToStateCode": _int(t["state_code"]), "toStateCode": _int(t["state_code"]),
        "transactionType": 1, "otherValue": _num(ewb.other_value or 0), "totalValue": _num(ewb.total_value),
        "cgstValue": _num(ewb.cgst_value), "sgstValue": _num(ewb.sgst_value), "igstValue": _num(ewb.igst_value),
        "cessValue": 0, "cessNonAdvolValue": 0, "totInvValue": _num(ewb.total_invoice_value),
        "itemList": [{"productName": i["name"], "productDesc": i["name"], "hsnCode": _int(i["hsn"]),
                      "quantity": float(i["qty"]), "qtyUnit": i["unit"], "cgstRate": float(i["cgst_rate"]),
                      "sgstRate": float(i["sgst_rate"]), "igstRate": float(i["igst_rate"]), "cessRate": 0,
                      "cessNonadvol": 0, "taxableAmount": float(i["taxable"])} for i in ewb.items],
    }
    payload.update(_part_b(ewb))
    return payload


def bulk_entry(ewb: EWayBill) -> dict:
    """One bill in the bulk-generation tool's format (field names differ
    slightly from the API's)."""
    p = api_payload(ewb)
    entry = {"userGstin": p["fromGstin"], "supplyType": p["supplyType"], "subSupplyType": _int(p["subSupplyType"]),
             "subSupplyDesc": p["subSupplyDesc"], "docType": p["docType"], "docNo": p["docNo"],
             "docDate": p["docDate"], "transType": 1}
    for key in ("fromGstin", "fromTrdName", "fromAddr1", "fromAddr2", "fromPlace", "fromPincode", "fromStateCode",
                "toGstin", "toTrdName", "toAddr1", "toAddr2", "toPlace", "toPincode", "toStateCode",
                "totalValue", "cgstValue", "sgstValue", "igstValue", "cessValue", "totInvValue", "transMode",
                "transDistance", "transporterName", "transporterId", "transDocNo", "transDocDate", "vehicleNo",
                "vehicleType"):
        entry[key] = p[key]
    entry["actualFromStateCode"] = p["actFromStateCode"]
    entry["actualToStateCode"] = p["actToStateCode"]
    entry["TotNonAdvolVal"] = 0
    entry["OthValue"] = p["otherValue"]
    entry["transMode"] = _int(p["transMode"])
    entry["transDistance"] = _int(p["transDistance"])
    entry["mainHsnCode"] = p["itemList"][0]["hsnCode"] if p["itemList"] else None
    entry["itemList"] = [{"itemNo": n, "productName": i["productName"], "productDesc": i["productDesc"],
                          "hsnCode": i["hsnCode"], "quantity": i["quantity"], "qtyUnit": i["qtyUnit"],
                          "taxableAmount": i["taxableAmount"], "sgstRate": i["sgstRate"], "cgstRate": i["cgstRate"],
                          "igstRate": i["igstRate"], "cessRate": 0, "cessNonAdvol": 0}
                         for n, i in enumerate(p["itemList"], start=1)]
    return entry


def bulk_json(ewbs) -> dict:
    return {"version": BULK_VERSION, "billLists": [bulk_entry(e) for e in ewbs]}


def irn_payload(ewb: EWayBill, irn: str) -> dict:
    """IRP 'generate e-way bill by IRN' request - Part B only, Part A comes from the IRN."""
    data = {"Irn": irn, "Distance": ewb.distance_km, "TransMode": ewb.trans_mode,
            "TransId": ewb.transporter_id, "TransName": ewb.transporter_name, "TransDocNo": ewb.trans_doc_no,
            "TransDocDt": _d(ewb.trans_doc_date) or None, "VehNo": ewb.vehicle_no, "VehType": ewb.vehicle_type}
    return {k: v for k, v in data.items() if v not in (None, "")}


# --- actions --------------------------------------------------------------------------

def _irn_of(ewb: EWayBill):
    if ewb.source_type != "invoice":
        return None
    invoice = Invoice.query.filter_by(tenant_id=ewb.tenant_id, id=ewb.source_id).first()
    if invoice and invoice.irn and not invoice.irn_cancelled_at:
        return invoice.irn
    return None


def _has_vehicle(ewb) -> bool:
    return bool(ewb.vehicle_no or ewb.trans_doc_no)


def _validity(ewb, start: datetime):
    return rules.eway_valid_upto(start, ewb.distance_km, ewb.vehicle_type == "O") if _has_vehicle(ewb) else None


def generate(tenant, user, ewb: EWayBill, http=None):
    if ewb.status != EWayBillStatus.DRAFT:
        raise EInvoiceError("This e-way bill was already generated.")
    _check(ewb)
    cred = credential_for(ewb.tenant_id, ewb.gstin_id)
    if cred is None:
        raise EInvoiceError("No API login is saved for this GSTIN - add one under e-Invoice settings, or download "
                            "the JSON, upload it on the e-way bill portal and record the number here.")
    irn = _irn_of(ewb)
    if irn:
        data = irn_payload(ewb, irn)
        result = call(cred, user, "ewb_by_irn", lambda p: p.ewb_by_irn(data), "eway_bill", ewb.id, http=http)
    else:
        data = api_payload(ewb)
        result = call(cred, user, "generate_ewb", lambda p: p.generate_ewb(data), "eway_bill", ewb.id, http=http)
    ewb.ewb_no = result.ewb_no
    ewb.ewb_date = result.ewb_date or utcnow()
    ewb.valid_upto = result.valid_upto or _validity(ewb, ewb.ewb_date)
    ewb.generated_via = "irn" if irn else "api"
    ewb.status = EWayBillStatus.GENERATED
    record_audit(user, "ewb_generated", tenant_id=ewb.tenant_id, entity_type="eway_bill", entity_id=ewb.id,
                 details={"ewb_no": ewb.ewb_no, "via": ewb.generated_via, "source": f"{ewb.source_type}:{ewb.source_id}"})
    return result


def record(user, ewb: EWayBill, ewb_no: str, ewb_date: str):
    """An e-way bill generated on the portal (bulk upload or by hand)."""
    if ewb.status != EWayBillStatus.DRAFT:
        raise EInvoiceError("This e-way bill already has a number.")
    ewb_no = re.sub(r"\s", "", ewb_no or "")
    if not EWB_NO.match(ewb_no):
        raise EInvoiceError("An e-way bill number is 12 digits.")
    when = parse_dt((ewb_date or "").replace("T", " ") + (":00" if len(ewb_date or "") == 16 else ""))
    if when is None:
        raise EInvoiceError("Enter the date and time the e-way bill was generated.")
    ewb.ewb_no, ewb.ewb_date = ewb_no, when
    ewb.valid_upto = _validity(ewb, when)
    ewb.generated_via = "portal"
    ewb.status = EWayBillStatus.GENERATED
    record_audit(user, "ewb_recorded", tenant_id=ewb.tenant_id, entity_type="eway_bill", entity_id=ewb.id,
                 details={"ewb_no": ewb_no})


def update_part_b(tenant, user, ewb: EWayBill, form, on_portal=False, http=None, now=None):
    if ewb.status != EWayBillStatus.GENERATED:
        raise EInvoiceError("Only a generated e-way bill's vehicle can be updated.")
    now = now or utcnow()
    if ewb.valid_upto and ewb.valid_upto < now:
        raise EInvoiceError("This e-way bill has expired - its vehicle can't be updated. It can be extended on the "
                            "portal, or a new one generated.")
    reason = form.get("reason_code") or ""
    if reason not in PART_B_REASONS:
        raise EInvoiceError("Choose why the vehicle is changing.")
    remark = (form.get("reason_rem") or PART_B_REASONS[reason]).strip()[:50]
    place = (form.get("from_place") or ewb.from_party.get("place") or "").strip()
    state = (form.get("from_state") or ewb.from_party.get("state_code") or "").strip()
    first_time = not _has_vehicle(ewb)
    _apply_part_b(ewb, {**form, "distance_km": str(ewb.distance_km)})
    if ewb.trans_mode == "1" and not (ewb.vehicle_no and VEHICLE.match(ewb.vehicle_no)):
        raise EInvoiceError("Enter the vehicle number, e.g. TN01AB1234.")
    if ewb.trans_mode != "1" and not ewb.trans_doc_no:
        raise EInvoiceError("Enter the transport document number.")
    valid = None
    if not on_portal:
        cred = credential_for(ewb.tenant_id, ewb.gstin_id)
        if cred is None:
            raise EInvoiceError("No API login is saved - update the vehicle on the portal, then record it here.")
        data = {"ewbNo": int(ewb.ewb_no), "vehicleNo": ewb.vehicle_no or "", "fromPlace": place,
                "fromState": _int(state), "reasonCode": reason, "reasonRem": remark,
                "transDocNo": ewb.trans_doc_no or "", "transDocDate": _d(ewb.trans_doc_date),
                "transMode": ewb.trans_mode}
        valid = call(cred, user, "update_part_b", lambda p: p.update_part_b(data), "eway_bill", ewb.id, http=http)
    if valid:
        ewb.valid_upto = valid
    elif first_time or ewb.valid_upto is None:
        # Validity runs from when Part B is first entered.
        ewb.valid_upto = _validity(ewb, now)
    record_audit(user, "ewb_part_b_updated", tenant_id=ewb.tenant_id, entity_type="eway_bill", entity_id=ewb.id,
                 details={"ewb_no": ewb.ewb_no, "vehicle_no": ewb.vehicle_no, "reason": PART_B_REASONS[reason],
                          "on_portal": on_portal})


def cancel(tenant, user, ewb: EWayBill, reason_code: str, remark: str, on_portal=False, http=None, now=None):
    remark = (remark or "").strip()[:50]
    if ewb.status == EWayBillStatus.DRAFT:
        # Never sent anywhere: cancelling it just closes the draft.
        if not remark:
            raise EInvoiceError("Add a reason.")
        ewb.status, ewb.cancelled_at, ewb.cancel_reason = EWayBillStatus.CANCELLED, utcnow(), f"Draft: {remark}"
        record_audit(user, "ewb_draft_cancelled", tenant_id=ewb.tenant_id, entity_type="eway_bill",
                     entity_id=ewb.id, details={"reason": remark})
        return
    if ewb.status != EWayBillStatus.GENERATED:
        raise EInvoiceError("This e-way bill is already cancelled.")
    if not rules.can_cancel_ewb(ewb, now):
        raise EInvoiceError(f"An e-way bill can only be cancelled within {EWAY_BILL_CANCEL_HOURS} hours of "
                            "generating it, and that time has passed.")
    if reason_code not in CANCEL_REASONS:
        raise EInvoiceError("Choose why the e-way bill is being cancelled.")
    if not remark:
        raise EInvoiceError("Add a remark.")
    when = None
    if not on_portal:
        cred = credential_for(ewb.tenant_id, ewb.gstin_id)
        if cred is None:
            raise EInvoiceError("No API login is saved - cancel it on the portal, then record that here.")
        when = call(cred, user, "cancel_ewb", lambda p: p.cancel_ewb(ewb.ewb_no, reason_code, remark),
                    "eway_bill", ewb.id, http=http)
    ewb.status = EWayBillStatus.CANCELLED
    ewb.cancelled_at = when or utcnow()
    ewb.cancel_reason = f"{CANCEL_REASONS[reason_code]}: {remark}"
    record_audit(user, "ewb_cancelled", tenant_id=ewb.tenant_id, entity_type="eway_bill", entity_id=ewb.id,
                 details={"ewb_no": ewb.ewb_no, "reason": ewb.cancel_reason, "on_portal": on_portal})


def local(dt) -> str:
    return dt.astimezone(IST).strftime("%d %b %Y %H:%M") if dt else ""
