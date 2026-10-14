from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.extensions import db
from app.models.category import Category, SubCategory
from app.models.price_list import PriceList, PriceListItem
from app.models.product import Product
from app.models.user import UserRole
from app.products import products_bp
from app.products.forms import CategoryForm, ProductForm, SubCategoryForm
from app.utils.audit import record_audit
from app.utils.features import feature_required
from app.utils.gst_config import UQC_CODES
from app.utils.tenant_scope import assert_owns, tenant_query
from app.utils.uploads import UploadError, delete_uploaded_image, save_uploaded_image

# POS tiles are square - crop/resize every product photo to match on
# upload so it fills the tile cleanly instead of being cropped live by
# CSS object-fit, whatever aspect ratio the original was.
PRODUCT_IMAGE_SIZE = 512


def _set_category_choices(form: ProductForm) -> None:
    """Populates the two optional pickers from this tenant's own active
    Category/SubCategory rows - blank first option means uncategorized.
    Only meaningful when the tenant has the "categories" feature on; the
    template hides both fields otherwise, but choices are always set so
    a submitted blank value still validates either way."""
    categories = tenant_query(Category).filter_by(is_active=True).order_by(Category.sort_order, Category.name).all()
    form.category_id.choices = [("", "Uncategorized")] + [(str(c.id), c.name) for c in categories]
    sub_categories = (
        tenant_query(SubCategory).filter_by(is_active=True).order_by(SubCategory.sort_order, SubCategory.name).all()
    )
    form.sub_category_id.choices = [("", "None")] + [
        (str(s.id), f"{s.category.name} › {s.name}") for s in sub_categories
    ]


def _allow_legacy_unit(form: ProductForm, unit: str | None) -> None:
    """An item saved before units became the GST portal's UQC list keeps
    its own free-text unit ("plate") selectable, so opening and saving it
    for an unrelated change doesn't silently rename what its bills print."""
    if unit and unit not in UQC_CODES:
        form.unit.choices = [(unit, f"{unit} (current unit - pick a standard one when you can)")] + list(
            form.unit.choices
        )


def _normalize_optional_fields(product: Product) -> None:
    product.alt_unit = product.alt_unit or None
    if not product.alt_unit:
        product.alt_unit_factor = None


def _apply_category_selection(form: ProductForm, product: Product) -> None:
    category_id = form.category_id.data or None
    sub_category_id = form.sub_category_id.data or None
    product.category_id = int(category_id) if category_id else None
    sub_category = (
        tenant_query(SubCategory).filter_by(id=int(sub_category_id)).first() if sub_category_id else None
    )
    # A sub-category whose own category doesn't match the chosen category
    # (or no category chosen at all) is dropped rather than left to form
    # an inconsistent pair - the cascading picker in the UI prevents this
    # in practice, but this is the data-level guarantee.
    if sub_category and product.category_id and sub_category.category_id == product.category_id:
        product.sub_category_id = sub_category.id
    else:
        product.sub_category_id = None


@products_bp.route("/")
@roles_required(UserRole.CLIENT_ADMIN)
def list_products():
    search = request.args.get("q", "").strip()
    query = tenant_query(Product).filter_by(is_active=True)
    if search:
        like = f"%{search}%"
        query = query.filter(
            db.or_(Product.name.ilike(like), Product.sku.ilike(like), Product.barcode == search)
        )
    products = query.order_by(Product.name).all()
    return render_template("products/list.html", products=products, search=search)


@products_bp.route("/new", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def new_product():
    tenant = current_user.tenant
    form = ProductForm(tenant=tenant)
    _set_category_choices(form)
    if request.method == "GET":
        form.price_includes_tax.data = tenant.default_price_includes_tax
    if form.validate_on_submit():
        product = Product(tenant_id=current_user.tenant_id)
        form.populate_obj(product)
        if not tenant.has_feature("favourites"):
            product.is_favourite = False
        _normalize_optional_fields(product)
        _apply_category_selection(form, product)
        try:
            product.image_id = save_uploaded_image(form.image.data, square_size=PRODUCT_IMAGE_SIZE)
        except UploadError as exc:
            flash(exc.message, "error")
            return render_template("products/form.html", form=form, product=None)
        db.session.add(product)
        db.session.commit()
        flash(f"{product.name} added.", "success")
        return redirect(url_for("products.list_products"))
    return render_template("products/form.html", form=form, product=None)


@products_bp.route("/<int:product_id>/edit", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def edit_product(product_id):
    product = tenant_query(Product).filter_by(id=product_id).first_or_404()
    assert_owns(product)
    form = ProductForm(obj=product, tenant=current_user.tenant, product_id=product.id)
    _set_category_choices(form)
    _allow_legacy_unit(form, product.unit)
    if request.method == "GET":
        form.category_id.data = str(product.category_id) if product.category_id else ""
        form.sub_category_id.data = str(product.sub_category_id) if product.sub_category_id else ""
        form.alt_unit.data = product.alt_unit or ""
    if form.validate_on_submit():
        was_favourite = product.is_favourite
        form.populate_obj(product)
        if not current_user.tenant.has_feature("favourites"):
            product.is_favourite = was_favourite  # the box wasn't on the page
        _normalize_optional_fields(product)
        _apply_category_selection(form, product)
        if form.image.data and form.image.data.filename:
            try:
                new_image_id = save_uploaded_image(form.image.data, square_size=PRODUCT_IMAGE_SIZE)
            except UploadError as exc:
                flash(exc.message, "error")
                return render_template("products/form.html", form=form, product=product)
            # New row referenced (and flushed) before the old one is
            # deleted - the other way around trips the foreign key.
            old_image_id = product.image_id
            product.image_id = new_image_id
            db.session.flush()
            delete_uploaded_image(old_image_id)
        db.session.commit()
        flash(f"{product.name} updated.", "success")
        return redirect(url_for("products.list_products"))
    return render_template("products/form.html", form=form, product=product)


@products_bp.route("/<int:product_id>/deactivate", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def deactivate_product(product_id):
    product = tenant_query(Product).filter_by(id=product_id).first_or_404()
    assert_owns(product)
    product.is_active = False
    db.session.commit()
    flash(f"{product.name} removed from active list.", "success")
    return redirect(url_for("products.list_products"))


@products_bp.route("/categories")
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("categories")
def categories():
    cats = tenant_query(Category).filter_by(is_active=True).order_by(Category.sort_order, Category.name).all()
    return render_template(
        "products/categories.html", categories=cats, category_form=CategoryForm(), sub_category_form=SubCategoryForm()
    )


@products_bp.route("/categories/new", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("categories")
def new_category():
    form = CategoryForm()
    if form.validate_on_submit():
        db.session.add(Category(tenant_id=current_user.tenant_id, name=form.name.data.strip()))
        db.session.commit()
        flash(f"Category '{form.name.data.strip()}' added.", "success")
    else:
        flash("Enter a category name.", "error")
    return redirect(url_for("products.categories"))


@products_bp.route("/categories/<int:category_id>/deactivate", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("categories")
def deactivate_category(category_id):
    category = tenant_query(Category).filter_by(id=category_id).first_or_404()
    assert_owns(category)
    category.is_active = False
    # Items already pointing at this category/its sub-categories just
    # keep category_id/sub_category_id set - they fall back to showing
    # under "Uncategorized" in the POS grouping (which only ever iterates
    # *active* categories) without needing a bulk update here.
    for sub in category.sub_categories:
        sub.is_active = False
    db.session.commit()
    flash(f"{category.name} removed.", "success")
    return redirect(url_for("products.categories"))


@products_bp.route("/categories/<int:category_id>/sub-categories/new", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("categories")
def new_sub_category(category_id):
    category = tenant_query(Category).filter_by(id=category_id).first_or_404()
    assert_owns(category)
    form = SubCategoryForm()
    if form.validate_on_submit():
        db.session.add(
            SubCategory(tenant_id=current_user.tenant_id, category_id=category.id, name=form.name.data.strip())
        )
        db.session.commit()
        flash(f"Sub-category '{form.name.data.strip()}' added under {category.name}.", "success")
    else:
        flash("Enter a sub-category name.", "error")
    return redirect(url_for("products.categories"))


@products_bp.route("/sub-categories/<int:sub_category_id>/deactivate", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("categories")
def deactivate_sub_category(sub_category_id):
    sub_category = tenant_query(SubCategory).filter_by(id=sub_category_id).first_or_404()
    assert_owns(sub_category)
    sub_category.is_active = False
    db.session.commit()
    flash(f"{sub_category.name} removed.", "success")
    return redirect(url_for("products.categories"))


# --- price lists (feature "multi_price_list") -----------------------------------

@products_bp.route("/price-lists")
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("multi_price_list")
def price_lists():
    lists = tenant_query(PriceList).order_by(PriceList.is_active.desc(), PriceList.name).all()
    return render_template("products/price_lists.html", lists=lists)


@products_bp.route("/price-lists/new", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("multi_price_list")
def new_price_list():
    name = (request.form.get("name") or "").strip()[:100]
    if not name:
        flash("Give the price list a name.", "error")
        return redirect(url_for("products.price_lists"))
    if tenant_query(PriceList).filter(db.func.lower(PriceList.name) == name.lower()).first():
        flash(f"There's already a price list called {name}.", "error")
        return redirect(url_for("products.price_lists"))
    price_list = PriceList(tenant_id=current_user.tenant_id, name=name)
    db.session.add(price_list)
    db.session.flush()
    if request.form.get("copy_wholesale"):
        for product in tenant_query(Product).filter(Product.is_active.is_(True), Product.wholesale_price.isnot(None)):
            db.session.add(PriceListItem(tenant_id=current_user.tenant_id, price_list_id=price_list.id,
                                         product_id=product.id, price=product.wholesale_price))
    record_audit(current_user, "price_list_created", tenant_id=current_user.tenant_id, entity_type="price_list",
                 entity_id=price_list.id, details={"name": name})
    db.session.commit()
    flash(f"Price list {name} created - set its prices below.", "success")
    return redirect(url_for("products.edit_price_list", list_id=price_list.id))


@products_bp.route("/price-lists/<int:list_id>", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("multi_price_list")
def edit_price_list(list_id):
    price_list = tenant_query(PriceList).filter_by(id=list_id).first_or_404()
    assert_owns(price_list)
    products = tenant_query(Product).filter_by(is_active=True).order_by(Product.name).all()
    if request.method == "POST":
        from decimal import Decimal, InvalidOperation

        current = {item.product_id: item for item in price_list.items}
        errors = []
        changed = 0
        for product in products:
            raw = (request.form.get(f"price_{product.id}") or "").strip()
            item = current.get(product.id)
            if not raw:
                if item is not None:
                    db.session.delete(item)
                    changed += 1
                continue
            try:
                price = Decimal(raw).quantize(Decimal("0.01"))
            except InvalidOperation:
                errors.append(product.name)
                continue
            if price < 0:
                errors.append(product.name)
                continue
            if item is None:
                db.session.add(PriceListItem(tenant_id=current_user.tenant_id, price_list_id=price_list.id,
                                             product_id=product.id, price=price))
                changed += 1
            elif item.price != price:
                item.price = price
                changed += 1
        if errors:
            db.session.rollback()
            flash("Not a valid price for: " + ", ".join(errors[:5]) + ". Nothing was saved.", "error")
            return redirect(url_for("products.edit_price_list", list_id=price_list.id))
        record_audit(current_user, "price_list_updated", tenant_id=current_user.tenant_id, entity_type="price_list",
                     entity_id=price_list.id, details={"name": price_list.name, "changed": changed})
        db.session.commit()
        flash(f"{price_list.name}: {changed} price(s) updated.", "success")
        return redirect(url_for("products.edit_price_list", list_id=price_list.id))
    return render_template("products/price_list_edit.html", price_list=price_list, products=products,
                           prices=price_list.price_map())


@products_bp.route("/price-lists/<int:list_id>/deactivate", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
@feature_required("multi_price_list")
def deactivate_price_list(list_id):
    from app.models.user import User

    price_list = tenant_query(PriceList).filter_by(id=list_id).first_or_404()
    assert_owns(price_list)
    price_list.is_active = False
    # Counters billing from it go back to normal prices.
    for user in tenant_query(User).filter_by(price_list_id=price_list.id):
        user.price_list_id = None
    record_audit(current_user, "price_list_deactivated", tenant_id=current_user.tenant_id, entity_type="price_list",
                 entity_id=price_list.id, details={"name": price_list.name})
    db.session.commit()
    flash(f"{price_list.name} switched off.", "success")
    return redirect(url_for("products.price_lists"))
