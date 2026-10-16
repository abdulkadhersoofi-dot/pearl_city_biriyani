"""IRN work for invoices and credit / debit notes - generate through the
client's provider or record one got from the IRP's offline tool, cancel
(within IRN_CANCEL_HOURS, then the document is voided), and the one place
every government API call goes through (`call`): log in when needed,
retry once on an expired login, time it, log it in gov_api_calls and the
audit log, and turn failures into GovApiError with a plain-language line.

Credentials are looked up by tenant_id + gstin_id, never by id alone."""

import re
import time
import uuid
from datetime import datetime

import requests

from app.einvoice import errors, rules
from app.einvoice.providers import PROVIDERS, ProviderError
from app.einvoice.providers.common import IST, parse_dt
from app.einvoice.schema import payload_for
from app.einvoice.vault import VaultError, seal, unseal
from app.extensions import db
from app.models.einvoice import EInvoiceCredential, EWayBill, EWayBillStatus, GovApiCall
from app.models.invoice import Invoice
from app.models.mixins import utcnow
from app.utils.audit import record_audit
from app.utils.gst_config import IRN_CANCEL_HOURS

IRN_RE = re.compile(r"^[0-9a-f]{64}$")
IRN_CANCEL_REASONS = {"1": "Duplicate", "2": "Data entry mistake", "3": "Order cancelled", "4": "Other"}


class EInvoiceError(Exception):
    """Something the user can fix, in plain words."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class GovApiError(EInvoiceError):
    """The IRP / e-way bill system / GSP refused or couldn't be reached:
    message is the plain line, code and original are what it sent."""

    def __init__(self, message: str, code: str | None = None, original: str = "", request_id: str = "",
                 info=None):
        super().__init__(message)
        self.code = code
        self.original = original
        self.request_id = request_id
        self.info = info or []


def kind_of(doc) -> str:
    return "invoice" if isinstance(doc, Invoice) else "note"


def number_of(doc) -> str:
    return doc.invoice_number if isinstance(doc, Invoice) else doc.note_number


# --- credentials ---------------------------------------------------------------------

def credential_for(tenant_id: int, gstin_id: int) -> EInvoiceCredential | None:
    return EInvoiceCredential.query.filter_by(tenant_id=tenant_id, gstin_id=gstin_id, is_active=True).first()


def save_credential(tenant_id: int, gstin, provider_key: str, values: dict, user) -> EInvoiceCredential:
    """values: what the settings form posted for this provider's fields.
    A secret left blank keeps the one already saved."""
    provider = PROVIDERS.get(provider_key)
    if provider is None:
        raise EInvoiceError("Choose who connects you to the IRP.")
    cred = EInvoiceCredential.query.filter_by(tenant_id=tenant_id, gstin_id=gstin.id).first()
    old = {}
    if cred is not None and cred.provider == provider_key:
        try:
            old = unseal(cred.secrets)
        except VaultError:
            old = {}
    data, missing = {}, []
    for f in provider.fields:
        value = (values.get(f.name) or "").strip()
        if f.name == "username":
            continue
        if not value and f.secret:
            value = old.get(f.name, "")
        if value:
            data[f.name] = value
        elif f.required:
            missing.append(f.label)
    username = (values.get("username") or "").strip()
    if not username:
        missing.insert(0, "API user name")
    if missing:
        raise EInvoiceError("Fill in: " + ", ".join(missing) + ".")
    if cred is None:
        cred = EInvoiceCredential(tenant_id=tenant_id, gstin_id=gstin.id)
        db.session.add(cred)
    cred.provider = provider_key
    cred.username = username
    cred.secrets = seal(data)
    cred.session = None  # a new login means a new session
    cred.token_expiry = None
    cred.is_active = True
    cred.updated_by_id = user.id
    record_audit(user, "einvoice_credential_saved", tenant_id=tenant_id, entity_type="gstin", entity_id=gstin.id,
                 details={"provider": provider_key, "username": username})
    return cred


def provider_for(cred: EInvoiceCredential, http=None):
    config = unseal(cred.secrets)
    config["username"] = cred.username
    session = unseal(cred.session) if cred.session else {}
    return PROVIDERS[cred.provider](cred.gstin.gstin, config, session, http=http)


def _session_live(cred) -> bool:
    return bool(cred.session and cred.token_expiry and cred.token_expiry > utcnow())


# --- every call goes through here --------------------------------------------------

def call(cred: EInvoiceCredential, user, action: str, fn, doc_type: str | None = None, doc_id: int | None = None,
         http=None):
    """Run fn(provider) with a live login; log it whatever happens. On
    failure the log row and audit entry are committed straight away (so
    the caller must not have other half-done changes in the session) and
    GovApiError is raised."""
    request_id = uuid.uuid4().hex
    started = time.monotonic()
    log = GovApiCall(tenant_id=cred.tenant_id, gstin_id=cred.gstin_id, provider=cred.provider, action=action,
                     document_type=doc_type, document_id=doc_id, request_id=request_id,
                     created_by_id=getattr(user, "id", None))
    result, failure = None, None
    try:
        provider = provider_for(cred, http=http)
        try:
            if action == "authenticate" or not _session_live(cred):
                provider.authenticate()
            if action != "authenticate":
                result = fn(provider)
        except ProviderError as exc:
            if not exc.auth_expired or action == "authenticate":
                raise
            provider.session = {}
            provider.authenticate()
            result = fn(provider)
        cred.session = seal(provider.session)
        expiry = provider.session.get("expiry")
        cred.token_expiry = datetime.fromisoformat(expiry) if expiry else None
        log.success = True
    except requests.RequestException as exc:
        timed_out = " (timed out)" if isinstance(exc, requests.Timeout) else ""
        failure = ProviderError(f"Couldn't connect: {exc.__class__.__name__}{timed_out}", code="NETWORK")
    except ProviderError as exc:
        failure = exc
    except VaultError as exc:
        failure = ProviderError(str(exc), code="CONFIG")
    except (KeyError, ValueError, TypeError) as exc:
        failure = ProviderError(f"Unexpected reply or missing setting: {exc}", code="REPLY")
    log.duration_ms = int((time.monotonic() - started) * 1000)
    if failure is not None:
        log.error_code = (failure.code or "")[:30] or None
        log.error_message = failure.message
        log.plain_message = errors.plain(failure)
    db.session.add(log)
    record_audit(user, f"gov_api_{action}", tenant_id=cred.tenant_id, entity_type=doc_type, entity_id=doc_id,
                 details={"request_id": request_id, "provider": cred.provider, "success": log.success,
                          "error_code": log.error_code})
    if failure is None:
        return result
    db.session.commit()
    raise GovApiError(log.plain_message, code=failure.code, original=failure.message, request_id=request_id,
                      info=failure.info)


# --- IRN -----------------------------------------------------------------------------

def _require_credential(doc):
    cred = credential_for(doc.tenant_id, doc.gstin_id)
    if cred is None:
        raise EInvoiceError("No IRP login is saved for this GSTIN - add one under e-Invoice settings, or download "
                            "the JSON, upload it with the IRP's offline tool and record the IRN here.")
    return cred


def _store(doc, irn: str, ack_no: str, ack_date, signed_qr: str | None):
    doc.irn = irn
    doc.irn_ack_number = ack_no
    doc.irn_ack_date = ack_date or utcnow()
    doc.qr_code_data = signed_qr


def generate_irn(tenant, user, doc, http=None):
    if not rules.needs_irn(tenant, doc):
        raise EInvoiceError("This document doesn't need an IRN, or already has one.")
    payload = payload_for(tenant, doc)  # EInvoiceDataError lists every problem
    cred = _require_credential(doc)
    kind = kind_of(doc)
    try:
        result = call(cred, user, "generate_irn", lambda p: p.generate_irn(payload), kind, doc.id, http=http)
    except GovApiError as exc:
        existing = _duplicate_irn(exc)
        if not existing:
            raise
        # The IRP already has it (a retry after a timeout, or generated on
        # the portal) - fetch it rather than make the user copy it over.
        result = call(cred, user, "get_irn", lambda p: p.get_irn(existing), kind, doc.id, http=http)
    _store(doc, result.irn, result.ack_no, result.ack_date, result.signed_qr)
    record_audit(user, "irn_generated", tenant_id=doc.tenant_id, entity_type=kind, entity_id=doc.id,
                 details={"irn": result.irn, "ack_no": result.ack_no, "provider": cred.provider})
    return result


def _duplicate_irn(exc: GovApiError) -> str | None:
    if not exc.code or errors.DUPLICATE_IRN_CODE not in exc.code.split(","):
        return None
    for info in getattr(exc, "info", None) or []:
        desc = info.get("Desc") if isinstance(info, dict) else None
        if isinstance(desc, dict) and desc.get("Irn"):
            return desc["Irn"]
    return None


def record_irn(tenant, user, doc, irn: str, ack_no: str, ack_date: str, signed_qr: str | None):
    """An IRN got outside the app (the IRP's offline tool / portal)."""
    if not rules.needs_irn(tenant, doc):
        raise EInvoiceError("This document doesn't need an IRN, or already has one.")
    irn = (irn or "").strip().lower()
    if not IRN_RE.match(irn):
        raise EInvoiceError("An IRN is 64 characters - digits and letters a-f. Copy it exactly from the IRP.")
    ack_no = (ack_no or "").strip()
    if not ack_no.isdigit():
        raise EInvoiceError("The acknowledgement number is digits only.")
    when = parse_dt((ack_date or "").replace("T", " ") + (":00" if len(ack_date or "") == 16 else ""))
    if when is None:
        raise EInvoiceError("Enter the acknowledgement date and time.")
    _store(doc, irn, ack_no, when, (signed_qr or "").strip() or None)
    record_audit(user, "irn_recorded", tenant_id=doc.tenant_id, entity_type=kind_of(doc), entity_id=doc.id,
                 details={"irn": irn, "ack_no": ack_no})


def cancel_irn(tenant, user, doc, reason_code: str, remark: str, on_portal: bool = False, http=None,
               now: datetime | None = None):
    """Cancel on the IRP (or record that it was cancelled on the portal),
    then void the document with the same reason."""
    if not doc.irn or doc.irn_cancelled_at or doc.status.value != "issued":
        raise EInvoiceError("This document has no active IRN.")
    if not rules.can_cancel_irn(doc, now):
        raise EInvoiceError(f"The IRP only allows cancelling an IRN within {IRN_CANCEL_HOURS} hours of generating "
                            "it, and that time has passed. Issue a credit note against the invoice instead.")
    if kind_of(doc) == "invoice" and EWayBill.query.filter_by(
            tenant_id=doc.tenant_id, source_type="invoice", source_id=doc.id, status=EWayBillStatus.GENERATED).first():
        # The IRP refuses to cancel an IRN while an e-way bill on it is active.
        raise EInvoiceError("This invoice has an active e-way bill - cancel the e-way bill first, then the IRN.")
    if reason_code not in IRN_CANCEL_REASONS:
        raise EInvoiceError("Choose why the IRN is being cancelled.")
    remark = (remark or "").strip()
    if not remark:
        raise EInvoiceError("Add a remark - it goes to the IRP and onto the voided document.")
    remark = remark[:100]
    kind = kind_of(doc)
    cancelled_at = None
    if not on_portal:
        cred = _require_credential(doc)
        cancelled_at = call(cred, user, "cancel_irn", lambda p: p.cancel_irn(doc.irn, reason_code, remark),
                            kind, doc.id, http=http)
    doc.irn_cancelled_at = cancelled_at or utcnow()
    doc.irn_cancel_reason = f"{IRN_CANCEL_REASONS[reason_code]}: {remark}"
    reason = f"IRN cancelled ({IRN_CANCEL_REASONS[reason_code]}): {remark}"
    if kind == "invoice":
        from app.invoicing.services import void_invoice

        void_invoice(doc, user, reason)
    else:
        from app.notes.services import void_note

        void_note(doc, user, reason)
    record_audit(user, "irn_cancelled", tenant_id=doc.tenant_id, entity_type=kind, entity_id=doc.id,
                 details={"irn": doc.irn, "reason": doc.irn_cancel_reason, "on_portal": on_portal})


def ack_local(doc) -> str:
    if not doc.irn_ack_date:
        return ""
    return doc.irn_ack_date.astimezone(IST).strftime("%d %b %Y %H:%M")

