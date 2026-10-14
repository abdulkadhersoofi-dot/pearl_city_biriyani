from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileSize
from wtforms import BooleanField, DecimalField, SelectField, SelectMultipleField, StringField, widgets
from wtforms.validators import DataRequired, Length, NumberRange, Optional, Regexp

from app.models.pos_bill import PAYMENT_MODE_LABELS, SELECTABLE_PAYMENT_MODES

from app.pos.receipt import THERMAL_WIDTHS_IN
from app.utils.features import AVAILABLE_FEATURES, BUSINESS_TYPE_CHOICES
from app.utils.theme import FONT_FAMILY_CHOICES, HEX_COLOR_RE
from app.utils.uploads import ALLOWED_IMAGE_EXTENSIONS, MAX_IMAGE_BYTES

RECEIPT_FORMAT_CHOICES = [(k, f"{k} roll") for k in THERMAL_WIDTHS_IN] + [("a4", "A4 copy")]
PAYMENT_MODE_CHOICES = [(m.value, PAYMENT_MODE_LABELS[m]) for m in SELECTABLE_PAYMENT_MODES]
# name@bank handle, as every UPI app shows it.
UPI_VPA_RE = r"^[A-Za-z0-9.\-_]{2,256}@[A-Za-z][A-Za-z0-9]{1,63}$"


class MultiCheckboxField(SelectMultipleField):
    widget = widgets.ListWidget(prefix_label=False)
    option_widget = widgets.CheckboxInput()


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
    text_color = StringField(
        "Font colour",
        validators=[DataRequired(), Regexp(HEX_COLOR_RE, message="Enter a valid colour, e.g. #1f2a24")],
        render_kw={"type": "color"},
    )
    font_family = SelectField("Font", choices=FONT_FAMILY_CHOICES, validators=[DataRequired()])
    default_receipt_format = SelectField(
        "POS bill print size", choices=RECEIPT_FORMAT_CHOICES, validators=[DataRequired()]
    )

    # Optional server-side: the select always submits a value, and a client
    # that omits it keeps its current type (settings.routes).
    business_type = SelectField("Type of business", choices=BUSINESS_TYPE_CHOICES, validators=[Optional()])
    apply_business_preset = BooleanField("Also switch modules to the recommended set for this type of business")
    aato = DecimalField(
        "Turnover last financial year (Rs.)", places=2, validators=[Optional(), NumberRange(min=0)]
    )
    default_price_includes_tax = BooleanField("New items' prices already include GST")

    pos_payment_modes = MultiCheckboxField("Payment modes at the counter", choices=PAYMENT_MODE_CHOICES)
    upi_vpa = StringField(
        "UPI ID for scan-to-pay",
        filters=[lambda v: (v or "").strip() or None],
        validators=[Optional(), Regexp(UPI_VPA_RE, message="Enter a UPI ID like shopname@okhdfcbank")],
    )
    upi_payee_name = StringField(
        "Name shown in the customer's UPI app", filters=[lambda v: (v or "").strip() or None],
        validators=[Optional(), Length(max=100)],
    )


def feature_field_name(key: str) -> str:
    return f"feature_{key}"


# One switch per module that's actually built (app.utils.features) - a
# flag for a module that doesn't exist yet is never offered, since
# switching it on would do nothing.
for _feature in AVAILABLE_FEATURES:
    setattr(TenantSettingsForm, feature_field_name(_feature.key), BooleanField(_feature.label))
