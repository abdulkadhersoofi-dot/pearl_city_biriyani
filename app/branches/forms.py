from flask_wtf import FlaskForm
from wtforms import BooleanField, DecimalField, PasswordField, SelectField, StringField
from wtforms.validators import DataRequired, Email, EqualTo, Length, NumberRange, Optional

BRANCH_TYPE_CHOICES = [
    ("owned", "PCB-owned branch (internal - stock transfers as a Delivery Challan, no GST)"),
    ("third_party", "Third-party seller (independent buyer - billed as a Tax Invoice, with GST)"),
]


class BranchForm(FlaskForm):
    name = StringField("Branch name", validators=[DataRequired(), Length(max=255)])
    email = StringField("Login email", validators=[DataRequired(), Email(), Length(max=255)])
    phone = StringField("Phone", validators=[Optional(), Length(max=20)])
    branch_type = SelectField("Branch type", choices=BRANCH_TYPE_CHOICES, validators=[DataRequired()])
    password = PasswordField("Initial password", validators=[DataRequired(), Length(min=8)])
    confirm_password = PasswordField(
        "Confirm password",
        validators=[DataRequired(), EqualTo("password", message="Passwords must match")],
    )


class BranchPermissionsForm(FlaskForm):
    """What this branch/counter login may do beyond plain billing - see
    app.utils.permissions. A Client Admin always has all of these."""

    can_give_discount = BooleanField("Give discounts at billing")
    max_discount_percent = DecimalField(
        "Largest discount allowed (%)", places=2, default=0, validators=[Optional(), NumberRange(min=0, max=100)]
    )
    can_edit_price_at_pos = BooleanField("Change an item's price at billing")
    can_refund = BooleanField("Refund bills")
    can_void = BooleanField("Void bills")
    can_view_reports = BooleanField("See the Day-book and Z-report")
    can_manage_stock = BooleanField("Adjust and transfer stock")
