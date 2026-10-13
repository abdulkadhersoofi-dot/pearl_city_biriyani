from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.auth.decorators import roles_required
from app.extensions import db
from app.models.category import Category, SubCategory
from app.models.product import Product
from app.models.user import UserRole
from app.products import products_bp
from app.products.forms import CategoryForm, ProductForm, SubCategoryForm
from app.utils.tenant_scope import assert_owns, tenant_query
from app.utils.uploads import UploadError, delete_uploaded_image, save_uploaded_image

# POS tiles are square - crop/resize every product photo to match on
# upload so it fills the tile cleanly instead of being cropped live by
# CSS object-fit, whatever aspect ratio the original was.
PRODUCT_IMAGE_SIZE = 512


def _set_category_choices(form: ProductForm) -> None:
    """Populates the two optional pickers from this tenant's own active
    Category/SubCategory rows - blank first option means uncategorized.
    Only meaningful when the tenant has category_wise_items on; the
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
        query = query.filter(Product.name.ilike(f"%{search}%"))
    products = query.order_by(Product.name).all()
    return render_template("products/list.html", products=products, search=search)


@products_bp.route("/new", methods=["GET", "POST"])
@roles_required(UserRole.CLIENT_ADMIN)
def new_product():
    form = ProductForm()
    _set_category_choices(form)
    if form.validate_on_submit():
        product = Product(tenant_id=current_user.tenant_id)
        form.populate_obj(product)
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
    form = ProductForm(obj=product)
    _set_category_choices(form)
    if request.method == "GET":
        form.category_id.data = str(product.category_id) if product.category_id else ""
        form.sub_category_id.data = str(product.sub_category_id) if product.sub_category_id else ""
    if form.validate_on_submit():
        form.populate_obj(product)
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
def categories():
    cats = tenant_query(Category).filter_by(is_active=True).order_by(Category.sort_order, Category.name).all()
    return render_template(
        "products/categories.html", categories=cats, category_form=CategoryForm(), sub_category_form=SubCategoryForm()
    )


@products_bp.route("/categories/new", methods=["POST"])
@roles_required(UserRole.CLIENT_ADMIN)
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
def deactivate_sub_category(sub_category_id):
    sub_category = tenant_query(SubCategory).filter_by(id=sub_category_id).first_or_404()
    assert_owns(sub_category)
    sub_category.is_active = False
    db.session.commit()
    flash(f"{sub_category.name} removed.", "success")
    return redirect(url_for("products.categories"))
