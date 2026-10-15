from flask_wtf import FlaskForm
from wtforms import DateField, DecimalField, IntegerField, SelectField, StringField
from wtforms.validators import DataRequired, Length, NumberRange, Optional, Regexp

from app.utils.gst import GSTIN_REGEX, normalize_gstin
from app.utils.indian_states import INDIAN_STATES


class CustomerForm(FlaskForm):
    name = StringField("Customer name", validators=[DataRequired(), Length(max=255)])
    gstin = StringField(
        "GSTIN (optional)",
        validators=[Optional(), Regexp(GSTIN_REGEX, message="Enter a valid 15-character GSTIN")],
        filters=[normalize_gstin],
    )
    address_line1 = StringField("Address line 1", validators=[Optional(), Length(max=255)])
    address_line2 = StringField("Address line 2", validators=[Optional(), Length(max=255)])
    city = StringField("City", validators=[Optional(), Length(max=100)])
    state_code = SelectField("State", choices=INDIAN_STATES, validators=[DataRequired()])
    pincode = StringField("PIN code", validators=[Optional(), Length(max=10)])
    phone = StringField("Phone", validators=[Optional(), Length(max=20)])
    email = StringField("Email", validators=[Optional(), Length(max=255)])

    # Customer accounts (feature "receivables") - removed from the form by
    # the route when that module is off, so they're never touched then.
    opening_balance = DecimalField("Opening balance (Rs.)", places=2, validators=[Optional()],
                                   description="What they owed you when you started keeping accounts here "
                                               "(negative if they had paid in advance).")
    opening_balance_date = DateField("As on", validators=[Optional()])
    credit_limit = DecimalField("Credit limit (Rs.)", places=2, validators=[Optional(), NumberRange(min=0)],
                                description="Billing past this shows a warning - it never blocks the sale.")
    credit_days = IntegerField("Credit days", validators=[Optional(), NumberRange(min=0, max=3650)],
                               description="Days they get to pay - bills older than this show as overdue.")

    ACCOUNT_FIELDS = ("opening_balance", "opening_balance_date", "credit_limit", "credit_days")
