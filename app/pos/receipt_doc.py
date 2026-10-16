"""A POS bill as a plain receipt document - the same content as the PDF
receipt (pos/receipt_print.html), as data, for printers that take text:

  - the Android app prints it straight to a Bluetooth / USB thermal
    printer in ESC/POS (app/static/js/escpos.js turns this into bytes)
  - an offline bill builds the very same shape on the device
    (app/static/js/pos_offline.js), so both print alike.

Shape: {"header": [...], "copy_label", "number", "title", "meta": [[k, v]],
"lines": [{"desc", "details": [...], "qty", "amount", "discount"}],
"totals": [[k, v]], "grand": [k, v], "payments": [[k, v]], "status",
"footer": [...], "qr", "open_drawer"}"""

from app.models.pos_bill import PaymentMode


def _money(value) -> str:
    return f"{value or 0:.2f}"


def _qty(value) -> str:
    from decimal import Decimal

    return f"{Decimal(value).normalize():f}" if value is not None else ""


def receipt_doc(bill, firm_name: str, copy_label: str | None = None) -> dict:
    from app.einvoice.providers.common import IST
    from app.utils.module_settings import ORDER_TYPES

    header = [firm_name]
    if bill.gstin and bill.gstin.gstin:
        header.append(f"GSTIN: {bill.gstin.gstin}")
    meta = []
    if bill.completed_at:
        meta.append(["Date", bill.completed_at.astimezone(IST).strftime("%d %b %Y %H:%M")])
    snap = bill.customer_snapshot or {}
    if snap.get("name"):
        meta.append(["Customer", snap["name"]])
    if snap.get("gstin"):
        meta.append(["Cust. GSTIN", snap["gstin"]])
    if bill.order_type:
        order = ORDER_TYPES.get(bill.order_type, bill.order_type)
        meta.append(["Order", order + (f" - Table {bill.table.name}" if bill.table else "")])
    if bill.salesman:
        meta.append(["Salesman", bill.salesman.name])
    if bill.cashier:
        meta.append(["Cashier", bill.cashier.name])
    if bill.device:
        meta.append(["Counter", f"{bill.device.code} (billed offline)"])
    if bill.prescription:
        rx = bill.prescription
        meta.append(["Doctor", rx.get("doctor", "") + (f" ({rx['doctor_reg_no']})" if rx.get("doctor_reg_no") else "")])
        meta.append(["Patient", rx.get("patient", "")])

    lines = []
    for line in bill.lines:
        details = []
        if line.serial_numbers:
            details.append("S/N: " + ", ".join(line.serial_numbers))
        if line.note:
            details.append(f"* {line.note}")
        if line.jewellery:
            j = line.jewellery
            details.append(f"{j.get('purity')} {j.get('metal')}, net {j.get('net_weight')} g @ {j.get('rate')}/g")
        discount = None
        if line.line_discount_kind:
            discount = (f"less {_qty(line.line_discount_value)}%" if line.line_discount_kind == "percent"
                        else f"less Rs. {_money(line.line_discount_value)}") + " discount"
        lines.append({"desc": line.description, "details": details,
                      "qty": f"{_qty(line.qty)} {line.unit} x {_money(line.rate)}",
                      "amount": _money(line.line_total), "discount": discount})

    totals = []
    if bill.total_discount:
        totals.append(["Discount", "-" + _money(bill.total_discount)])
    if not bill.is_margin_scheme:
        totals.append(["Taxable value", _money(bill.total_taxable_value)])
        for label, value in (("CGST", bill.total_cgst), ("SGST", bill.total_sgst), ("IGST", bill.total_igst)):
            if value:
                totals.append([label, _money(value)])
    totals.append(["Round off", _money(bill.round_off)])

    payments = []
    if len(bill.payments) > 1:
        payments = [[p.label, _money(p.amount)] for p in bill.payments]
    else:
        label = bill.payments[0].label if bill.payments else (bill.payment_mode.value if bill.payment_mode else "-")
        payments.append(["Paid via", label.upper()])
    if bill.cash_tendered is not None:
        payments.append(["Cash received", _money(bill.cash_tendered)])
        payments.append(["Change", _money(bill.change_due)])

    status = None
    if bill.status.value == "voided":
        status = "CANCELLED"
    elif bill.status.value == "refunded":
        status = "REFUNDED"
    footer = []
    if bill.is_margin_scheme:
        from app.utils.margin import declaration_for

        footer.append(declaration_for(bill.gstin.tenant if bill.gstin else None))
    footer.append("Thank you, visit again!")
    return {
        "header": header, "copy_label": copy_label, "number": bill.bill_number,
        "title": "INVOICE - MARGIN SCHEME" if bill.is_margin_scheme else None,
        "meta": meta, "lines": lines, "totals": totals, "grand": ["TOTAL", _money(bill.grand_total)],
        "payments": payments, "status": status, "footer": footer, "qr": None,
        # Kick the cash drawer only when cash changed hands, on the original print.
        "open_drawer": copy_label is None and any(p.mode == PaymentMode.CASH for p in bill.payments),
    }
