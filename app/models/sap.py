"""The link to a client's SAP S/4HANA (app/integrations/sap): where it is,
what SAP's vendor and material codes are called in ARFA, the purchase orders
as SAP sent them, and a log of every call made."""

from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin, utcnow

SAP_API_V2 = "v2"        # API_PURCHASEORDER_PROCESS_SRV (OData V2)
SAP_API_V4 = "v4"        # API_PURCHASEORDER_2 (OData V4), its successor
SAP_AUTH_BASIC = "basic"
SAP_AUTH_OAUTH = "oauth"

MAP_SUPPLIER = "supplier"
MAP_MATERIAL = "material"

ORDER_WAITING = "waiting"        # its supplier isn't mapped yet
ORDER_IMPORTED = "imported"      # a purchase order in ARFA
ORDER_SKIPPED = "skipped"        # deleted in SAP before it ever came in, or has no items


class SapConnection(db.Model, TenantScopedMixin, TimestampMixin):
    """One per business."""

    __tablename__ = "sap_connections"
    __table_args__ = (db.UniqueConstraint("tenant_id", name="uq_sap_connections_tenant"),)

    id = db.Column(db.Integer, primary_key=True)
    # The registration SAP's orders are bought under.
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id", name="fk_sap_connections_gstin"))
    base_url = db.Column(db.String(255), nullable=False)
    api = db.Column(db.String(10), nullable=False, default=SAP_API_V2)
    auth = db.Column(db.String(10), nullable=False, default=SAP_AUTH_BASIC)
    username = db.Column(db.String(120))        # the technical user, or the OAuth client id
    secrets = db.Column(db.Text)                # sealed (app/einvoice/vault.py): the password or client secret
    token_url = db.Column(db.String(255))       # OAuth only
    sap_client = db.Column(db.String(3))        # the SAP client number, when the system needs it
    company_code = db.Column(db.String(10))     # only this company code's orders (optional)
    purchasing_org = db.Column(db.String(10))
    plant = db.Column(db.String(10))            # only items for this plant (optional)
    since = db.Column(db.Date)                  # orders dated before this are left in SAP
    lookup_partners = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    enabled = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    # Orders changed in SAP after this moment are fetched next.
    cursor = db.Column(db.DateTime(timezone=True))
    last_pull_at = db.Column(db.DateTime(timezone=True))
    last_pull_ok = db.Column(db.Boolean)
    last_pull_message = db.Column(db.String(500))
    last_pull_counts = db.Column(db.JSON)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_sap_connections_updated_by"))

    gstin = db.relationship("Gstin")


class SapMapping(db.Model, TenantScopedMixin, TimestampMixin):
    """What an SAP vendor code or material number is in ARFA. Without a
    supplier or product it is waiting to be mapped by a person."""

    __tablename__ = "sap_mappings"
    __table_args__ = (db.UniqueConstraint("tenant_id", "kind", "sap_code", name="uq_sap_mappings_code"),)

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(10), nullable=False)
    sap_code = db.Column(db.String(40), nullable=False)
    sap_name = db.Column(db.String(255))
    sap_gstin = db.Column(db.String(15))
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id", name="fk_sap_mappings_supplier"))
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", name="fk_sap_mappings_product"))
    how = db.Column(db.String(20))              # gstin | code | sku | person
    mapped_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_sap_mappings_mapped_by"))

    supplier = db.relationship("Supplier")
    product = db.relationship("Product")

    @property
    def mapped(self) -> bool:
        return bool(self.supplier_id if self.kind == MAP_SUPPLIER else self.product_id)


class SapOrder(db.Model, TenantScopedMixin, TimestampMixin):
    """A purchase order as SAP sent it last. Kept whether or not it could
    be brought into ARFA yet, so nothing fetched is lost while a supplier
    waits to be mapped."""

    __tablename__ = "sap_orders"
    __table_args__ = (db.UniqueConstraint("tenant_id", "number", name="uq_sap_orders_number"),)

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(20), nullable=False)
    supplier_code = db.Column(db.String(40))
    order_date = db.Column(db.Date)
    changed_at = db.Column(db.DateTime(timezone=True))
    deleted = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    payload = db.Column(db.JSON, nullable=False)      # the order, normalised (client.normalise)
    status = db.Column(db.String(12), nullable=False, default=ORDER_WAITING)
    note = db.Column(db.String(300))
    purchase_order_id = db.Column(db.Integer, db.ForeignKey("purchase_orders.id", name="fk_sap_orders_po"), index=True)

    purchase_order = db.relationship("PurchaseOrder")


class SapCall(db.Model, TenantScopedMixin):
    """Every request ARFA made to the client's SAP: when, what, how it went.
    Never the login, never a response body."""

    __tablename__ = "sap_calls"

    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_sap_calls_user"))   # empty: the schedule
    purpose = db.Column(db.String(20), nullable=False)       # orders | supplier | test | token
    path = db.Column(db.String(300), nullable=False)
    status = db.Column(db.Integer)
    ms = db.Column(db.Integer)
    records = db.Column(db.Integer)
    error = db.Column(db.String(300))
