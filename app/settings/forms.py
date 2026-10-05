from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileSize
from wtforms import SelectField, StringField
from wtforms.validators import DataRequired, Regexp

from app.pos.receipt import THERMAL_WIDTHS_IN
from app.utils.uploads import ALLOWED_IMAGE_EXTENSIONS, MAX_IMAGE_BYTES

HEX_COLOR_REGEX = r"^#[0-9A-Fa-f]{6}$"

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
        "Login page colour",
        validators=[DataRequired(), Regexp(HEX_COLOR_REGEX, message="Enter a valid colour, e.g. #1f7a4d")],
        render_kw={"type": "color"},
    )
    default_receipt_format = SelectField(
        "Default print size", choices=RECEIPT_FORMAT_CHOICES, validators=[DataRequired()]
    )
