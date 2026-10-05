from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileSize
from wtforms import SelectField, StringField
from wtforms.validators import DataRequired, Regexp

from app.pos.receipt import THERMAL_WIDTHS_IN
from app.utils.theme import FONT_FAMILY_CHOICES, HEX_COLOR_RE
from app.utils.uploads import ALLOWED_IMAGE_EXTENSIONS, MAX_IMAGE_BYTES

RECEIPT_FORMAT_CHOICES = [(k, f"{k} roll") for k in THERMAL_WIDTHS_IN] + [("a4", "A4 copy")]


class TenantSettingsForm(FlaskForm):
    logo = FileField(
        "Logo",
        validators=[
            FileAllowed(sorted(ALLOWED_IMAGE_EXTENSIONS), "PNG, JPG, GIF or WEBP only"),
            FileSize(MAX_IMAGE_BYTES, message="Image is too large - 5 MB max."),
        ],
    )
    primary_color = StringField(
        "Button colour",
        validators=[DataRequired(), Regexp(HEX_COLOR_RE, message="Enter a valid colour, e.g. #1f7a4d")],
        render_kw={"type": "color"},
    )
    background_color = StringField(
        "Background colour",
        validators=[DataRequired(), Regexp(HEX_COLOR_RE, message="Enter a valid colour, e.g. #f6f7f5")],
        render_kw={"type": "color"},
    )
    font_family = SelectField("Font", choices=FONT_FAMILY_CHOICES, validators=[DataRequired()])
    default_receipt_format = SelectField(
        "Default print size", choices=RECEIPT_FORMAT_CHOICES, validators=[DataRequired()]
    )
