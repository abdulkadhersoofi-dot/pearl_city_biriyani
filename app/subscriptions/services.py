"""The firm's own subscription invoices (platform billing, Phase 10).

Each client (Tenant) and Auditor office already has a billing cycle -
monthly or yearly, a next renewal date (app.utils.billing). With a
subscription fee set, the firm raises a GST invoice for each period from
that data, a few days before the renewal (PlatformProfile.days_before_due):

  - numbered in the firm's own series (SUB/2026-27/0001) under the same
    row-locked allocator as every client series (app.utils.numbering)
  - tax through the one GST service (app.utils.gst): SAC and rate from
    the firm's profile (defaults in gst_config), CGST+SGST when the
    subscriber is in the firm's state, else IGST; no GST at all while the
    firm has no GSTIN on its profile
  - a UPI link / QR for the amount (app.pos.share.upi_uri)
  - paid -> the subscriber's cycle moves on through the same idempotent
    mark_period_paid as the "Mark this period paid" button
  - never deleted: voided with a reason, every step audit-logged."""

from datetime import date, timedelta
from decimal import Decimal

from app.extensions import db
from app.models.mixins import utcnow
from app.models.platform import (
    PlatformInvoice,
    PlatformInvoiceStatus,
    PlatformProfile,
    PlatformSeries,
)
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.utils import gst_config
from app.utils.audit import record_audit
from app.utils.billing import advance_billing_cycle, mark_period_paid
from app.utils.financial_year import financial_year_for
from app.utils.gst import compute_invoice_totals, compute_line
from app.utils.numbering import allocate_number

AUDITOR_ROLES = (UserRole.AUDITOR, UserRole.SUB_AUDITOR)


class SubscriptionError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def profile() -> PlatformProfile | None:
    return PlatformProfile.query.order_by(PlatformProfile.id).first()


def profile_or_default() -> PlatformProfile:
    """The saved profile, or an unsaved one with the starting values for
    the edit form."""
    return profile() or PlatformProfile(sac_code=gst_config.PLATFORM_SERVICE_SAC,
                                        gst_rate=gst_config.PLATFORM_SERVICE_GST_RATE, days_before_due=7,
                                        invoice_prefix="SUB", state_code="")


# --- who is billed -------------------------------------------------------------------------

def bill_to(subject) -> dict:
    if isinstance(subject, Tenant):
        gstin = subject.gstins.filter_by(is_primary=True).first() or subject.gstins.first()
        admin = subject.users.filter_by(role=UserRole.CLIENT_ADMIN).first()
        return {"name": subject.legal_name, "gstin": (gstin.gstin or "") if gstin else "",
                "state_code": gstin.state_code if gstin else "", "address": (gstin.registered_address or "") if gstin else "",
                "email": admin.email if admin else ""}
    return {"name": subject.name, "gstin": "", "state_code": "", "address": "", "email": subject.email}


def _subject_filter(query, subject):
    if isinstance(subject, Tenant):
        return query.filter(PlatformInvoice.tenant_id == subject.id)
    return query.filter(PlatformInvoice.auditor_id == subject.id)


def live_invoice(subject, period_start: date) -> PlatformInvoice | None:
    return _subject_filter(PlatformInvoice.query, subject).filter(
        PlatformInvoice.period_start == period_start, PlatformInvoice.status != PlatformInvoiceStatus.VOID).first()


def billable(subject) -> bool:
    return bool(subject.billing_cycle is not None and subject.next_billing_due is not None
                and subject.subscription_fee and Decimal(subject.subscription_fee) > 0)


def due_subjects(today: date) -> list:
    """Clients and Auditor offices whose next period should be invoiced
    now (renewal within the profile's days_before_due) and isn't yet."""
    prof = profile()
    if prof is None:
        return []
    horizon = today + timedelta(days=prof.days_before_due or 0)
    subjects = (Tenant.query.filter(Tenant.billing_cycle.isnot(None), Tenant.subscription_fee > 0,
                                    Tenant.next_billing_due <= horizon).order_by(Tenant.legal_name).all()
                + User.query.filter(User.role.in_(AUDITOR_ROLES), User.billing_cycle.isnot(None),
                                    User.subscription_fee > 0, User.next_billing_due <= horizon)
                .order_by(User.name).all())
    return [s for s in subjects if live_invoice(s, s.next_billing_due) is None]


# --- the invoice ---------------------------------------------------------------------------

def compute(fee: Decimal, gst_rate: Decimal, firm_registered: bool, intra_state: bool) -> dict:
    """The invoice's figures - through the one GST service."""
    line = compute_line(qty=Decimal("1"), rate=Decimal(fee), discount_percent=Decimal("0"), gst_rate=Decimal(gst_rate),
                        is_intra_state=intra_state, charge_gst=firm_registered)
    return compute_invoice_totals([line])


def create_invoice(subject, actor=None, today: date | None = None) -> PlatformInvoice:
    today = today or date.today()
    prof = profile()
    if prof is None:
        raise SubscriptionError("Fill in the firm's billing profile first.")
    if not billable(subject):
        raise SubscriptionError("Set a billing cycle and a subscription fee first.")
    start = subject.next_billing_due
    if live_invoice(subject, start) is not None:
        raise SubscriptionError("That period is already invoiced.")
    to = bill_to(subject)
    place = to["state_code"] or prof.state_code
    registered = bool(prof.gstin)
    totals = compute(subject.subscription_fee, prof.gst_rate, registered, place == prof.state_code)
    fy = financial_year_for(today)
    series = PlatformSeries.query.filter_by(financial_year=fy, prefix=prof.invoice_prefix).first()
    if series is None:
        series = PlatformSeries(financial_year=fy, prefix=prof.invoice_prefix, next_number=1)
        db.session.add(series)
        db.session.flush()
    number = allocate_number(series)
    end = advance_billing_cycle(subject, start) - timedelta(days=1)
    cycle = subject.billing_cycle.value
    invoice = PlatformInvoice(
        tenant_id=subject.id if isinstance(subject, Tenant) else None,
        auditor_id=None if isinstance(subject, Tenant) else subject.id,
        series_id=series.id, number=number, invoice_date=today, period_start=start, period_end=end,
        billing_cycle=cycle,
        seller={"name": prof.legal_name, "gstin": prof.gstin or "", "state_code": prof.state_code,
                "address": prof.address or "", "email": prof.email or "", "phone": prof.phone or ""},
        bill_to=to, place_of_supply=place, sac_code=prof.sac_code,
        description=f"ARFA billing software - {cycle} subscription, {start.strftime('%d %b %Y')} to "
                    f"{end.strftime('%d %b %Y')}",
        gst_rate=prof.gst_rate if registered else Decimal("0"),
        taxable_value=totals["total_taxable_value"], cgst=totals["total_cgst"], sgst=totals["total_sgst"],
        igst=totals["total_igst"], round_off=totals["round_off"], grand_total=totals["grand_total"],
        status=PlatformInvoiceStatus.ISSUED, created_by_id=actor.id if actor else None,
    )
    db.session.add(invoice)
    db.session.flush()
    record_audit(actor, "platform_invoice_issued", tenant_id=invoice.tenant_id, entity_type="platform_invoice",
                 entity_id=invoice.id, details={"number": number, "amount": str(invoice.grand_total),
                                                "period_start": start.isoformat()})
    return invoice


def raise_due(actor=None, today: date | None = None) -> list[PlatformInvoice]:
    today = today or date.today()
    return [create_invoice(s, actor, today) for s in due_subjects(today)]


def subject_of(invoice: PlatformInvoice):
    return invoice.tenant if invoice.tenant_id else invoice.auditor


def mark_paid(invoice: PlatformInvoice, actor, reference: str | None = None, today: date | None = None) -> str:
    """Paid: the invoice, and the subscriber's cycle through the same
    idempotent mark_period_paid as the "Mark this period paid" button.
    Returns what happened to the cycle ("advanced" / "paid_ahead" /
    "already_paid")."""
    if invoice.status == PlatformInvoiceStatus.PAID:
        return "already_paid"
    if invoice.status == PlatformInvoiceStatus.VOID:
        raise SubscriptionError("A voided invoice can't be paid.")
    today = today or date.today()
    invoice.status = PlatformInvoiceStatus.PAID
    invoice.paid_at = utcnow()
    invoice.paid_by_id = actor.id if actor else None
    invoice.payment_reference = (reference or "").strip()[:100] or None
    subject = subject_of(invoice)
    result = mark_period_paid(subject, today, for_period=invoice.period_start)
    record_audit(actor, "platform_invoice_paid", tenant_id=invoice.tenant_id, entity_type="platform_invoice",
                 entity_id=invoice.id, details={"number": invoice.number, "cycle": result,
                                                "next_billing_due": subject.next_billing_due.isoformat()
                                                if subject.next_billing_due else None})
    return result


def void(invoice: PlatformInvoice, actor, reason: str) -> None:
    reason = (reason or "").strip()
    if not reason:
        raise SubscriptionError("A reason is required to void an invoice.")
    if invoice.status != PlatformInvoiceStatus.ISSUED:
        raise SubscriptionError("Only an unpaid invoice can be voided.")
    invoice.status = PlatformInvoiceStatus.VOID
    invoice.voided_at = utcnow()
    invoice.voided_by_id = actor.id
    invoice.void_reason = reason[:500]
    record_audit(actor, "platform_invoice_voided", tenant_id=invoice.tenant_id, entity_type="platform_invoice",
                 entity_id=invoice.id, details={"number": invoice.number, "reason": invoice.void_reason})


def upi_link(invoice: PlatformInvoice) -> str | None:
    prof = profile()
    if prof is None or not prof.upi_vpa or invoice.status != PlatformInvoiceStatus.ISSUED:
        return None
    from app.pos.share import upi_uri

    return upi_uri(prof.upi_vpa, prof.upi_payee_name or prof.legal_name, invoice.grand_total, invoice.number)


def render_pdf(invoice: PlatformInvoice) -> bytes:
    from flask import current_app, render_template
    from weasyprint import HTML

    from app.pos.share import qr_svg_data_uri

    link = upi_link(invoice)
    html = render_template("subscriptions/invoice_pdf.html", invoice=invoice,
                           upi_qr=qr_svg_data_uri(link) if link else None)
    return HTML(string=html, base_url=current_app.root_path).write_pdf()
