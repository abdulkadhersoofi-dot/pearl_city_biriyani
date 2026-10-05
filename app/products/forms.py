from decimal import Decimal

from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileSize
from wtforms import DecimalField, SelectField, StringField
from wtforms.validators import DataRequired, InputRequired, Length, NumberRange, Optional

from app.utils.uploads import ALLOWED_IMAGE_EXTENSIONS, MAX_IMAGE_BYTES

# The actual GST slabs published on the GST portal - not a free-typed
# number, so an item can't end up on a rate that doesn't exist (e.g. a
# typo'd "2.8" meant to be "28"). "0" covers nil-rated items.
GST_RATE_CHOICES = [
    (Decimal("0"), "0%"),
    (Decimal("0.1"), "0.1%"),
    (Decimal("0.25"), "0.25%"),
    (Decimal("1"), "1%"),
    (Decimal("1.5"), "1.5%"),
    (Decimal("3"), "3%"),
    (Decimal("5"), "5%"),
    (Decimal("6"), "6%"),
    (Decimal("7.5"), "7.5%"),
    (Decimal("12"), "12%"),
    (Decimal("18"), "18%"),
    (Decimal("28"), "28%"),
    (Decimal("40"), "40%"),
]


class ProductForm(FlaskForm):
    name = StringField("Item name", validators=[DataRequired(), Length(max=255)])
    description = StringField("Description (optional)", validators=[Optional()])
    hsn_or_sac_code = StringField("HSN/SAC code", validators=[DataRequired(), Length(max=8)])
    # InputRequired, not DataRequired: DataRequired treats Decimal('0')
    # as falsy and rejects it outright, which blocked 0% (nil-rated)
    # items - some food items are genuinely 0% GST. InputRequired only
    # checks that a choice was submitted, so 0% passes through fine.
    gst_rate = SelectField("GST rate", choices=GST_RATE_CHOICES, coerce=Decimal, validators=[InputRequired()])
    unit = StringField("Unit", validators=[DataRequired(), Length(max=20)], default="pcs")
    default_price = DecimalField("Default price", validators=[InputRequired(), NumberRange(min=0)])
    image = FileField(
        "Item image (optional)",
        validators=[
            FileAllowed(sorted(ALLOWED_IMAGE_EXTENSIONS), "PNG, JPG, GIF or WEBP only"),
            FileSize(MAX_IMAGE_BYTES, message="Image is too large - 5 MB max."),
        ],
    )
