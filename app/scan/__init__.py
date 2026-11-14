"""Scan bills (flag doc_scan): photos and PDFs of bills read by the AI
engine (app/scan/engine), checked on one screen, and posted as purchase
bills or expenses.

  engine/reader.py      the reading pipeline (QR, two-tier AI, repair)
  engine/validators.py  deterministic checks on what was read
  services.py           storing, reading, splitting, duplicates, posting
  routes.py             the pages, and the JSON endpoints a phone app uses

For the phone app (signed in like the website, with the session cookie and
the X-CSRFToken header):
  POST /scan/upload              multipart "file" -> {id, status, existing, ...}
  POST /scan/<id>/read           -> the document, or {"split": [parts]} for a bundle
  GET  /scan/documents.json?tab= check | reading | done | rejected
  GET  /scan/<id>.json           everything read, with the checks
  GET  /scan/<id>/preview.jpg    the picture; /scan/<id>/file the original
Checking and posting stay on the review page (/scan/<id>), which works on a
phone screen as it is.
"""

from flask import Blueprint

scan_bp = Blueprint("scan", __name__, url_prefix="/scan", template_folder="../templates/scan")

from app.scan import routes  # noqa: E402,F401
