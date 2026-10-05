from app.extensions import db
from app.models.mixins import TimestampMixin


class UploadedImage(db.Model, TimestampMixin):
    """A logo or product photo, stored as bytes in the database rather
    than on local disk - see app/utils/uploads.py for why: local-disk
    uploads are lost on container restart/redeploy on most hosts (this
    one included), which meant a client's logo and item photos silently
    vanished and had to be re-uploaded. A database row survives exactly
    as reliably as the rest of the tenant's data does.
    """

    __tablename__ = "uploaded_images"

    id = db.Column(db.Integer, primary_key=True)
    content_type = db.Column(db.String(50), nullable=False)
    data = db.Column(db.LargeBinary, nullable=False)

    def __repr__(self):
        return f"<UploadedImage {self.id} {self.content_type} ({len(self.data)} bytes)>"
