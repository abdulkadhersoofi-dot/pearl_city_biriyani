from datetime import date, timedelta

from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileSize
from wtforms import DateField, PasswordField, SelectField, StringField
from wtforms.validators import DataRequired, Email, EqualTo, Length, Optional, Regexp

from app.models.tenant import BillingCycle, RegistrationType
from app.utils.gst import GSTIN_REGEX, normalize_gstin
from app.utils.indian_states import INDIAN_STATES
from app.utils.uploads import ALLOWED_IMAGE_EXTENSIONS, MAX_IMAGE_BYTES

STATE_CODE_REGEX = r"^[0-9]{2}$"


def _default_valid_until() -> date:
    # A one-year suggestion, not a policy - the Ultra Admin picks the
    # real date; this just saves re-typing the common case.
    return date.today() + timedelta(days=365)


class OnboardClientForm(FlaskForm):
    legal_name = StringField("Business legal name", validators=[DataRequired(), Length(max=255)])
    trade_name = StringField("Trade name (optional)", validators=[Optional(), Length(max=255)])
    registration_type = SelectField(
        "GST registration type",
        choices=[(t.value, t.name.title()) for t in RegistrationType],
        validators=[DataRequired()],
    )
    gstin = StringField(
        "GSTIN (leave blank if unregistered)",
        validators=[Optional(), Regexp(GSTIN_REGEX, message="Enter a valid 15-character GSTIN")],
        filters=[normalize_gstin],
    )
    state_code = SelectField("State", choices=INDIAN_STATES, validators=[DataRequired()])
    registered_address = StringField("Registered address", validators=[Optional()])

    # No valid_until/billing_cycle field here, whoever is filling this in
    # (Ultra Admin or Auditor) - billing selection always stays with the
    # Ultra Admin, set later at verification (tenants.verify_client), not
    # at onboarding. Every new client starts pending either way.

    admin_name = StringField("Client Admin - full name", validators=[DataRequired(), Length(max=255)])
    admin_email = StringField("Client Admin - email", validators=[DataRequired(), Email()])
    admin_phone = StringField("Client Admin - phone", validators=[Optional(), Length(max=20)])
    admin_password = PasswordField("Client Admin - initial password", validators=[DataRequired(), Length(min=8)])
    admin_confirm_password = PasswordField(
        "Confirm password",
        validators=[DataRequired(), EqualTo("admin_password", message="Passwords must match")],
    )
    logo = FileField(
        "Client logo (optional)",
        validators=[
            FileAllowed(sorted(ALLOWED_IMAGE_EXTENSIONS), "PNG, JPG, GIF or WEBP only"),
            FileSize(MAX_IMAGE_BYTES, message="Image is too large - 5 MB max."),
        ],
    )


class RenewAccessForm(FlaskForm):
    """Only the Ultra Admin ever fills this in (tenants.renew_access) - it
    moves a tenant's hard expiry date, nothing else can."""

    valid_until = DateField("New access valid-until date", validators=[DataRequired()])


class VerifyClientForm(FlaskForm):
    """Only the Ultra Admin ever fills this in (tenants.verify_client) -
    the one point where a pending client (billing_cycle is None,
    whoever onboarded it) gets its billing cycle picked and is allowed
    to go live. This selection never belongs to the Auditor who may have
    onboarded the client."""

    billing_cycle = SelectField(
        "Billing cycle",
        choices=[(c.value, c.name.title()) for c in BillingCycle],
        validators=[DataRequired()],
    )
    valid_until = DateField(
        "Access valid until", validators=[DataRequired()], default=_default_valid_until
    )


class CreateAuditorForm(FlaskForm):
    """Creates an Auditor (Ultra Admin only) or a Sub-Auditor (Ultra Admin,
    or an Auditor creating one within their own office). Which of the two
    roles, and the parent Auditor for a Sub-Auditor, are handled by the
    route via manual request.form parsing rather than a field here - the
    choice depends on who's filling the form in, same convention used
    elsewhere in this codebase for variable-shaped admin input (e.g. POS
    line items)."""

    name = StringField("Full name", validators=[DataRequired(), Length(max=255)])
    email = StringField("Email", validators=[DataRequired(), Email()])
    phone = StringField("Phone", validators=[Optional(), Length(max=20)])
    password = PasswordField("Initial password", validators=[DataRequired(), Length(min=8)])
    confirm_password = PasswordField(
        "Confirm password",
        validators=[DataRequired(), EqualTo("password", message="Passwords must match")],
    )


class AddGstinForm(FlaskForm):
    """Backfills a business-location row for a tenant that was onboarded
    before onboarding always created one (or simply has none yet)."""

    gstin = StringField(
        "GSTIN (leave blank if unregistered)",
        validators=[Optional(), Regexp(GSTIN_REGEX, message="Enter a valid 15-character GSTIN")],
        filters=[normalize_gstin],
    )
    state_code = SelectField("State", choices=INDIAN_STATES, validators=[DataRequired()])
    registered_address = StringField("Registered address", validators=[Optional()])
