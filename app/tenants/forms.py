from datetime import date, timedelta

from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileSize
from wtforms import DateField, PasswordField, SelectField, StringField
from wtforms.validators import DataRequired, Email, EqualTo, Length, Optional, Regexp

from app.models.tenant import RegistrationType
from app.utils.gst import GSTIN_REGEX, normalize_gstin
from app.utils.indian_states import INDIAN_STATES
from app.utils.uploads import ALLOWED_IMAGE_EXTENSIONS, MAX_IMAGE_BYTES

STATE_CODE_REGEX = r"^[0-9]{2}$"


def _default_valid_until() -> date:
    # A one-year suggestion, not a policy - the Super Admin picks the
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

    valid_until = DateField(
        "Access valid until", validators=[DataRequired()], default=_default_valid_until
    )

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
    """Only a Super Admin ever fills this in (tenants.renew_access) - it
    moves a tenant's hard expiry date, nothing else can."""

    valid_until = DateField("New access valid-until date", validators=[DataRequired()])


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
