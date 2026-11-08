"""Warehouse dispatch by serial scan (module "warehouse_dispatch") - the
website side: the warehouse computer's Ready to invoice queue, the App
logins page, supervising claims, receiving purchases and the reports.
dispatch.py is the service everything goes through (the ARFA Warehouse
app's API in app/warehouse_api too); notify.py sends the pushes."""

from flask import Blueprint

warehouse_bp = Blueprint("warehouse", __name__, url_prefix="/warehouse")

from app.warehouse import routes  # noqa: E402,F401
