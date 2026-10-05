from app.invoicing.routes import _tenant_document_type_choices
from app.models.invoice_series import DocumentType


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


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
