"""A client's business details after onboarding: names, GST registration
type, business type, turnover and business locations (GSTINs) - changed
from the console (tenants.edit_details) by the Ultra Admin or an auditor
with the "edit client details" power.

Documents already issued never change. A document points at the business
location it was issued from, so a location that has issued documents is
never given a different GSTIN in place: the old location is kept, switched
off, and a new one takes its place, carrying on its numbering (numbers
are unique across the business, so starting again at 0001 would repeat
numbers already issued). Names, address, city and PIN code are corrected
in place."""

import re

from app.extensions import db
from app.models.tenant import BusinessType, Gstin, RegistrationType
from app.utils.audit import record_audit
from app.utils.gst import GSTIN_REGEX, normalize_gstin
from app.utils.indian_states import STATE_NAME_BY_CODE

REGISTRATION_LABELS = {
    RegistrationType.REGULAR: "Regular (GST registered)",
    RegistrationType.COMPOSITION: "Composition scheme",
    RegistrationType.UNREGISTERED: "Unregistered (no GSTIN)",
}


class DetailsError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def location_has_documents(location: Gstin) -> bool:
    """Has anything been numbered or recorded against this location? Every
    numbered document takes its number from a series on its location; the
    direct checks cover anything recorded without one."""
    from app.models.invoice import Invoice
    from app.models.invoice_series import InvoiceSeries
    from app.models.note import CreditDebitNote
    from app.models.pos_bill import POSBill

    if location.id is None:
        return False
    if InvoiceSeries.query.filter(InvoiceSeries.gstin_id == location.id, InvoiceSeries.next_number > 1).first():
        return True
    for model in (Invoice, POSBill, CreditDebitNote):
        if model.query.filter_by(gstin_id=location.id).first() is not None:
            return True
    return False


def _clean(value, limit=None):
    value = (value or "").strip()
    return value[:limit] if limit else value


def _check_gstin(raw: str, registered: bool, tenant, location) -> tuple[str | None, str | None]:
    """(gstin, state code it belongs to) for this registration, or a
    DetailsError saying what's wrong."""
    gstin = normalize_gstin(raw) or None
    if not registered:
        return None, None
    if not gstin:
        raise DetailsError("A GST-registered business needs a GSTIN on every business location.")
    if not re.match(GSTIN_REGEX, gstin):
        raise DetailsError(f"{gstin} isn't a valid 15-character GSTIN.")
    if gstin[:2] not in STATE_NAME_BY_CODE:
        raise DetailsError(f"{gstin} starts with {gstin[:2]}, which isn't a state code.")
    other = Gstin.query.filter(Gstin.gstin == gstin, Gstin.id != (location.id or 0)).first()
    if other is not None and other.tenant_id != tenant.id:
        raise DetailsError(f"{gstin} is already on file for another client.")
    if other is not None and other.is_active:
        raise DetailsError(f"{gstin} is already on another of this client's locations.")
    return gstin, gstin[:2]


def save_details(tenant, actor, data: dict) -> list[str]:
    """Applies the posted details. data: legal_name, trade_name,
    registration_type, business_type, apply_preset, aato and locations -
    [{id, gstin, state_code, address, city, pincode, remove}] with the
    primary location's id in primary_id. Returns what changed, in words.
    Raises DetailsError (nothing is saved by the caller then)."""
    from app.utils.features import BUSINESS_TYPE_LABELS, apply_business_type_preset

    changes = []
    before = {"legal_name": tenant.legal_name, "trade_name": tenant.trade_name,
              "registration_type": tenant.registration_type.value, "business_type": tenant.business_type.value,
              "aato": str(tenant.aato) if tenant.aato is not None else None}

    legal_name = _clean(data.get("legal_name"), 255)
    if not legal_name:
        raise DetailsError("The business's legal name can't be empty.")
    try:
        registration = RegistrationType(data.get("registration_type"))
        business_type = BusinessType(data.get("business_type"))
    except ValueError:
        raise DetailsError("Pick a registration type and a type of business.")
    registered = registration != RegistrationType.UNREGISTERED

    if legal_name != tenant.legal_name:
        changes.append(f"Legal name: {tenant.legal_name} -> {legal_name}")
        tenant.legal_name = legal_name
    trade_name = _clean(data.get("trade_name"), 255) or None
    if trade_name != tenant.trade_name:
        changes.append(f"Trade name: {tenant.trade_name or '-'} -> {trade_name or '-'}")
        tenant.trade_name = trade_name
    if registration != tenant.registration_type:
        changes.append(f"Registration: {REGISTRATION_LABELS[tenant.registration_type]} -> {REGISTRATION_LABELS[registration]}")
        tenant.registration_type = registration
    if data.get("apply_preset"):
        apply_business_type_preset(tenant, business_type)
        changes.append(f"Business type: {BUSINESS_TYPE_LABELS[business_type]}, modules set to its recommended set")
    elif business_type != tenant.business_type:
        changes.append(f"Business type: {BUSINESS_TYPE_LABELS[tenant.business_type]} -> {BUSINESS_TYPE_LABELS[business_type]}")
        tenant.business_type = business_type
    aato = data.get("aato")
    if aato != tenant.aato:
        changes.append(f"Turnover last year: {tenant.aato if tenant.aato is not None else '-'} -> {aato if aato is not None else '-'}")
        tenant.aato = aato

    changes += _save_locations(tenant, data.get("locations") or [], data.get("primary_id"), registered)

    if changes:
        after = {"legal_name": tenant.legal_name, "trade_name": tenant.trade_name,
                 "registration_type": tenant.registration_type.value, "business_type": tenant.business_type.value,
                 "aato": str(tenant.aato) if tenant.aato is not None else None}
        record_audit(actor, "client_details_changed", tenant_id=tenant.id, entity_type="tenant", entity_id=tenant.id,
                     details={"before": before, "after": after, "changes": changes})
    return changes


def _save_locations(tenant, posted: list[dict], primary_id, registered: bool) -> list[str]:
    changes = []
    current = {g.id: g for g in tenant.gstins.filter_by(is_active=True).order_by(Gstin.id)}
    if not current:
        if registered:
            raise DetailsError("Add a business location with its GSTIN first.")
        return changes
    kept, primary, seen = [], None, set()
    for row in posted:
        loc = current.get(row.get("id"))
        if loc is None:
            continue
        if row.get("remove"):
            if loc.is_primary or str(loc.id) == str(primary_id):
                raise DetailsError("The main business location can't be removed - make another one the main one first.")
            loc.is_active = False
            loc.is_primary = False
            changes.append(f"Location {loc.display_label} switched off (its documents stay as they are)")
            continue
        gstin, gstin_state = _check_gstin(row.get("gstin"), registered, tenant, loc)
        if gstin and gstin in seen:
            raise DetailsError(f"{gstin} is on two locations - each location has its own GSTIN.")
        seen.add(gstin)
        state_code = gstin_state or _clean(row.get("state_code"))
        if state_code not in STATE_NAME_BY_CODE:
            raise DetailsError("Pick the state of every business location.")
        address, city, pincode = (_clean(row.get("address")), _clean(row.get("city"), 100),
                                  _clean(row.get("pincode"), 6))
        if pincode and not re.fullmatch(r"[1-9][0-9]{5}", pincode):
            raise DetailsError(f"{pincode} isn't a 6-digit PIN code.")
        if gstin != loc.gstin or state_code != loc.state_code:
            earlier = (Gstin.query.filter_by(tenant_id=tenant.id, gstin=gstin, is_active=False).first()
                       if gstin else None)
            if earlier is not None or location_has_documents(loc):
                new = _replace(tenant, loc, gstin, state_code)
                changes.append(f"Location {loc.display_label} -> {new.display_label} (a new location; "
                               f"the old one keeps its documents)")
                loc = new
            else:
                changes.append(f"Location {loc.display_label} -> {gstin or 'Unregistered - ' + STATE_NAME_BY_CODE[state_code]}")
                loc.gstin, loc.state_code, loc.state_name = gstin, state_code, STATE_NAME_BY_CODE[state_code]
        if (address or None, city or None, pincode or None) != (loc.registered_address or None, loc.city or None,
                                                                loc.pincode or None):
            loc.registered_address, loc.city, loc.pincode = address or None, city or None, pincode or None
            changes.append(f"Location {loc.display_label}: address updated")
        kept.append(loc)
        if str(row.get("id")) == str(primary_id):
            primary = loc
    # Every location still on, checked against the registration - including
    # any that wasn't posted (a GSTIN can't stay on an unregistered business).
    posted_ids = {row.get("id") for row in posted}
    for loc in current.values():
        if loc.id in posted_ids or not loc.is_active:
            continue
        if registered != bool(loc.gstin):
            raise DetailsError("Every business location needs updating for the new registration type.")
        kept.append(loc)
    if not kept:
        raise DetailsError("Keep at least one business location.")
    primary = primary or next((l for l in kept if l.is_primary), kept[0])
    if not primary.is_primary:
        changes.append(f"Main location: {primary.display_label}")
    for loc in kept:
        loc.is_primary = loc is primary
    db.session.flush()
    return changes


def _replace(tenant, old: Gstin, gstin, state_code) -> Gstin:
    """A new location in place of one that has issued documents - or the
    client's own earlier location with this GSTIN, switched back on."""
    again = (Gstin.query.filter_by(tenant_id=tenant.id, gstin=gstin, is_active=False).first()
             if gstin else None)
    new = again or Gstin(tenant_id=tenant.id, gstin=gstin)
    new.state_code, new.state_name = state_code, STATE_NAME_BY_CODE[state_code]
    new.registered_address, new.city, new.pincode = old.registered_address, old.city, old.pincode
    new.is_active, new.is_primary = True, old.is_primary
    old.is_active, old.is_primary = False, False
    db.session.add(new)
    db.session.flush()
    _carry_numbering(old, new)
    return new


def _carry_numbering(old: Gstin, new: Gstin) -> None:
    """The new location carries on the old one's numbering. Bill and
    invoice numbers are unique across the whole business, so restarting
    at 0001 would repeat numbers already issued. The series rows move to
    the new location; documents keep pointing at the location they were
    issued from."""
    from app.models.invoice_series import InvoiceSeries

    for series in InvoiceSeries.query.filter_by(gstin_id=old.id).all():
        twin = InvoiceSeries.query.filter_by(tenant_id=series.tenant_id, gstin_id=new.id,
                                             financial_year=series.financial_year,
                                             document_type=series.document_type, prefix=series.prefix).first()
        if twin is None:
            series.gstin_id = new.id
        else:
            twin.next_number = max(twin.next_number, series.next_number)
    db.session.flush()
