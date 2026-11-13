"""Phase 7 of the simple app: the search box finds records, a branch login
has three doors, and a guide says where every classic menu entry went."""

from datetime import date
from decimal import Decimal

from app import search
from app.models.customer import Customer
from app.models.inventory import ProductSerial, SerialStatus
from app.models.product import Product
from app.models.purchase import Supplier
from app.models.user import User, UserRole
from app.purchases import services as ps

D = Decimal


def _login(client, email="admin@acme.example.com", password="ClientSecret123"):
    client.post("/auth/login", data={"email": email, "password": password})


def _records(db, tenant, client_admin):
    for key in ("purchases", "inventory", "serial_imei", "receivables"):
        tenant.set_feature(key, True)
    customer = Customer(tenant_id=tenant.id, name="Meenakshi Stores", phone="9840012345", state_code="33")
    supplier = Supplier(tenant_id=tenant.id, name="Tumkur Spices Pvt Ltd", gstin="29AABCT1332L1ZA", state_code="29",
                        state_name="Karnataka")
    phone = Product(tenant_id=tenant.id, name="Galaxy M15 6/128", hsn_or_sac_code="8517", gst_rate=D("18"),
                    unit="NOS", sale_price=D("14999"), sku="SM-M156", barcode="8806095000012", track_serial=True)
    db.session.add_all([customer, supplier, phone])
    db.session.commit()
    order = ps.create_purchase_order(tenant, client_admin, supplier.id, tenant.gstins.first().id, [
        ps.PurchaseLineInput(product_id=phone.id, description=phone.name, hsn_or_sac_code="8517", qty=D("5"),
                             unit="NOS", rate=D("12000"), gst_rate=D("18"))], order_date=date.today())
    db.session.add(ProductSerial(tenant_id=tenant.id, product_id=phone.id, serial_no="356789104455667",
                                 status=SerialStatus.IN_STOCK))
    db.session.commit()
    return customer, supplier, phone, order


def test_search_finds_records_of_each_kind(app, db, tenant, client_admin):
    customer, supplier, phone, order = _records(db, tenant, client_admin)
    with app.test_request_context():
        def found(q):
            return {(r["group"], r["label"]): r for r in search.records(tenant, q)}

        assert ("Customer", "Meenakshi Stores") in found("meenak")
        by_phone = found("98400")[("Customer", "Meenakshi Stores")]
        assert by_phone["url"] == f"/accounts/customers/{customer.id}" or str(customer.id) in by_phone["url"]
        assert ("Supplier", "Tumkur Spices Pvt Ltd") in found("29AABCT")
        assert ("Item", "Galaxy M15 6/128") in found("sm-m156") and ("Item", "Galaxy M15 6/128") in found("8806095")
        serial = found("5566")[("Serial number", "356789104455667")]
        assert "Galaxy M15" in serial["detail"] and "q=356789104455667" in serial["url"]
        assert found(order.number[-6:])[("Purchase order", order.number)]["detail"] == "Tumkur Spices Pvt Ltd"
        # Too short to mean anything, and characters that mean "anything" in a pattern are taken as written.
        assert search.records(tenant, "m") == [] and search.records(tenant, "%%") == [] and search.records(tenant, "__") == []
        # A module that is off isn't searched.
        tenant.set_feature("purchases", False)
        db.session.commit()
        assert not [k for k in found("tumkur") if k[0] == "Supplier"]


def test_the_search_box_asks_the_server_and_only_for_the_owner(client, db, tenant, client_admin, staff):
    _records(db, tenant, client_admin)
    _login(client)
    page = client.get("/setup").get_data(as_text=True)
    assert 'data-find="/find.json"' in page and "an invoice number, a customer" in page
    data = client.get("/find.json?q=galaxy").get_json()
    assert [r["label"] for r in data["records"]] == ["Galaxy M15 6/128"]
    assert client.get("/find.json?q=").get_json() == {"records": []}
    client.get("/auth/logout")
    _login(client, staff.email, "CashierSecret123")
    assert client.get("/find.json?q=galaxy").status_code == 403


def test_one_business_never_finds_anothers_records(app, client, db, tenant, client_admin, super_admin):
    from app.models.tenant import BillingCycle, RegistrationType, Tenant

    _records(db, tenant, client_admin)
    other = Tenant(legal_name="Other Traders", registration_type=RegistrationType.REGULAR,
                   onboarded_by_id=super_admin.id, billing_cycle=BillingCycle.MONTHLY)
    db.session.add(other)
    db.session.flush()
    db.session.add(Customer(tenant_id=other.id, name="Meenakshi Hardware", state_code="33"))
    db.session.commit()
    with app.test_request_context():
        assert [r["label"] for r in search.records(tenant, "meenakshi")] == ["Meenakshi Stores"]
        assert [r["label"] for r in search.records(other, "meenakshi")] == ["Meenakshi Hardware"]


def test_a_branch_login_has_three_doors(client, db, tenant, client_admin):
    for key in ("pos", "warehouse_dispatch", "sales_documents", "inventory"):
        tenant.set_feature(key, True)
    branch = User(tenant_id=tenant.id, name="Anna Nagar", email="anna@acme.example.com", role=UserRole.STAFF,
                  must_change_password=False)
    branch.set_password("BranchSecret123")
    db.session.add(branch)
    db.session.commit()
    _login(client, "anna@acme.example.com", "BranchSecret123")
    page = client.get("/sell/new").get_data(as_text=True)
    from app.navigation import _branch_groups

    with client.application.test_request_context():
        assert [(g.key, g.label) for g in _branch_groups()] == [("counter", "Counter"), ("sell", "New sale"),
                                                                ("today", "Today")]
        assert [l.label for l in _branch_groups()[1].links] == ["New sale", "Sales orders"]
    assert page.count("New sale") >= 2 and "Sales orders" in page and "Day-book" in page


def test_the_guide_says_where_every_classic_entry_went(client, db, tenant, client_admin):
    for key in ("pos", "purchases", "inventory", "business_reports", "doc_scan", "receivables"):
        tenant.set_feature(key, True)
    db.session.commit()
    _login(client)
    page = client.get("/moved")
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and "What moved where" in html and "Nothing was removed" in html
    from app import doors
    from app.navigation import _client_groups

    with client.application.test_request_context():
        from flask_login import login_user

        login_user(client_admin)
        rows = doors.what_moved()
        shown = [l.label for g in _client_groups() for l in g.links if l.visible()]
        # Every classic entry this business has is on the guide, each behind a door.
        assert [r["label"] for r in rows] == shown and all(r["door"] for r in rows)
        by = {r["label"]: r["door"] for r in rows}
        assert by["POS"] == "Sell" and by["Purchases"] == "Buy" and by["Scan bills"] == "Buy"
    assert "What moved where" in client.get("/setup").get_data(as_text=True)
