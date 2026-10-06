from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField
from wtforms.validators import DataRequired, Email, EqualTo, Length, Optional


class BranchForm(FlaskForm):
    name = StringField("Branch name", validators=[DataRequired(), Length(max=255)])
    email = StringField("Login email", validators=[DataRequired(), Email(), Length(max=255)])
    phone = StringField("Phone", validators=[Optional(), Length(max=20)])
    password = PasswordField("Initial password", validators=[DataRequired(), Length(min=8)])
    confirm_password = PasswordField(
        "Confirm password",
        validators=[DataRequired(), EqualTo("password", message="Passwords must match")],
    )
