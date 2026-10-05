"""Image uploads (tenant logos, product photos) - stored as rows in the
uploaded_images table, not on local disk.

An earlier version of this saved files under app/static/uploads/ instead.
That's lost on every container restart or redeploy on most hosts
(this one included) - a client's logo and item photos would silently
vanish and need re-uploading. A database row persists exactly as
reliably as the rest of the tenant's data, with no extra infrastructure
(S3, etc.) needed to keep this app's self-hosted, offline-friendly
design intact.
"""

import base64
import io

from PIL import Image, ImageOps, UnidentifiedImageError

from app.extensions import db
from app.models.media import UploadedImage

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024

_CONTENT_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}


class UploadError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def save_uploaded_image(file_storage, square_size: int | None = None) -> int | None:
    """Validates and stores an uploaded image as a new UploadedImage row
    (flushed, not committed - the caller's own commit covers it),
    returning its id, or None if no file was submitted.

    square_size, when given, center-crops and resizes the image down to
    an exact square (not just CSS object-fit on display) - for product
    photos, so every POS tile shows a consistently filled square
    regardless of what aspect ratio was uploaded. Logos are stored at
    their native aspect ratio (square_size omitted) since forcing a
    wordmark logo into a square would crop it badly."""
    if not file_storage or not file_storage.filename:
        return None

    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise UploadError("Unsupported image type - use PNG, JPG, GIF or WEBP.")

    data = file_storage.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise UploadError("Image is too large - 5 MB max.")

    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise UploadError("That file doesn't look like a valid image.") from exc

    content_type = _CONTENT_TYPES[ext]
    if square_size:
        data, content_type = _square_crop(data, square_size)

    record = UploadedImage(content_type=content_type, data=data)
    db.session.add(record)
    db.session.flush()
    return record.id


def _square_crop(data: bytes, size: int) -> tuple[bytes, str]:
    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)  # phone photos often carry a rotation flag, not pixels
        fitted = ImageOps.fit(img, (size, size), Image.LANCZOS)
        if fitted.mode in ("RGBA", "P", "LA"):
            fitted = fitted.convert("RGB")
        buf = io.BytesIO()
        fitted.save(buf, format="JPEG", quality=88)
        return buf.getvalue(), "image/jpeg"


def delete_uploaded_image(image_id: int | None) -> None:
    if not image_id:
        return
    UploadedImage.query.filter_by(id=image_id).delete()


def image_data_uri(image_id: int | None) -> str | None:
    """A data: URI embedding the image inline - for a WeasyPrint-rendered
    PDF's <img src=...>, which has no way to reach a database-backed
    /media/<id> URL (and no filesystem path to resolve either)."""
    if not image_id:
        return None
    record = UploadedImage.query.get(image_id)
    if not record:
        return None
    return f"data:{record.content_type};base64,{base64.b64encode(record.data).decode('ascii')}"
