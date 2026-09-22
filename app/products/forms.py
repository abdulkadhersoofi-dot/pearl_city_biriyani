from flask_wtf import FlaskForm
from wtforms import DecimalField, StringField
from wtforms.validators import DataRequired, Length, NumberRange, Optional


class ProductForm(FlaskForm):
    name = StringField("Item name", validators=[DataRequired(), Length(max=255)])
    description = StringField("Description (optional)", validators=[Optional()])
    hsn_or_sac_code = StringField("HSN/SAC code", validators=[DataRequired(), Length(max=8)])
    gst_rate = DecimalField("GST rate (%)", validators=[DataRequired(), NumberRange(min=0, max=28)])
    unit = StringField("Unit", validators=[DataRequired(), Length(max=20)], default="pcs")
    default_price = DecimalField("Default price", validators=[DataRequired(), NumberRange(min=0)])
