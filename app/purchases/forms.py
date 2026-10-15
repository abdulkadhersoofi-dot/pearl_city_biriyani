from flask_wtf import FlaskForm
from wtforms import DateField, DecimalField, IntegerField, SelectField, StringField
from wtforms.validators import DataRequired, Length, NumberRange, Optional, Regexp

from app.utils.gst import GSTIN_REGEX, normalize_gstin
from app.utils.indian_states import INDIAN_STATES


class SupplierForm(FlaskForm):
    name = StringField("Supplier name", validators=[DataRequired(), Length(max=255)])
    gstin = StringField(
        "GSTIN (optional)",
        validators=[Optional(), Regexp(GSTIN_REGEX, message="Enter a valid 15-character GSTIN")],
        filters=[normalize_gstin],
    )
    state_code = SelectField("State", choices=INDIAN_STATES, validators=[DataRequired()])
    address_line1 = StringField("Address", validators=[Optional(), Length(max=255)])
    city = StringField("City", validators=[Optional(), Length(max=100)])
    pincode = StringField("PIN code", validators=[Optional(), Length(max=10)])
    contact_person = StringField("Contact person", validators=[Optional(), Length(max=100)])
    phone = StringField("Phone", validators=[Optional(), Length(max=20)])
    email = StringField("Email", validators=[Optional(), Length(max=255)])
    payment_terms_days = IntegerField("Credit days", validators=[Optional(), NumberRange(min=0, max=365)])
    opening_balance = DecimalField("Opening balance (owed to them)", places=2, default=0, validators=[Optional()])
    # Supplier accounts only (feature "payables") - removed by the route otherwise.
    opening_balance_date = DateField("Opening balance as on", validators=[Optional()])

    def validate(self, extra_validators=None):
        ok = super().validate(extra_validators)
        # A registered supplier's state is the first two digits of their GSTIN.
        if ok and self.gstin.data and self.gstin.data[:2] != self.state_code.data:
            self.state_code.errors.append("This doesn't match the state in the GSTIN.")
            return False
        return ok
