"""Scanned bills (the "Scan bills" module, flag doc_scan - app/scan/).

A ScanDocument is one photo or PDF a client uploaded: the file itself,
what the reading engine found on it (app/scan/engine/reader.py), and what
became of it - posted as a purchase bill or an expense, kept on file,
rejected, or discarded. A PDF holding several invoices is split into one
child document per invoice (parent_id); the parent stays as "split".

The file and its preview are deferred columns: a list of documents never
loads the pictures."""

from app.extensions import db
from app.models.mixins import TenantScopedMixin, TimestampMixin

UPLOADED = "uploaded"     # waiting to be read
READING = "reading"       # being read now
TO_CHECK = "to_check"     # read - waiting for someone to check and post it
REJECTED = "rejected"     # unreadable, or not this business's bill
FAILED = "failed"         # the read itself failed (service down, bad file) - can be retried
POSTED = "posted"         # posted as a purchase bill or an expense
FILED = "filed"           # kept as a record only (a sales bill, a receipt, ...)
DISCARDED = "discarded"   # thrown away by the user
SPLIT = "split"           # a bundle, now its child documents

OPEN_STATUSES = (UPLOADED, READING, TO_CHECK, REJECTED, FAILED)


class ScanDocument(db.Model, TenantScopedMixin, TimestampMixin):
    __tablename__ = "scan_documents"

    id = db.Column(db.Integer, primary_key=True)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_scan_documents_uploaded_by"),
                               nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey("scan_documents.id", name="fk_scan_documents_parent"),
                          index=True)
    filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(50), nullable=False)
    file_hash = db.Column(db.String(64), nullable=False, index=True)
    file_data = db.deferred(db.Column(db.LargeBinary, nullable=False))
    preview = db.deferred(db.Column(db.LargeBinary))
    page_count = db.Column(db.Integer)

    status = db.Column(db.String(20), nullable=False, default=UPLOADED, server_default=UPLOADED, index=True)
    category = db.Column(db.String(40))
    extracted = db.Column(db.JSON)
    validation = db.Column(db.JSON)
    engine = db.Column(db.JSON)
    rejection_code = db.Column(db.String(30))
    message = db.Column(db.Text)
    gstin_id = db.Column(db.Integer, db.ForeignKey("gstins.id", name="fk_scan_documents_gstin"))
    duplicate_of = db.Column(db.String(120))

    posted_type = db.Column(db.String(20))   # purchase_bill / expense
    posted_id = db.Column(db.Integer)
    posted_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_scan_documents_posted_by"))
    read_at = db.Column(db.DateTime(timezone=True))
    posted_at = db.Column(db.DateTime(timezone=True))

    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id])
    gstin = db.relationship("Gstin")

    @property
    def is_pdf(self) -> bool:
        return self.content_type == "application/pdf"

    @property
    def title(self) -> str:
        ex = self.extracted or {}
        party = ex.get("supplier_name") or ex.get("receiver_name")
        number = ex.get("invoice_number")
        if party and number:
            return f"{party} · {number}"
        return party or number or self.filename
