"""Quotations / estimates, proforma invoices, sales orders and delivery
challans - each turned into an invoice in one click (feature
"sales_documents") - and recurring invoices (feature "recurring_invoices")."""

from flask import Blueprint

sales_bp = Blueprint("sales", __name__, url_prefix="/sales", template_folder="../templates/sales")

from app.sales import routes  # noqa: E402,F401
