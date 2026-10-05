from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileSize
from wtforms import PasswordField, SelectField, StringField
from wtforms.validators import DataRequired, Email, EqualTo, Length, Optional, Regexp

from app.models.tenant import RegistrationType
from app.utils.gst import GSTIN_REGEX, normalize_gstin
from app.utils.indian_states import INDIAN_STATES
from app.utils.uploads import ALLOWED_IMAGE_EXTENSIONS, MAX_IMAGE_BYTES

STATE_CODE_REGEX = r"^[0-9]{2}$"


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
