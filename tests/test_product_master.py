"""Item master upgrades: UQC units, codes, prices, MRP, packs, HSN by AATO."""

from decimal import Decimal

from app.models.product import Product
from app.models.tenant import RegistrationType, Tenant


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _form(**overrides):
    data = {
        "name": "Basmati Rice 1kg",
        "description": "",
        "hsn_or_sac_code": "1006",
        "gst_rate": "5",
        "sale_price": "120",
        "unit": "PCS",
        "alt_unit": "",
    }
    data.update(overrides)
    return data


def test_item_saves_every_new_master_field(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post(
        "/products/new",
        data=_form(
            price_includes_tax="y",
            mrp="125",
            wholesale_price="110",
            purchase_rate="95",
            cost_price="96.5",
            sku="RICE-1KG",
            barcode="8901234567890",
            alt_unit="BAG",
            alt_unit_factor="25",
            min_stock_level="10",
            allow_negative_stock="y",
        ),
        follow_redirects=True,
    )
    assert resp.status_code == 200
    p = Product.query.filter_by(name="Basmati Rice 1kg").one()
    assert p.sale_price == Decimal("120.00") and p.price_includes_tax is True
    assert p.mrp == Decimal("125.00") and p.wholesale_price == Decimal("110.00")
    assert p.purchase_rate == Decimal("95.00") and p.cost_price == Decimal("96.50")
    assert (p.sku, p.barcode) == ("RICE-1KG", "8901234567890")
    assert (p.alt_unit, p.alt_unit_factor) == ("BAG", Decimal("25.000"))
    assert p.min_stock_level == Decimal("10.000")
    assert p.unit == "PCS" and p.is_service is False


def test_unit_must_be_a_gst_uqc_code_but_case_is_forgiven(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    client.post("/products/new", data=_form(name="A", unit="kgs"))
    assert Product.query.filter_by(name="A").one().unit == "KGS"
    resp = client.post("/products/new", data=_form(name="B", unit="plate"))
    assert resp.status_code == 200
    assert Product.query.filter_by(name="B").first() is None


def test_editing_an_old_item_keeps_its_pre_uqc_unit(client, db, tenant, client_admin):
    p = Product(tenant_id=tenant.id, name="Chicken Biriyani", hsn_or_sac_code="996331", gst_rate=Decimal("5"), unit="plate", sale_price=Decimal("150"))
    db.session.add(p)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post(f"/products/{p.id}/edit", data=_form(name="Chicken Biriyani", hsn_or_sac_code="996331", unit="plate", sale_price="160"), follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(p)
    assert p.unit == "plate" and p.sale_price == Decimal("160.00")


def test_sale_price_above_mrp_is_refused(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    # 110 + 5% GST = 115.50 > MRP 115
    resp = client.post("/products/new", data=_form(sale_price="110", mrp="115"))
    assert b"more than the MRP" in resp.data
    assert Product.query.count() == 0
    client.post("/products/new", data=_form(sale_price="110", mrp="115.50"))
    assert Product.query.count() == 1


def test_pack_unit_and_factor_go_together(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post("/products/new", data=_form(alt_unit="BOX"))
    assert b"Set both the pack unit" in resp.data
    resp = client.post("/products/new", data=_form(alt_unit="PCS", alt_unit_factor="12"))
    assert b"must be different" in resp.data
    assert Product.query.count() == 0


def test_hsn_digits_follow_the_clients_turnover(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    assert Product.query.count() == 0
    client.post("/products/new", data=_form(hsn_or_sac_code="1006"))
    assert Product.query.count() == 1

    tenant.aato = Decimal("75000000")  # 7.5 crore
    db.session.commit()
    resp = client.post("/products/new", data=_form(name="Other rice", hsn_or_sac_code="1006"))
    assert b"at least 6 digits" in resp.data
    client.post("/products/new", data=_form(name="Other rice", hsn_or_sac_code="100630"))
    assert Product.query.count() == 2


def test_service_needs_a_sac_code(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post("/products/new", data=_form(name="Repair labour", is_service="y", hsn_or_sac_code="8517"))
    assert b"starts with 99" in resp.data
    client.post("/products/new", data=_form(name="Repair labour", is_service="y", hsn_or_sac_code="998719", gst_rate="18"))
    assert Product.query.filter_by(name="Repair labour").one().is_service is True


def test_sku_and_barcode_are_unique_per_client_only(client, db, tenant, client_admin):
    other = Tenant(legal_name="Other", registration_type=RegistrationType.REGULAR)
    db.session.add(other)
    db.session.flush()
    db.session.add(Product(tenant_id=other.id, name="Theirs", hsn_or_sac_code="1006", sku="X1", barcode="111", sale_price=Decimal("1")))
    db.session.commit()

    _login(client, client_admin.email, "ClientSecret123")
    client.post("/products/new", data=_form(name="Mine", sku="X1", barcode="111"))
    assert Product.query.filter_by(tenant_id=tenant.id, sku="X1").count() == 1  # another client's code doesn't clash

    resp = client.post("/products/new", data=_form(name="Mine again", sku="X1"))
    assert b"Already used by Mine" in resp.data
    resp = client.post("/products/new", data=_form(name="Mine again", barcode="111"))
    assert b"Already used by Mine" in resp.data


def test_new_item_defaults_to_the_clients_gst_included_setting(client, db, tenant, client_admin):
    tenant.default_price_includes_tax = True
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    # A server-computed attribute (not Alpine markup): WTForms renders a
    # ticked BooleanField as <input checked id=...>.
    assert b'<input checked id="price_includes_tax"' in client.get("/products/new").data
    tenant.default_price_includes_tax = False
    db.session.commit()
    assert b'<input checked id="price_includes_tax"' not in client.get("/products/new").data


def test_item_list_searches_sku_and_barcode(client, db, tenant, client_admin):
    db.session.add(Product(tenant_id=tenant.id, name="Pen", hsn_or_sac_code="9608", sku="STN-PEN", barcode="890100", sale_price=Decimal("10")))
    db.session.add(Product(tenant_id=tenant.id, name="Pencil", hsn_or_sac_code="9609", sale_price=Decimal("5")))
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    assert b"Pencil" not in client.get("/products/?q=STN").data
    body = client.get("/products/?q=890100").data
    assert b"Pen" in body and b"Pencil" not in body


def test_an_item_without_a_category_can_be_edited_on_a_fresh_request(app, client, db, tenant, client_admin,
                                                                     super_admin):
    # In production every request starts with an empty database session;
    # acting as the client, nothing loads the client's package before the
    # save's module check does - which first writes the item as the form
    # left it. The blank category must
    # already be "none" by then, not "" in a number column (that was a 500).
    p = Product(tenant_id=tenant.id, name="Consulting", hsn_or_sac_code="9983", gst_rate=Decimal("18"), unit="NOS",
                sale_price=Decimal("1000"), is_service=True)
    db.session.add(p)
    db.session.commit()
    product_id = p.id
    _login(client, client_admin.email, "ClientSecret123")
    with client.session_transaction() as sess:
        sess["impersonator_id"] = super_admin.id  # the Ultra Admin acting as the client
    with app.app_context():  # its own session and signed-in user, as in production
        resp = client.post(f"/products/{product_id}/edit", data=_form(
            name="Consulting", hsn_or_sac_code="9963", unit="NOS", sale_price="1000", gst_rate="18", is_service="y",
            category_id="", sub_category_id=""))
    assert resp.status_code == 302
    db.session.expire_all()
    p = db.session.get(Product, product_id)
    assert p.hsn_or_sac_code == "9963" and p.category_id is None and p.sub_category_id is None
