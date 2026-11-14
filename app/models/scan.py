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
    # What the phone app read on the phone itself before sending it (the
    # ARFA Scan app): {"text": its on-device OCR, "qr": the e-invoice QR's
    # payload, "replaces": the document it was retaken for}. Given to the
    # reader as a second pair of eyes.
    phone_read = db.Column(db.JSON)

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


class ScanAppToken(db.Model, TenantScopedMixin, TimestampMixin):
    """A phone signed in to the ARFA Scan app (app/scan/api.py): the app
    sends its token with every request. Only the token's hash is kept;
    signing out, a password change or the user being switched off ends it."""

    __tablename__ = "scan_app_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_scan_app_tokens_user"), nullable=False,
                        index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    device_name = db.Column(db.String(120))
    # A fingerprint of the password it was signed in with: a new password
    # (or an admin's reset) signs every phone out.
    password_stamp = db.Column(db.String(16), nullable=False)
    last_used_at = db.Column(db.DateTime(timezone=True))
    revoked_at = db.Column(db.DateTime(timezone=True))

    user = db.relationship("User")


LESSON_CORRECTION = "correction"   # rows the reader missed on a bill, learnt when it was posted
LESSON_NOTE = "note"               # a note someone wrote ("Teach ARFA")
LESSON_HEADING = "heading"         # a heading on a supplier's bills and what it means


class ScanLesson(db.Model, TimestampMixin):
    """Something ARFA's bill reader has been taught (app/scan/lessons.py).

    A correction: rows the reader missed on a supplier's bill, saved when the
    bill was posted with them added - shown to the reader with that
    supplier's next bills. A note: what the owner (or, for every business,
    the Ultra Admin: tenant_id empty) wrote about a supplier's bills, or
    about bills in general (supplier_gstin empty). Switched off rather than
    lost; deleting is the owner's choice."""

    __tablename__ = "scan_lessons"

    id = db.Column(db.Integer, primary_key=True)
    # Empty: the Ultra Admin's, for every business.
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id", name="fk_scan_lessons_tenant"), index=True)
    kind = db.Column(db.String(20), nullable=False, default=LESSON_NOTE)
    # Empty: about every supplier's bills.
    supplier_gstin = db.Column(db.String(15), index=True)
    supplier_name = db.Column(db.String(255))
    text = db.Column(db.Text)
    # A correction: {"invoice_number", "rows": [{"description", "hsn", "qty", "rate", "value"}],
    #                "read_rows", "posted_rows", "read_total", "posted_total"}
    example = db.Column(db.JSON)
    source_doc_id = db.Column(db.Integer, db.ForeignKey("scan_documents.id", name="fk_scan_lessons_doc"))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_scan_lessons_created_by"))
    active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    uses = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    last_used_at = db.Column(db.DateTime(timezone=True))

    created_by = db.relationship("User")
