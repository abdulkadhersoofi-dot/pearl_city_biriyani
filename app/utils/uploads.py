"""Local-disk image uploads (logos, product photos). Saved under
app/static/uploads/<subdir>/ with a random filename, so they're served
free by Flask's own static handler - no separate route needed.

Self-hosted on a VPS with persistent disk, this is fine and matches the
app's offline/LAN-friendly design. On a host with an ephemeral filesystem
(Render's free tier, for one) these files are lost on every redeploy or
restart - swap this for S3-compatible object storage before relying on
uploaded images in a deployment like that (see README).
"""

import os
import uuid

from flask import current_app

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


class UploadError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def save_uploaded_image(file_storage, subdir: str) -> str | None:
    """Returns a path relative to app/static/ (for url_for('static', ...)
    and for direct filesystem use in WeasyPrint PDFs), or None if no file
    was submitted."""
    if not file_storage or not file_storage.filename:
        return None

    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise UploadError("Unsupported image type - use PNG, JPG, GIF or WEBP.")

    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > MAX_IMAGE_BYTES:
        raise UploadError("Image is too large - 5 MB max.")

    target_dir = os.path.join(current_app.root_path, "static", "uploads", subdir)
    os.makedirs(target_dir, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    file_storage.save(os.path.join(target_dir, filename))
    return f"uploads/{subdir}/{filename}"


def delete_uploaded_image(relative_path: str | None) -> None:
    if not relative_path:
        return
    full_path = os.path.join(current_app.root_path, "static", relative_path)
    if os.path.isfile(full_path):
        os.remove(full_path)


def absolute_image_path(relative_path: str | None) -> str | None:
    """Absolute filesystem path for embedding in a WeasyPrint PDF -
    url_for('static', ...) produces a server-absolute URL path ('/static/
    ...') that WeasyPrint's file resolver can't map back to this app's
    static folder, so PDF templates need the real path instead."""
    if not relative_path:
        return None
    full_path = os.path.join(current_app.root_path, "static", relative_path)
    return full_path if os.path.isfile(full_path) else None
