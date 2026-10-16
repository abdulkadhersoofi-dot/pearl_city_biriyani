"""e-Invoice (IRN) and e-Way Bill - flags einvoice and eway_bill (Phase 8).

  rules.py      when each applies (thresholds in app.utils.gst_config)
  schema.py     INV-01 JSON builder + validator
  providers/    who the app talks to: NIC sandbox / production, any GSP
  vault.py      encrypted logins
  services.py   IRN generate / record / cancel, and the one logged call path
  eway.py       e-way bills: Part A / B, JSON, generate, vehicle, cancel
  errors.py     IRP errors in plain words"""

from flask import Blueprint

einvoice_bp = Blueprint("einvoice", __name__, url_prefix="/einvoice", template_folder="../templates/einvoice")

from app.einvoice import routes  # noqa: E402,F401
