from decimal import Decimal

from app.models.category import Category, SubCategory
from app.models.product import Product
from app.pos.catalog import build_category_tree


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _enable_category_mode(db, tenant):
    tenant.set_feature("categories", True)
    db.session.commit()


# --- Settings toggle -------------------------------------------------------

def test_settings_turns_category_mode_on(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post(
        "/settings/",
        data={
            "primary_color": "#1f7a4d",
            "background_color": "#f6f7f5",
            "text_color": "#1f2a24",
            "font_family": "system",
            "default_receipt_format": "3in",
            "feature_categories": "y",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(tenant)
    assert tenant.has_feature("categories") is True


def test_settings_leaves_category_mode_off_when_unchecked(client, db, tenant, client_admin):
    _enable_category_mode(db, tenant)
    _login(client, client_admin.email, "ClientSecret123")
    client.post(
        "/settings/",
        data={
            "primary_color": "#1f7a4d",
            "background_color": "#f6f7f5",
            "text_color": "#1f2a24",
            "font_family": "system",
            "default_receipt_format": "3in",
            # feature_categories omitted - an unchecked checkbox sends nothing
        },
    )
    db.session.refresh(tenant)
    assert tenant.has_feature("categories") is False


# --- Category/sub-category CRUD --------------------------------------------

def test_client_admin_creates_a_category_and_sub_category(client, db, tenant, client_admin):
    _enable_category_mode(db, tenant)
    _login(client, client_admin.email, "ClientSecret123")

    resp = client.post("/products/categories/new", data={"name": "Biriyani"}, follow_redirects=True)
    assert resp.status_code == 200
    category = Category.query.filter_by(tenant_id=tenant.id, name="Biriyani").first()
    assert category is not None

    resp = client.post(
        f"/products/categories/{category.id}/sub-categories/new", data={"name": "Chicken"}, follow_redirects=True
    )
    assert resp.status_code == 200
    sub = SubCategory.query.filter_by(tenant_id=tenant.id, name="Chicken").first()
    assert sub is not None
    assert sub.category_id == category.id


def test_deactivating_a_category_cascades_to_its_sub_categories(client, db, tenant, client_admin):
    _enable_category_mode(db, tenant)
    category = Category(tenant_id=tenant.id, name="Drinks")
    db.session.add(category)
    db.session.flush()
    sub = SubCategory(tenant_id=tenant.id, category_id=category.id, name="Soft drinks")
    db.session.add(sub)
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")
    client.post(f"/products/categories/{category.id}/deactivate", follow_redirects=True)

    db.session.refresh(category)
    db.session.refresh(sub)
    assert category.is_active is False
    assert sub.is_active is False


def test_client_admin_cannot_manage_another_tenants_category(client, db, tenant, client_admin):
    from app.models.tenant import RegistrationType, Tenant

    other_tenant = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR)
    db.session.add(other_tenant)
    db.session.flush()
    other_category = Category(tenant_id=other_tenant.id, name="Not Yours")
    db.session.add(other_category)
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post(f"/products/categories/{other_category.id}/deactivate")
    assert resp.status_code == 404


# --- Product form: category is always optional ------------------------------

def test_item_can_be_created_with_a_category_and_sub_category(client, db, tenant, client_admin):
    _enable_category_mode(db, tenant)
    category = Category(tenant_id=tenant.id, name="Biriyani")
    db.session.add(category)
    db.session.flush()
    sub = SubCategory(tenant_id=tenant.id, category_id=category.id, name="Chicken")
    db.session.add(sub)
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post(
        "/products/new",
        data={
            "name": "Chicken Dum Biriyani",
            "description": "",
            "hsn_or_sac_code": "2106",
            "gst_rate": "5",
            "unit": "NOS",
            "sale_price": "180",
            "category_id": str(category.id),
            "sub_category_id": str(sub.id),
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    product = Product.query.filter_by(name="Chicken Dum Biriyani").first()
    assert product is not None
    assert product.category_id == category.id
    assert product.sub_category_id == sub.id


def test_item_can_be_created_with_no_category_even_when_mode_is_on(client, db, tenant, client_admin):
    _enable_category_mode(db, tenant)
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post(
        "/products/new",
        data={
            "name": "Mineral Water",
            "description": "",
            "hsn_or_sac_code": "2201",
            "gst_rate": "18",
            "unit": "BTL",
            "sale_price": "20",
            "category_id": "",
            "sub_category_id": "",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    product = Product.query.filter_by(name="Mineral Water").first()
    assert product is not None
    assert product.category_id is None
    assert product.sub_category_id is None


def test_a_sub_category_from_a_different_category_is_dropped(client, db, tenant, client_admin):
    # Submitting a mismatched category/sub-category pair (shouldn't
    # happen through the real UI, but the server must not trust it)
    # keeps the category and drops the inconsistent sub-category.
    _enable_category_mode(db, tenant)
    cat_a = Category(tenant_id=tenant.id, name="Biriyani")
    cat_b = Category(tenant_id=tenant.id, name="Drinks")
    db.session.add_all([cat_a, cat_b])
    db.session.flush()
    sub_under_b = SubCategory(tenant_id=tenant.id, category_id=cat_b.id, name="Soft drinks")
    db.session.add(sub_under_b)
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")
    client.post(
        "/products/new",
        data={
            "name": "Mismatched Item",
            "description": "",
            "hsn_or_sac_code": "2106",
            "gst_rate": "5",
            "unit": "NOS",
            "sale_price": "100",
            "category_id": str(cat_a.id),
            "sub_category_id": str(sub_under_b.id),
        },
    )
    product = Product.query.filter_by(name="Mismatched Item").first()
    assert product is not None
    assert product.category_id == cat_a.id
    assert product.sub_category_id is None


# --- build_category_tree -----------------------------------------------------

def test_build_category_tree_groups_products_correctly(db, tenant):
    cat = Category(tenant_id=tenant.id, name="Biriyani")
    db.session.add(cat)
    db.session.flush()
    sub = SubCategory(tenant_id=tenant.id, category_id=cat.id, name="Chicken")
    db.session.add(sub)
    db.session.flush()

    in_sub = Product(tenant_id=tenant.id, name="Chicken Biriyani", hsn_or_sac_code="2106", sale_price=Decimal("180"), category_id=cat.id, sub_category_id=sub.id)
    loose_in_cat = Product(tenant_id=tenant.id, name="Papad", hsn_or_sac_code="2106", sale_price=Decimal("20"), category_id=cat.id)
    uncategorized = Product(tenant_id=tenant.id, name="Water", hsn_or_sac_code="2201", sale_price=Decimal("20"))
    db.session.add_all([in_sub, loose_in_cat, uncategorized])
    db.session.commit()

    tree, uncategorized_products = build_category_tree([in_sub, loose_in_cat, uncategorized])

    assert len(tree) == 1
    assert tree[0].name == "Biriyani"
    assert [p.name for p in tree[0].loose_products] == ["Papad"]
    assert len(tree[0].subcategories) == 1
    assert tree[0].subcategories[0].name == "Chicken"
    assert [p.name for p in tree[0].subcategories[0].products] == ["Chicken Biriyani"]
    assert [p.name for p in uncategorized_products] == ["Water"]


def test_build_category_tree_treats_a_deactivated_category_as_uncategorized(db, tenant):
    cat = Category(tenant_id=tenant.id, name="Discontinued", is_active=False)
    db.session.add(cat)
    db.session.flush()
    product = Product(tenant_id=tenant.id, name="Old Item", hsn_or_sac_code="2106", sale_price=Decimal("50"), category_id=cat.id)
    db.session.add(product)
    db.session.commit()

    tree, uncategorized_products = build_category_tree([product])
    assert tree == []
    assert [p.name for p in uncategorized_products] == ["Old Item"]


# --- POS terminal renders the category tree ---------------------------------

def test_pos_terminal_shows_category_names_when_mode_is_on(client, db, tenant, client_admin, staff):
    # x-if is client-side (Alpine, evaluated in a real browser) - the
    # "Expand all" markup is always present in raw server HTML either
    # way, so what's actually testable here server-side is the JS data:
    # the CATEGORY_WISE flag and the category tree's contents.
    _enable_category_mode(db, tenant)
    cat = Category(tenant_id=tenant.id, name="Biriyani")
    db.session.add(cat)
    db.session.flush()
    db.session.add(Product(tenant_id=tenant.id, name="Chicken Biriyani", hsn_or_sac_code="2106", sale_price=Decimal("180"), category_id=cat.id))
    db.session.commit()

    _login(client, staff.email, "CashierSecret123")
    resp = client.get("/pos/")
    assert resp.status_code == 200
    assert b"const CATEGORY_WISE = true;" in resp.data
    assert b"Biriyani" in resp.data


def test_pos_terminal_category_tree_is_empty_when_mode_is_off(client, db, tenant, staff):
    _login(client, staff.email, "CashierSecret123")
    resp = client.get("/pos/")
    assert resp.status_code == 200
    assert b"const CATEGORY_WISE = false;" in resp.data
