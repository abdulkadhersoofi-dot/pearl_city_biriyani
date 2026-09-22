from datetime import date

from flask_wtf import FlaskForm
from wtforms import DateField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional

from app.utils.indian_states import INDIAN_STATES


class InvoiceHeaderForm(FlaskForm):
    """Header fields only - line items are posted as parallel arrays
    (line_description[], line_qty[], ...) and validated in the route, so
    the whole invoice stays a single screen with client-side add/remove
    rows instead of a server round-trip per row.
    """

    gstin_id = SelectField("Billing from (your GSTIN)", coerce=int, validators=[DataRequired()])
    document_type = SelectField("Document type", validators=[DataRequired()])

    customer_id = SelectField("Customer", validators=[DataRequired()])
    new_customer_name = StringField("New customer name", validators=[Optional(), Length(max=255)])
    new_customer_gstin = StringField("New customer GSTIN (optional)", validators=[Optional(), Length(max=15)])
    new_customer_address = StringField("Address", validators=[Optional(), Length(max=255)])
    new_customer_state_code = SelectField(
        "State", choices=[("", "Select state")] + INDIAN_STATES, validators=[Optional()]
    )

    place_of_supply_state_code = SelectField(
        "Place of supply", choices=INDIAN_STATES, validators=[DataRequired()]
    )
    invoice_date = DateField("Invoice date", validators=[DataRequired()], default=date.today)
    notes = TextAreaField("Notes (optional)", validators=[Optional(), Length(max=1000)])
