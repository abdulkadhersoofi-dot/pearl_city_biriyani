"""Does a client hold records in a module? (packages and powers, stage 3)

When a package takes a module away from a client who has records in it,
the module turns read-only instead of vanishing: old records open, print
and export, nothing new can be saved. Only modules with records of their
own are listed; the rest (a dashboard, a counter option) simply switch off.

Each entry also names the module's own page, where Settings > Modules
links "Open records" for a read-only module.
"""

from app.extensions import db


def _model(name):
    import app.models as models

    return getattr(models, name)


# module -> (models whose rows mean "this client has records here", home endpoint)
MODULE_RECORDS = {
    "pos": (("POSBill",), "pos.day_book"),
    "inventory": (("StockMovement",), "inventory.index"),
    "purchases": (("PurchaseBill",), "purchases.bills"),
    "expenses": (("Expense",), "expenses.index"),
    "receivables": (("CustomerReceipt", "RefundVoucher"), "accounts.receivables_index"),
    "payables": (("SupplierPayment",), "accounts.payables_index"),
    "sales_documents": (("SalesDocument",), "sales.documents"),
    "recurring_invoices": (("RecurringInvoice",), "sales.recurring"),
    "job_cards": (("JobCard",), "jobs.index"),
    "jewellery": (("OldGoldPurchase",), "jewellery.old_gold_list"),
    "loyalty": (("LoyaltyEntry",), "loyalty.index"),
    "restaurant_mode": (("KitchenTicket",), "restaurant.tables"),
    "eway_bill": (("EWayBill",), "einvoice.eway_bills"),
    "batch_expiry": (("ProductBatch",), "inventory.expiry"),
    "serial_imei": (("ProductSerial",), "inventory.serials"),
    "wholesale": (("Salesman", "CustomerGroup"), "wholesale.index"),
    "multi_price_list": (("PriceList",), "products.price_lists"),
}


def has_records(tenant, key: str) -> bool:
    """True when the client has at least one record of this module."""
    entry = MODULE_RECORDS.get(key)
    if entry is None or tenant is None or tenant.id is None:
        return False
    for name in entry[0]:
        model = _model(name)
        if db.session.query(model.query.filter(model.tenant_id == tenant.id).exists()).scalar():
            return True
    return False


def home_endpoint(key: str):
    entry = MODULE_RECORDS.get(key)
    return entry[1] if entry else None
