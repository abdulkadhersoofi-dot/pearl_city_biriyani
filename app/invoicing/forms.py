from datetime import date

from flask_wtf import FlaskForm
from wtforms import DateField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional, Regexp

from app.utils.gst import GSTIN_REGEX, normalize_gstin
from app.utils.indian_states import INDIAN_STATES


class InvoiceHeaderForm(FlaskForm):
    """Header fields only - line items are posted as parallel arrays
    (line_description[], line_qty[], ...) and validated in the route, so
    the whole invoice stays a single screen with client-side add/remove
    rows instead of a server round-trip per row.
    """

    gstin_id = SelectField("Billing from (your GSTIN)", coerce=int, validators=[DataRequired()])
    document_type = SelectField("Document type", validators=[DataRequired()])

    # Exactly one of these is used - which one is picked in the UI via the
    # "Bill to" toggle (customer vs. branch). branch_user_id blank means
    # "billing a customer"; routes.py/create_invoice decide which applies.
    branch_user_id = SelectField("Branch", validators=[Optional()])
    customer_id = SelectField("Customer", validators=[Optional()])
    new_customer_name = StringField("New customer name", validators=[Optional(), Length(max=255)])
    new_customer_gstin = StringField(
        "New customer GSTIN (optional)",
        validators=[Optional(), Regexp(GSTIN_REGEX, message="Enter a valid 15-character GSTIN, or leave it blank")],
        filters=[normalize_gstin],
    )
    new_customer_address = StringField("Address", validators=[Optional(), Length(max=255)])
    new_customer_state_code = SelectField(
        "State", choices=[("", "Select state")] + INDIAN_STATES, validators=[Optional()]
    )

    place_of_supply_state_code = SelectField(
        "Place of supply", choices=INDIAN_STATES, validators=[DataRequired()]
    )
    invoice_date = DateField("Invoice date", validators=[DataRequired()], default=date.today)
    notes = TextAreaField("Notes (optional)", validators=[Optional(), Length(max=1000)])
