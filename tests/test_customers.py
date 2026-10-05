from app.models.customer import Customer


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def test_a_blank_gstin_is_stored_as_null_not_a_blank_string(client, db, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post(
        "/customers/new",
        data={"name": "Walk-in Regular", "gstin": "", "state_code": "29"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    customer = Customer.query.filter_by(name="Walk-in Regular").first()
    assert customer is not None
    assert customer.gstin is None


def test_a_whitespace_only_gstin_is_stored_as_null(client, db, client_admin):
    # Regression: WTForms' Optional() skips the Regexp check for blank
    # input but leaves field.data as whatever was typed, so a stray
    # space used to end up stored as a truthy " " - which then
    # misclassified the customer as B2B in GSTR-1 reports.
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post(
        "/customers/new",
        data={"name": "Stray Space Co", "gstin": "   ", "state_code": "29"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    customer = Customer.query.filter_by(name="Stray Space Co").first()
    assert customer is not None
    assert customer.gstin is None


def test_a_lowercase_gstin_is_normalized_to_uppercase(client, db, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.post(
        "/customers/new",
        data={"name": "Lowercase GSTIN Co", "gstin": "29bbbbb0000b1z5", "state_code": "29"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    customer = Customer.query.filter_by(name="Lowercase GSTIN Co").first()
    assert customer is not None
    assert customer.gstin == "29BBBBB0000B1Z5"
