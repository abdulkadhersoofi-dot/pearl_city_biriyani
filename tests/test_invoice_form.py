from app.invoicing.routes import _tenant_document_type_choices
from app.models.customer import Customer
from app.models.invoice_series import DocumentType


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _new_invoice_form_data(tenant, **overrides):
    gstin = tenant.gstins.first()
    data = {
        "gstin_id": str(gstin.id),
        "document_type": "tax_invoice",
        "customer_id": "new",
        "new_customer_name": "Brand New Customer",
        "new_customer_gstin": "",
        "new_customer_address": "",
        "new_customer_state_code": "",
        "place_of_supply_state_code": gstin.state_code,
        "invoice_date": "2026-10-05",
        "notes": "",
        "line_description[]": "Catering",
        "line_hsn[]": "996331",
        "line_qty[]": "1",
        "line_rate[]": "100",
        "line_discount[]": "0",
        "line_gst_rate[]": "18",
        "line_product_id[]": "",
        "line_unit[]": "pcs",
    }
    data.update(overrides)
    return data


def test_tax_invoice_is_the_first_and_default_document_type_choice(client, db, client_admin):
    # Regression: building the choices from a set() intersection iterated
    # in whatever order the enum members happened to hash to, which could
    # (and did) put Reverse Charge Invoice first/selected-by-default
    # instead of the common case, Tax Invoice.
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get("/invoices/new")
    assert resp.status_code == 200
    body = resp.data.decode()
    tax_invoice_pos = body.find('value="tax_invoice"')
    rcm_pos = body.find('value="rcm_invoice"')
    assert tax_invoice_pos != -1
    assert rcm_pos == -1 or tax_invoice_pos < rcm_pos


def test_document_type_choices_are_ordered_tax_invoice_first(app, db, client_admin):
    with app.test_request_context():
        from flask_login import login_user

        login_user(client_admin)
        choices = _tenant_document_type_choices()
        assert choices[0][0] == DocumentType.TAX_INVOICE.value


def test_a_blank_new_customer_gstin_creates_an_unregistered_customer(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post("/invoices/new", data=_new_invoice_form_data(tenant), follow_redirects=True)
    assert resp.status_code == 200
    customer = Customer.query.filter_by(name="Brand New Customer").first()
    assert customer is not None
    assert customer.gstin is None


def test_a_valid_gstin_is_accepted_for_a_new_invoice_customer(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post(
        "/invoices/new",
        data=_new_invoice_form_data(tenant, new_customer_gstin="33cwwpk2162h1za"),
        follow_redirects=True,
    )
    assert resp.status_code == 200
    customer = Customer.query.filter_by(name="Brand New Customer").first()
    assert customer is not None
    assert customer.gstin == "33CWWPK2162H1ZA"


def test_nil_is_rejected_as_a_new_invoice_customers_gstin(client, db, tenant, client_admin):
    # Regression: this field had no format validation at all (just a
    # max-length check), so typing "NIL", "-", "N/A" etc. to mean
    # "unregistered" was silently accepted as a literal GSTIN value -
    # misclassifying the sale as B2B. Only a real GSTIN or a blank field
    # means registered/unregistered now.
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post("/invoices/new", data=_new_invoice_form_data(tenant, new_customer_gstin="NIL"))
    assert resp.status_code == 200
    assert Customer.query.filter_by(name="Brand New Customer").first() is None
    assert b"Enter a valid 15-character GSTIN" in resp.data


def test_a_dash_is_rejected_as_a_new_invoice_customers_gstin(client, db, tenant, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post("/invoices/new", data=_new_invoice_form_data(tenant, new_customer_gstin="-"))
    assert resp.status_code == 200
    assert Customer.query.filter_by(name="Brand New Customer").first() is None
    assert b"Enter a valid 15-character GSTIN" in resp.data
