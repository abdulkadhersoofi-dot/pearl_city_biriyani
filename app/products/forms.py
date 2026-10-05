from flask_wtf import FlaskForm
from wtforms import DecimalField, StringField
from wtforms.validators import DataRequired, InputRequired, Length, NumberRange, Optional


class ProductForm(FlaskForm):
    name = StringField("Item name", validators=[DataRequired(), Length(max=255)])
    description = StringField("Description (optional)", validators=[Optional()])
    hsn_or_sac_code = StringField("HSN/SAC code", validators=[DataRequired(), Length(max=8)])
    # InputRequired, not DataRequired: DataRequired treats Decimal('0') as
    # falsy and rejects it outright, which blocked 0% (nil-rated) items -
    # some food items are genuinely 0% GST. InputRequired only checks that
    # the field was submitted, so an explicit 0 passes through to NumberRange.
    gst_rate = DecimalField("GST rate (%)", validators=[InputRequired(), NumberRange(min=0, max=28)])
    unit = StringField("Unit", validators=[DataRequired(), Length(max=20)], default="pcs")
    default_price = DecimalField("Default price", validators=[InputRequired(), NumberRange(min=0)])
