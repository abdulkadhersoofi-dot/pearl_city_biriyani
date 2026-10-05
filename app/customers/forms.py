from flask_wtf import FlaskForm
from wtforms import SelectField, StringField
from wtforms.validators import DataRequired, Length, Optional, Regexp

from app.tenants.forms import GSTIN_REGEX
from app.utils.indian_states import INDIAN_STATES


def _normalize_gstin(value):
    # Without this, a stray leading/trailing space left in the field
    # survives as the stored value (Optional() only skips the Regexp
    # check for blank input, it doesn't clear it) - a whitespace string
    # is truthy, so it was silently misclassifying these customers as
    # B2B (has a GSTIN) in GSTR-1 instead of B2C.
    return (value or "").strip().upper() or None


class CustomerForm(FlaskForm):
    name = StringField("Customer name", validators=[DataRequired(), Length(max=255)])
    gstin = StringField(
        "GSTIN (optional)",
        validators=[Optional(), Regexp(GSTIN_REGEX, message="Enter a valid 15-character GSTIN")],
        filters=[_normalize_gstin],
    )
    address_line1 = StringField("Address line 1", validators=[Optional(), Length(max=255)])
    address_line2 = StringField("Address line 2", validators=[Optional(), Length(max=255)])
    city = StringField("City", validators=[Optional(), Length(max=100)])
    state_code = SelectField("State", choices=INDIAN_STATES, validators=[DataRequired()])
    pincode = StringField("PIN code", validators=[Optional(), Length(max=10)])
    phone = StringField("Phone", validators=[Optional(), Length(max=20)])
    email = StringField("Email", validators=[Optional(), Length(max=255)])
