from flask_wtf import FlaskForm
from wtforms import PasswordField, SelectField, StringField
from wtforms.validators import DataRequired, Email, EqualTo, Length, Optional

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
