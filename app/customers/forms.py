from flask_wtf import FlaskForm
from wtforms import SelectField, StringField
from wtforms.validators import DataRequired, Length, Optional, Regexp

from app.tenants.forms import GSTIN_REGEX
from app.utils.indian_states import INDIAN_STATES


class CustomerForm(FlaskForm):
    name = StringField("Customer name", validators=[DataRequired(), Length(max=255)])
    gstin = StringField(
        "GSTIN (optional)", validators=[Optional(), Regexp(GSTIN_REGEX, message="Enter a valid 15-character GSTIN")]
    )
    address_line1 = StringField("Address line 1", validators=[Optional(), Length(max=255)])
    address_line2 = StringField("Address line 2", validators=[Optional(), Length(max=255)])
    city = StringField("City", validators=[Optional(), Length(max=100)])
    state_code = SelectField("State", choices=INDIAN_STATES, validators=[DataRequired()])
    pincode = StringField("PIN code", validators=[Optional(), Length(max=10)])
    phone = StringField("Phone", validators=[Optional(), Length(max=20)])
    email = StringField("Email", validators=[Optional(), Length(max=255)])
