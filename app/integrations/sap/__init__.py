"""The link to a client's SAP S/4HANA: purchase orders are fetched from SAP
(read only - ARFA never changes SAP), so a scanned supplier bill can find
the order it belongs to (app/purchases/matching.py).

  client.py   talks to SAP's Purchase Order OData service (V2 or V4)
  sync.py     stages what was fetched, maps SAP's codes, makes the orders
  routes.py   Setup > SAP link: the connection, Fetch now, the "to map" list
"""

from flask import Blueprint

sap_bp = Blueprint("sap", __name__, url_prefix="/sap")

from app.integrations.sap import routes  # noqa: E402,F401
