"""INV-01 - the e-invoice JSON the IRP takes (schema version 1.1, the one in
use on the NIC IRP and its offline / bulk-upload tool), built from an
invoice or credit / debit note, and checked here before it is sent so
the user sees a plain message instead of an IRP error code.

Amount rules the IRP checks, and so do we (to the rupee):
  item:  TotAmt = UnitPrice x Qty, AssAmt = TotAmt - Discount,
         TotItemVal = AssAmt + GST
  total: AssVal = sum of AssAmt, TotInvVal = AssVal + GST + RndOffAmt
Discount on a line is always the pre-tax discount (app.utils.gst)."""

import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from app.einvoice.rules import supply_type
from app.models.invoice import Invoice
from app.models.invoice_series import DocumentType
from app.models.note import CreditDebitNote
from app.utils.gst import GSTIN_REGEX
from app.utils.gst_config import GST_RATE_SLABS, UQC_CODES

VERSION = "1.1"
DOC_NO = re.compile(r"^[A-Z1-9][A-Z0-9/-]{0,15}$")
PIN = re.compile(r"^[1-9][0-9]{5}$")
HSN = re.compile(r"^[0-9]{4}([0-9]{2}){0,2}$")
EXPORT_STATE = "96"  # "other country" - place of supply and state code for exports
EXPORT_PIN = 999999
TOLERANCE = Decimal("1.00")


class EInvoiceDataError(Exception):
    """The document can't be e-invoiced as it stands - each problem in plain words."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _n(value, places=2):
    d = Decimal(value or 0).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return float(d)


def _date(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _clean(text, limit):
    return re.sub(r"\s+", " ", (text or "")).strip()[:limit] or None


def _unit(unit: str) -> str:
    u = (unit or "").strip().upper()
    return u if u in UQC_CODES else "OTH"


def _seller(tenant, gstin) -> dict:
    return {
        "Gstin": gstin.gstin, "LglNm": _clean(tenant.legal_name, 100), "TrdNm": _clean(tenant.trade_name, 100),
        "Addr1": _clean(gstin.registered_address, 100), "Loc": _clean(gstin.city, 50),
        "Pin": int(gstin.pincode) if (gstin.pincode or "").isdigit() else None, "Stcd": gstin.state_code,
    }


def _buyer(snapshot: dict, pos: str, kind: str) -> dict:
    if kind.startswith("EXP"):
        return {"Gstin": "URP", "LglNm": _clean(snapshot.get("name"), 100), "Pos": EXPORT_STATE,
                "Addr1": _clean(snapshot.get("address_line1"), 100) or "Outside India",
                "Loc": _clean(snapshot.get("city"), 50) or "Outside India", "Pin": EXPORT_PIN, "Stcd": EXPORT_STATE}
    pin = snapshot.get("pincode") or ""
    return {
        "Gstin": (snapshot.get("gstin") or "").strip().upper() or None, "LglNm": _clean(snapshot.get("name"), 100),
        "Pos": pos, "Addr1": _clean(snapshot.get("address_line1"), 100), "Addr2": _clean(snapshot.get("address_line2"), 100),
        "Loc": _clean(snapshot.get("city"), 50), "Pin": int(pin) if str(pin).isdigit() else None,
        "Stcd": snapshot.get("state_code"),
    }


def _items(lines, rcm: bool) -> list[dict]:
    items = []
    for i, line in enumerate(lines, start=1):
        qty = Decimal(line.qty)
        assessable = Decimal(line.taxable_value)
        discount = Decimal(line.discount_amount or 0)
        gross = assessable + discount
        cgst = Decimal(getattr(line, "rcgst_amount", 0) or 0) if rcm else Decimal(line.cgst_amount)
        sgst = Decimal(getattr(line, "rsgst_amount", 0) or 0) if rcm else Decimal(line.sgst_amount)
        igst = Decimal(getattr(line, "rigst_amount", 0) or 0) if rcm else Decimal(line.igst_amount)
        hsn = (line.hsn_or_sac_code or "").strip()
        items.append({
            "SlNo": str(i), "PrdDesc": _clean(line.description, 300), "IsServc": "Y" if hsn.startswith("99") else "N",
            "HsnCd": hsn, "Qty": _n(qty, 3), "Unit": _unit(line.unit),
            "UnitPrice": _n(gross / qty if qty else 0, 3), "TotAmt": _n(gross), "Discount": _n(discount),
            "AssAmt": _n(assessable), "GstRt": _n(line.gst_rate, 3), "IgstAmt": _n(igst), "CgstAmt": _n(cgst),
            "SgstAmt": _n(sgst), "CesRt": 0, "CesAmt": 0, "CesNonAdvlAmt": 0, "StateCesRt": 0, "StateCesAmt": 0,
            "StateCesNonAdvlAmt": 0, "OthChrg": 0, "TotItemVal": _n(assessable + cgst + sgst + igst),
        })
    return items


def _values(doc, items, rcm: bool) -> dict:
    ass = sum((Decimal(str(i["AssAmt"])) for i in items), Decimal(0))
    cgst = sum((Decimal(str(i["CgstAmt"])) for i in items), Decimal(0))
    sgst = sum((Decimal(str(i["SgstAmt"])) for i in items), Decimal(0))
    igst = sum((Decimal(str(i["IgstAmt"])) for i in items), Decimal(0))
    total = ass + cgst + sgst + igst + Decimal(doc.round_off or 0)
    return {"AssVal": _n(ass), "CgstVal": _n(cgst), "SgstVal": _n(sgst), "IgstVal": _n(igst), "CesVal": 0,
            "StCesVal": 0, "Discount": 0, "OthChrg": 0, "RndOffAmt": _n(doc.round_off), "TotInvVal": _n(total)}


def build(tenant, doc) -> dict:
    """INV-01 JSON (as a dict) for an invoice or a credit / debit note."""
    if isinstance(doc, Invoice):
        invoice, typ, number = doc, "INV", doc.invoice_number
    elif isinstance(doc, CreditDebitNote):
        invoice = doc.original_invoice
        typ = "CRN" if doc.document_type == DocumentType.CREDIT_NOTE else "DBN"
        number = doc.note_number
    else:
        raise TypeError("Only invoices and credit / debit notes are e-invoiced.")
    kind = supply_type(invoice) if invoice is not None else None
    if kind is None:
        raise EInvoiceDataError(["This document doesn't need an IRN - only B2B and export invoices and their "
                                 "notes do."])
    rcm = invoice.document_type == DocumentType.RCM_INVOICE
    items = _items(doc.lines, rcm)
    payload = {
        "Version": VERSION,
        "TranDtls": {"TaxSch": "GST", "SupTyp": kind, "RegRev": "Y" if rcm else "N", "IgstOnIntra": "N"},
        "DocDtls": {"Typ": typ, "No": number, "Dt": _date(doc.invoice_date if typ == "INV" else doc.note_date)},
        "SellerDtls": _seller(tenant, doc.gstin),
        "BuyerDtls": _buyer(doc.customer_snapshot or {}, doc.place_of_supply_state_code, kind),
        "ItemList": items,
        "ValDtls": _values(doc, items, rcm),
    }
    if typ != "INV":
        payload["RefDtls"] = {"PrecDocDtls": [{"InvNo": invoice.invoice_number, "InvDt": _date(invoice.invoice_date)}]}
    return _drop_empty(payload)


def _drop_empty(value):
    if isinstance(value, dict):
        return {k: _drop_empty(v) for k, v in value.items() if v not in (None, "")}
    if isinstance(value, list):
        return [_drop_empty(v) for v in value]
    return value


def validate(payload: dict, today: date | None = None) -> list[str]:
    """Problems that would make the IRP refuse this, in plain words."""
    today = today or date.today()
    problems = []
    doc, seller, buyer = payload["DocDtls"], payload["SellerDtls"], payload["BuyerDtls"]
    if not DOC_NO.match(doc["No"]):
        problems.append(f"The document number '{doc['No']}' won't be accepted: the IRP allows up to 16 characters - "
                        "capital letters, digits, / and -, not starting with 0, / or -.")
    d = date(int(doc["Dt"][6:]), int(doc["Dt"][3:5]), int(doc["Dt"][:2]))
    if d > today:
        problems.append("The document is dated in the future.")
    if not re.match(GSTIN_REGEX, seller.get("Gstin") or ""):
        problems.append("Your GSTIN is missing or not valid.")
    if not seller.get("Addr1") or not seller.get("Loc") or not seller.get("Pin") or not PIN.match(str(seller.get("Pin"))):
        problems.append("Your business address, city and 6-digit PIN code are needed - fill them in under "
                        "e-Invoice settings.")
    if not seller.get("LglNm") or len(seller["LglNm"]) < 3:
        problems.append("Your legal name must be at least 3 characters.")
    if buyer.get("Gstin") != "URP":
        if not re.match(GSTIN_REGEX, buyer.get("Gstin") or ""):
            problems.append("The customer's GSTIN is missing or not valid.")
        if not buyer.get("Addr1") or not buyer.get("Loc"):
            problems.append("The customer's address and city are needed - edit the customer, then make the "
                            "document again or fill them in before generating.")
        if not buyer.get("Pin") or not PIN.match(str(buyer["Pin"])):
            problems.append("The customer's 6-digit PIN code is needed.")
        if not buyer.get("Stcd"):
            problems.append("The customer's state is needed.")
    if not buyer.get("LglNm") or len(buyer["LglNm"]) < 3:
        problems.append("The customer's name must be at least 3 characters.")
    allowed_rates = {float(r) for r in GST_RATE_SLABS}
    for item in payload["ItemList"]:
        n = item["SlNo"]
        if not HSN.match(item.get("HsnCd") or ""):
            problems.append(f"Line {n}: an HSN / SAC code of 4, 6 or 8 digits is needed.")
        if item["GstRt"] not in allowed_rates:
            problems.append(f"Line {n}: {item['GstRt']}% is not a GST rate.")
        if item["Qty"] <= 0 and item["IsServc"] == "N":
            problems.append(f"Line {n}: quantity must be more than 0.")
        tot, ass = Decimal(str(item["TotAmt"])), Decimal(str(item["AssAmt"]))
        if abs(Decimal(str(item["UnitPrice"])) * Decimal(str(item["Qty"])) - tot) > TOLERANCE:
            problems.append(f"Line {n}: price x quantity doesn't match the amount.")
        if abs(tot - Decimal(str(item["Discount"])) - ass) > TOLERANCE:
            problems.append(f"Line {n}: amount less discount doesn't match the taxable value.")
        tax = sum(Decimal(str(item[k])) for k in ("IgstAmt", "CgstAmt", "SgstAmt"))
        if abs(ass + tax - Decimal(str(item["TotItemVal"]))) > TOLERANCE:
            problems.append(f"Line {n}: the line total doesn't add up.")
        if item["IgstAmt"] and (item["CgstAmt"] or item["SgstAmt"]):
            problems.append(f"Line {n}: a line can't have both IGST and CGST/SGST.")
    val = payload["ValDtls"]
    ass_total = sum(Decimal(str(i["AssAmt"])) for i in payload["ItemList"])
    if abs(ass_total - Decimal(str(val["AssVal"]))) > TOLERANCE:
        problems.append("The taxable total doesn't match the lines.")
    gst_total = sum(Decimal(str(val[k])) for k in ("CgstVal", "SgstVal", "IgstVal"))
    if abs(Decimal(str(val["AssVal"])) + gst_total + Decimal(str(val.get("RndOffAmt", 0)))
           - Decimal(str(val["TotInvVal"]))) > TOLERANCE:
        problems.append("The invoice total doesn't add up.")
    if not payload["ItemList"]:
        problems.append("The document has no lines.")
    return problems


def payload_for(tenant, doc) -> dict:
    """Built and checked - raises EInvoiceDataError with every problem."""
    payload = build(tenant, doc)
    problems = validate(payload)
    if problems:
        raise EInvoiceDataError(problems)
    return payload
