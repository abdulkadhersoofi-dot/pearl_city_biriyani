from flask_wtf import FlaskForm
from wtforms import SelectField, StringField
from wtforms.validators import DataRequired, Email, Length, Optional, Regexp

from app.models.tenant import RegistrationType
from app.utils.indian_states import INDIAN_STATES

GSTIN_REGEX = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$"
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
    )
    state_code = SelectField("State", choices=INDIAN_STATES, validators=[DataRequired()])
    registered_address = StringField("Registered address", validators=[Optional()])

    admin_name = StringField("Client Admin - full name", validators=[DataRequired(), Length(max=255)])
    admin_email = StringField("Client Admin - email", validators=[DataRequired(), Email()])
    admin_phone = StringField("Client Admin - phone", validators=[Optional(), Length(max=20)])
