"""Search finds records, not only screens: the search box (Ctrl K) asks
here for an invoice number, a customer, a supplier, an item, a serial
number, an order... Only the signed-in business's own records, only the
kinds whose module is on, a few of each."""

from __future__ import annotations

from flask import url_for

from app.extensions import db

PER_KIND = 5
MIN_LENGTH = 2


def _like(text: str) -> str:
    """The text as a LIKE pattern matching it anywhere, taken literally."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def records(tenant, query: str) -> list[dict]:
    """[{label, group, url, icon, detail}] for the owner's search box."""
    query = " ".join((query or "").split())[:60]
    if len(query) < MIN_LENGTH:
        return []
    like = _like(query)
    has = tenant.has_feature
    out: list[dict] = []

    def add(group, icon, rows):
        for label, detail, url in rows:
            out.append({"label": label, "group": group, "url": url, "icon": icon, "detail": detail or ""})

    def match(*columns):
        return db.or_(*[c.ilike(like, escape="\\") for c in columns])

    from app.models.customer import Customer
    from app.models.invoice import Invoice
    from app.models.note import CreditDebitNote
    from app.models.product import Product

    rows = Invoice.query.filter(Invoice.tenant_id == tenant.id, match(Invoice.invoice_number)) \
        .order_by(Invoice.id.desc()).limit(PER_KIND).all()
    add("Bill", "receipt-text", [(i.invoice_number, _party(i) + i.invoice_date.strftime("%d %b %Y"),
                                  url_for("invoicing.view_invoice", invoice_id=i.id)) for i in rows])

    rows = CreditDebitNote.query.filter(CreditDebitNote.tenant_id == tenant.id, match(CreditDebitNote.note_number)) \
        .order_by(CreditDebitNote.id.desc()).limit(PER_KIND).all()
    add("Note", "file-text", [(n.note_number, n.note_date.strftime("%d %b %Y"),
                               url_for("notes.view_note", note_id=n.id)) for n in rows])

    if has("pos"):
        from app.models.pos_bill import POSBill

        rows = POSBill.query.filter(POSBill.tenant_id == tenant.id, POSBill.bill_number.isnot(None),
                                    match(POSBill.bill_number)).order_by(POSBill.id.desc()).limit(PER_KIND).all()
        add("Counter bill", "monitor", [(b.bill_number, f"Rs. {b.grand_total:,.2f}",
                                         url_for("pos.view_receipt", bill_id=b.id)) for b in rows])

    if has("sales_documents"):
        from app.models.sales import SALES_DOCUMENT_LABELS, SalesDocument

        rows = SalesDocument.query.filter(SalesDocument.tenant_id == tenant.id, match(SalesDocument.number)) \
            .order_by(SalesDocument.id.desc()).limit(PER_KIND).all()
        add("Order or quote", "file-text", [(d.number, SALES_DOCUMENT_LABELS.get(d.kind, ""),
                                             url_for("sales.view_document", doc_id=d.id)) for d in rows])

    rows = Customer.query.filter(Customer.tenant_id == tenant.id,
                                 match(Customer.name, Customer.phone, Customer.gstin)) \
        .order_by(Customer.name).limit(PER_KIND).all()
    add("Customer", "users", [(c.name, " ".join(x for x in (c.phone, c.gstin) if x),
                               url_for("accounts.customer_ledger", customer_id=c.id) if has("receivables")
                               else url_for("customers.edit_customer", customer_id=c.id)) for c in rows])

    rows = Product.query.filter(Product.tenant_id == tenant.id, match(Product.name, Product.sku, Product.barcode)) \
        .order_by(Product.name).limit(PER_KIND).all()
    add("Item", "package", [(p.name, " ".join(x for x in (p.sku, p.barcode) if x),
                             url_for("products.edit_product", product_id=p.id)) for p in rows])

    if has("purchases"):
        from app.models.purchase import PurchaseBill, PurchaseOrder, Supplier

        rows = Supplier.query.filter(Supplier.tenant_id == tenant.id, match(Supplier.name, Supplier.gstin)) \
            .order_by(Supplier.name).limit(PER_KIND).all()
        add("Supplier", "truck", [(s.name, s.gstin or "", url_for("purchases.edit_supplier", supplier_id=s.id))
                                  for s in rows])
        rows = PurchaseBill.query.filter(PurchaseBill.tenant_id == tenant.id,
                                         match(PurchaseBill.number, PurchaseBill.supplier_invoice_no)) \
            .order_by(PurchaseBill.id.desc()).limit(PER_KIND).all()
        add("Purchase bill", "truck", [(b.number, f"{b.supplier.name} {b.supplier_invoice_no or ''}".strip(),
                                        url_for("purchases.view_bill", bill_id=b.id)) for b in rows])
        rows = PurchaseOrder.query.filter(PurchaseOrder.tenant_id == tenant.id, match(PurchaseOrder.number)) \
            .order_by(PurchaseOrder.id.desc()).limit(PER_KIND).all()
        add("Purchase order", "clipboard-list", [(o.number, o.supplier.name,
                                                  url_for("purchases.view_order", order_id=o.id)) for o in rows])

    if has("serial_imei") and has("inventory"):
        from app.models.inventory import ProductSerial

        rows = ProductSerial.query.filter(ProductSerial.tenant_id == tenant.id, match(ProductSerial.serial_no)) \
            .order_by(ProductSerial.id.desc()).limit(PER_KIND).all()
        add("Serial number", "scan-line", [(s.serial_no, f"{s.product.name} - {s.status.value.replace('_', ' ')}",
                                            url_for("inventory.serials", q=s.serial_no)) for s in rows])
    return out


def _party(invoice) -> str:
    customer = getattr(invoice, "customer", None)
    return (customer.name + " - ") if customer is not None else ""
