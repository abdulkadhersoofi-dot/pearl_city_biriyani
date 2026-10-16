"""The first-run checklist a new Client Admin sees (Phase 10): business
profile -> GSTIN -> first item -> first customer -> first bill -> printer
test. Each step ticks itself from the client's own data; the Client Admin
can close the list at any time. Clients that existed before this were
marked dismissed by the migration, so nobody already set up sees it.

State lives in Tenant.first_run: {"dismissed", "profile_saved",
"printer_tested"}."""

from flask import url_for

from app.models.customer import Customer
from app.models.invoice import Invoice
from app.models.pos_bill import POSBill
from app.models.product import Product
from app.models.tenant import RegistrationType


def _state(tenant) -> dict:
    return dict(tenant.first_run or {})


def mark(tenant, key: str, value=True) -> None:
    tenant.first_run = {**_state(tenant), key: value}  # reassigned: JSON columns don't track in-place changes


def checklist(tenant) -> dict | None:
    """The steps with done / not done, or None once dismissed."""
    state = _state(tenant)
    if state.get("dismissed"):
        return None
    gstin = tenant.gstins.filter_by(is_active=True).first()
    registered = tenant.registration_type == RegistrationType.REGULAR
    steps = [
        {"key": "profile", "label": "Business profile",
         "help": "Your business type, logo and how bills print.",
         "done": bool(state.get("profile_saved")), "url": url_for("settings.index"), "action": "Open settings"},
        {"key": "gstin", "label": "GSTIN" if registered else "Business location",
         "help": "Added by your accountant's office when your account was set up - ask them if it's missing.",
         "done": bool(gstin and (gstin.gstin or not registered)), "url": None, "action": None},
        {"key": "product", "label": "First item", "help": "Something you sell, with its price and GST rate.",
         "done": Product.query.filter_by(tenant_id=tenant.id).first() is not None,
         "url": url_for("products.new_product"), "action": "Add an item"},
        {"key": "customer", "label": "First customer", "help": "A regular buyer - with their GSTIN for B2B bills.",
         "done": Customer.query.filter_by(tenant_id=tenant.id).first() is not None,
         "url": url_for("customers.new_customer"), "action": "Add a customer"},
        {"key": "bill", "label": "First bill", "help": "Ring up a sale on the POS, or raise an invoice.",
         "done": (POSBill.query.filter(POSBill.tenant_id == tenant.id, POSBill.bill_number.isnot(None)).first()
                  is not None) or Invoice.query.filter_by(tenant_id=tenant.id).first() is not None,
         "url": url_for("pos.terminal"), "action": "Open the POS"},
        {"key": "printer", "label": "Printer test", "help": "Print a test receipt to check the paper size.",
         "done": bool(state.get("printer_tested")), "url": url_for("settings.printer_test"), "action": "Print a test"},
    ]
    done = sum(1 for s in steps if s["done"])
    return {"steps": steps, "done": done, "total": len(steps), "complete": done == len(steps)}
