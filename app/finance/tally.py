"""TallyPrime import files from app.finance.journal - a ZIP with
masters.xml (ledgers; import first) and vouchers.xml (Gateway of Tally ->
Import -> Masters / Transactions). Plain XML built with ElementTree, so
names with &, < or quotes are escaped properly.

Tally's sign convention: a debit is a negative AMOUNT with
ISDEEMEDPOSITIVE = Yes; a credit is positive with ISDEEMEDPOSITIVE = No.
Voucher types used are Tally's own predefined ones (Sales, Purchase,
Credit Note, Debit Note, Receipt, Payment, Journal), so nothing has to be
created in the company first."""

import zipfile
from datetime import date
from io import BytesIO
from xml.etree import ElementTree as ET

from app.finance.journal import build


def _envelope(report: str, company: str):
    env = ET.Element("ENVELOPE")
    header = ET.SubElement(env, "HEADER")
    ET.SubElement(header, "TALLYREQUEST").text = "Import Data"
    body = ET.SubElement(env, "BODY")
    imp = ET.SubElement(body, "IMPORTDATA")
    desc = ET.SubElement(imp, "REQUESTDESC")
    ET.SubElement(desc, "REPORTNAME").text = report
    statics = ET.SubElement(desc, "STATICVARIABLES")
    ET.SubElement(statics, "SVCURRENTCOMPANY").text = company
    data = ET.SubElement(imp, "REQUESTDATA")
    return env, data


def _bytes(env) -> bytes:
    ET.indent(env)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(env, encoding="utf-8")


def masters_xml(ledgers, company: str) -> bytes:
    env, data = _envelope("All Masters", company)
    for l in ledgers:
        msg = ET.SubElement(data, "TALLYMESSAGE", {"xmlns:UDF": "TallyUDF"})
        led = ET.SubElement(msg, "LEDGER", {"NAME": l.name, "ACTION": "Create"})
        names = ET.SubElement(led, "NAME.LIST")
        ET.SubElement(names, "NAME").text = l.name
        ET.SubElement(led, "PARENT").text = l.group
        if l.group in ("Sundry Debtors", "Sundry Creditors"):
            ET.SubElement(led, "ISBILLWISEON").text = "Yes"
            if l.gstin:
                ET.SubElement(led, "GSTREGISTRATIONTYPE").text = "Regular"
                ET.SubElement(led, "PARTYGSTIN").text = l.gstin
            else:
                ET.SubElement(led, "GSTREGISTRATIONTYPE").text = "Unregistered/Consumer"
            if l.state:
                ET.SubElement(led, "LEDSTATENAME").text = l.state
        if l.tax_head:
            ET.SubElement(led, "TAXTYPE").text = "GST"
            ET.SubElement(led, "GSTDUTYHEAD").text = l.tax_head
    return _bytes(env)


def _amount(entry) -> str:
    return f"-{entry.debit:.2f}" if entry.debit else f"{entry.credit:.2f}"


def vouchers_xml(vouchers, company: str) -> bytes:
    env, data = _envelope("Vouchers", company)
    for v in vouchers:
        msg = ET.SubElement(data, "TALLYMESSAGE", {"xmlns:UDF": "TallyUDF"})
        vch = ET.SubElement(msg, "VOUCHER", {"VCHTYPE": v.vch_type, "ACTION": "Create"})
        ET.SubElement(vch, "DATE").text = v.on.strftime("%Y%m%d")
        ET.SubElement(vch, "VOUCHERTYPENAME").text = v.vch_type
        ET.SubElement(vch, "VOUCHERNUMBER").text = v.number
        if v.party:
            ET.SubElement(vch, "PARTYLEDGERNAME").text = v.party
        ET.SubElement(vch, "NARRATION").text = v.narration
        for e in v.entries:
            le = ET.SubElement(vch, "ALLLEDGERENTRIES.LIST")
            ET.SubElement(le, "LEDGERNAME").text = e.ledger
            ET.SubElement(le, "ISDEEMEDPOSITIVE").text = "Yes" if e.debit else "No"
            ET.SubElement(le, "AMOUNT").text = _amount(e)
    return _bytes(env)


def tally_zip(tenant, start: date, end: date) -> BytesIO:
    ledgers, vouchers = build(tenant, start, end)
    company = tenant.legal_name
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("1_masters.xml", masters_xml(ledgers, company))
        zf.writestr("2_vouchers.xml", vouchers_xml(vouchers, company))
        zf.writestr("README.txt", (
            f"Tally import for {company}, {start.isoformat()} to {end.isoformat()}.\n"
            "In TallyPrime, open the company, then Gateway of Tally -> Import -> Masters and choose 1_masters.xml,\n"
            "then Import -> Transactions and choose 2_vouchers.xml. Each voucher balances; any paisa from\n"
            "rounding is on the 'Round Off' ledger. Ledgers are created under Tally's standard groups.\n"
            f"{len(ledgers)} ledgers, {len(vouchers)} vouchers.\n"))
    buf.seek(0)
    return buf
