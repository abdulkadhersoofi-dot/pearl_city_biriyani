"""e-Invoice (IRN) and e-Way Bill records (Phase 8, flags einvoice / eway_bill).

  - EInvoiceCredential: per GSTIN, which provider to call (NIC direct -
    sandbox or production - or a GSP) and its login. Every secret is
    stored encrypted (app.einvoice.vault); only the user name is plain.
  - GovApiCall: one row per call to the IRP / e-way bill system - action,
    request id, success or the error code and message, and the plain-
    language explanation shown to the user. Never edited or deleted.
  - EWayBill: an e-way bill for an invoice, delivery challan or stock
    transfer - Part A from the document, Part B (vehicle) from the user;
    generated through the API or recorded after a bulk upload on the
    portal. Cancelled with a reason, never deleted.

The IRN itself lives on the document (Invoice / CreditDebitNote: irn,
irn_ack_number, irn_ack_date, qr_code_data = the signed QR string)."""

import enum

from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin, utcnow


class EInvoiceCredential(db.Model, TenantScopedMixin, TimestampMixin):
    __tablename__ = "einvoice_credentials"
    __table_args__ = (db.UniqueConstraint("gstin_id", name="uq_einvoice_credentials_gstin"),)

    id = db.Column(db.Integer, primary_key=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id", name="fk_einvoice_credentials_gstin"), nullable=False)
    provider = db.Column(db.String(30), nullable=False)  # see app.einvoice.providers.PROVIDERS
    username = db.Column(db.String(100), nullable=False)
    secrets = db.Column(db.Text, nullable=False)  # Fernet token of a JSON object
    session = db.Column(db.Text)  # Fernet token: auth token / session key from the last login
    token_expiry = db.Column(db.DateTime(timezone=True))
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_einvoice_credentials_updated_by"))

    gstin = db.relationship("Gstin")


class GovApiCall(db.Model, TenantScopedMixin):
    __tablename__ = "gov_api_calls"

    id = db.Column(db.Integer, primary_key=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id", name="fk_gov_api_calls_gstin"))
    provider = db.Column(db.String(30), nullable=False)
    action = db.Column(db.String(30), nullable=False)
    document_type = db.Column(db.String(30))
    document_id = db.Column(db.Integer)
    request_id = db.Column(db.String(64), nullable=False)
    success = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    error_code = db.Column(db.String(30))
    error_message = db.Column(db.Text)
    plain_message = db.Column(db.Text)
    duration_ms = db.Column(db.Integer)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_gov_api_calls_created_by"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class EWayBillStatus(str, enum.Enum):
    DRAFT = "draft"
    GENERATED = "generated"
    CANCELLED = "cancelled"


class EWayBill(db.Model, TenantScopedMixin, TimestampMixin):
    __tablename__ = "eway_bills"
    __table_args__ = (db.Index("ix_eway_bills_source", "tenant_id", "source_type", "source_id"),)

    id = db.Column(db.Integer, primary_key=True)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id", name="fk_eway_bills_gstin"), nullable=False)
    source_type = db.Column(db.String(30), nullable=False)  # invoice / sales_document / stock_transfer
    source_id = db.Column(db.Integer, nullable=False)
    document_number = db.Column(db.String(64), nullable=False)
    document_date = db.Column(db.Date, nullable=False)
    doc_type = db.Column(db.String(3), nullable=False)  # INV / CHL / BIL / OTH
    supply_type = db.Column(db.String(1), nullable=False, default="O", server_default="O")
    sub_supply_type = db.Column(db.String(2), nullable=False)  # 1 supply, 3 export, 4 job work, 5 own use...
    sub_supply_desc = db.Column(db.String(50))
    from_party = db.Column(db.JSON, nullable=False)  # gstin, name, addr1, place, pincode, state_code
    to_party = db.Column(db.JSON, nullable=False)
    items = db.Column(db.JSON, nullable=False)
    total_value = db.Column(db.Numeric(14, 2), nullable=False, default=0)  # taxable
    cgst_value = db.Column(db.Numeric(14, 2), nullable=False, default=0, server_default="0")
    sgst_value = db.Column(db.Numeric(14, 2), nullable=False, default=0, server_default="0")
    igst_value = db.Column(db.Numeric(14, 2), nullable=False, default=0, server_default="0")
    # Value on the document that isn't taxable value or tax - for a margin-
    # scheme sale, the part of the price that isn't margin (so taxable +
    # tax + other = invoice value, as the e-way bill system checks).
    other_value = db.Column(db.Numeric(14, 2), nullable=False, default=0, server_default="0")
    total_invoice_value = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    # Part B
    trans_mode = db.Column(db.String(1), nullable=False, default="1", server_default="1")  # 1 road 2 rail 3 air 4 ship
    distance_km = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    transporter_id = db.Column(db.String(15))
    transporter_name = db.Column(db.String(100))
    trans_doc_no = db.Column(db.String(15))
    trans_doc_date = db.Column(db.Date)
    vehicle_no = db.Column(db.String(20))
    vehicle_type = db.Column(db.String(1), nullable=False, default="R", server_default="R")  # R regular, O ODC
    # Result
    status = db.Column(db.Enum(EWayBillStatus, name="eway_bill_status"), nullable=False, default=EWayBillStatus.DRAFT)
    ewb_no = db.Column(db.String(20))
    ewb_date = db.Column(db.DateTime(timezone=True))
    valid_upto = db.Column(db.DateTime(timezone=True))
    generated_via = db.Column(db.String(10))  # api / portal
    cancelled_at = db.Column(db.DateTime(timezone=True))
    cancel_reason = db.Column(db.Text)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_eway_bills_created_by"), nullable=False)

    gstin = db.relationship("Gstin")
