from decimal import Decimal

from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileSize
from wtforms import BooleanField, DecimalField, SelectField, StringField
from wtforms.validators import DataRequired, InputRequired, Length, NumberRange, Optional

from app.utils import gst_config
from app.utils.gst import exceeds_mrp, hsn_error
from app.utils.uploads import ALLOWED_IMAGE_EXTENSIONS, MAX_IMAGE_BYTES

# The actual GST slabs (app.utils.gst_config) - not a free-typed number, so
# an item can't end up on a rate that doesn't exist (e.g. a typo'd "2.8"
# meant to be "28"). "0" covers nil-rated items.
GST_RATE_CHOICES = [(rate, f"{rate.normalize():f}%") for rate in gst_config.GST_RATE_SLABS]

UQC_CHOICES = [(code, f"{code} - {label}") for code, label in gst_config.UQC_CODES.items()]


def _strip_or_none(value):
    return (value or "").strip() or None


def _uqc_case(value):
    """'pcs' -> 'PCS' for a real UQC code; anything else (an old item's own
    unit like 'plate') is left exactly as it was."""
    value = (value or "").strip()
    return value.upper() if value.upper() in gst_config.UQC_CODES else value


class ProductForm(FlaskForm):
    name = StringField("Item name", validators=[DataRequired(), Length(max=255)])
    description = StringField("Description (optional)", validators=[Optional()])
    is_service = BooleanField("This is a service (labour, repair, consultation) - not stocked")
    hsn_or_sac_code = StringField(
        "HSN code (SAC for a service)", validators=[DataRequired(), Length(max=8)], filters=[lambda v: (v or "").strip()]
    )
    # InputRequired, not DataRequired: DataRequired treats Decimal('0')
    # as falsy and rejects it outright, which blocked 0% (nil-rated)
    # items - some food items are genuinely 0% GST. InputRequired only
    # checks that a choice was submitted, so 0% passes through fine.
    gst_rate = SelectField("GST rate", choices=GST_RATE_CHOICES, coerce=Decimal, validators=[InputRequired()])
    sale_price = DecimalField("Sale price", places=2, validators=[InputRequired(), NumberRange(min=0)])
    price_includes_tax = BooleanField("Sale price already includes GST")
    # Choices are the GST portal UQC list; products.routes appends an item's
    # own pre-UQC unit ("plate") as an extra choice so editing an old item
    # doesn't force a unit change on its printed bills.
    unit = SelectField(
        "Unit", choices=UQC_CHOICES, default=gst_config.DEFAULT_UQC, validators=[DataRequired()], filters=[_uqc_case]
    )

    # "More details" - all optional.
    mrp = DecimalField("MRP (incl. GST)", places=2, validators=[Optional(), NumberRange(min=0)])
    wholesale_price = DecimalField("Wholesale price", places=2, validators=[Optional(), NumberRange(min=0)])
    purchase_rate = DecimalField("Purchase rate", places=2, validators=[Optional(), NumberRange(min=0)])
    cost_price = DecimalField("Cost price", places=2, validators=[Optional(), NumberRange(min=0)])
    sku = StringField("SKU / item code", validators=[Optional(), Length(max=64)], filters=[_strip_or_none])
    barcode = StringField("Barcode", validators=[Optional(), Length(max=64)], filters=[_strip_or_none])
    alt_unit = SelectField(
        "Bigger pack unit", choices=[("", "None")] + UQC_CHOICES, validators=[Optional()], filters=[_uqc_case]
    )
    alt_unit_factor = DecimalField(
        "Units in one pack", places=3, validators=[Optional(), NumberRange(min=Decimal("0.001"))]
    )
    min_stock_level = DecimalField("Low-stock alert below", places=3, validators=[Optional(), NumberRange(min=0)])
    allow_negative_stock = BooleanField("Allow billing even when stock shows zero", default=True)
    is_favourite = BooleanField("Pin to the favourites row at the counter")

    image = FileField(
        "Item image (optional)",
        validators=[
            FileAllowed(sorted(ALLOWED_IMAGE_EXTENSIONS), "PNG, JPG, GIF or WEBP only"),
            FileSize(MAX_IMAGE_BYTES, message="Image is too large - 5 MB max."),
        ],
    )
    # Only rendered/used when the tenant has the "categories" feature on
    # (see products/form.html and products.routes) - choices are populated
    # per request from that tenant's own Category/SubCategory rows. Both
    # stay optional even then - "" (coerced to None) means uncategorized.
    category_id = SelectField("Category (optional)", choices=[], validators=[Optional()])
    sub_category_id = SelectField("Sub-category (optional)", choices=[], validators=[Optional()])

    def __init__(self, *args, tenant=None, product_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Set by the route: whose AATO bracket the HSN rule uses, and
        # which item (if editing) the SKU/barcode uniqueness check skips.
        self.tenant = tenant
        self.product_id = product_id

    def validate(self, extra_validators=None):
        ok = super().validate(extra_validators=extra_validators)
        if self.hsn_or_sac_code.data:
            error = hsn_error(self.hsn_or_sac_code.data, self.tenant.aato if self.tenant else None, self.is_service.data)
            if error:
                self.hsn_or_sac_code.errors.append(error)
                ok = False
        if (
            self.sale_price.data is not None
            and self.gst_rate.data is not None
            and exceeds_mrp(self.sale_price.data, self.gst_rate.data, self.price_includes_tax.data, self.mrp.data)
        ):
            self.sale_price.errors.append("With GST, this price is more than the MRP - selling above MRP isn't allowed.")
            ok = False
        if bool(self.alt_unit.data) != (self.alt_unit_factor.data is not None):
            self.alt_unit_factor.errors.append("Set both the pack unit and how many units are in one pack, or neither.")
            ok = False
        if self.alt_unit.data and self.alt_unit.data == self.unit.data:
            self.alt_unit.errors.append("The pack unit must be different from the item's own unit.")
            ok = False
        if self.tenant is not None:
            from app.models.product import Product

            for field_ in (self.sku, self.barcode):
                if not field_.data:
                    continue
                clash = Product.query.filter(
                    Product.tenant_id == self.tenant.id,
                    getattr(Product, field_.name) == field_.data,
                    Product.id != (self.product_id or 0),
                ).first()
                if clash:
                    field_.errors.append(f"Already used by {clash.name}.")
                    ok = False
        return ok


class CategoryForm(FlaskForm):
    name = StringField("Category name", validators=[DataRequired(), Length(max=100)])


class SubCategoryForm(FlaskForm):
    name = StringField("Sub-category name", validators=[DataRequired(), Length(max=100)])
