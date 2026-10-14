"""Resuming held POS bills.

The test client normally shares one database session across requests,
so objects loaded by an earlier request are still in memory - which hid
a crash that real (one-session-per-request) servers hit: resuming a
held bill once rendered the page from rows it had already deleted. Each
request here starts from a cleared session, like production."""

import json
import re
from decimal import Decimal

import pytest

from app.models.customer import Customer
from app.models.pos_bill import POSBill, POSBillStatus
from app.models.product import Product


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _fresh(db):
    """Forget everything loaded so far - the next request starts cold.
    (The test app context lives across requests, so Flask-Login's cached
    user has to go too, as it would in a real new request.)"""
    from flask import g

    db.session.commit()
    db.session.remove()
    g.pop("_login_user", None)


def _prefill(html: str) -> dict:
    match = re.search(r"\{lines: \[\], customer: null, bill_disc_kind: \"\", bill_disc_value: 0, price_list_id: null\},\s*(\{.*?\})\s*\);", html, re.S)
    assert match, "no PREFILL block on the page"
    return json.loads(match.group(1))


@pytest.fixture()
def items(db, tenant):
    tea = Product(tenant_id=tenant.id, name="Tea", hsn_or_sac_code="0902", gst_rate=Decimal("5"), unit="NOS", sale_price=Decimal("20"))
    rice = Product(tenant_id=tenant.id, name="Rice", hsn_or_sac_code="1006", gst_rate=Decimal("5"), unit="KGS", sale_price=Decimal("60"), price_includes_tax=True)
    db.session.add_all([tea, rice])
    db.session.commit()
    return tea.id, rice.id


def _hold(client, label, lines, **extra):
    data = {"label": label, **extra}
    for key in ("line_product_id[]", "line_description[]", "line_hsn[]", "line_qty[]", "line_rate[]", "line_gst_rate[]",
                "line_incl[]", "line_unit[]", "line_discount_kind[]", "line_discount_value[]"):
        data[key] = []
    for pid, name, hsn, qty, rate, unit, dk, dv in lines:
        data["line_product_id[]"].append(str(pid)); data["line_description[]"].append(name); data["line_hsn[]"].append(hsn)
        data["line_qty[]"].append(qty); data["line_rate[]"].append(rate); data["line_gst_rate[]"].append("5")
        data["line_incl[]"].append("0"); data["line_unit[]"].append(unit)
        data["line_discount_kind[]"].append(dk); data["line_discount_value[]"].append(dv)
    return client.post("/pos/hold", data=data)


def _held_ids(db):
    return [b.id for b in POSBill.query.filter_by(status=POSBillStatus.HELD).order_by(POSBill.id)]


def test_resume_one_of_several_held_bills_from_a_cold_session(client, db, tenant, client_admin, items):
    tea, rice = items
    buyer = Customer(tenant_id=tenant.id, name="Ravi", phone="9876543210")
    db.session.add(buyer)
    db.session.commit()
    buyer_id = buyer.id
    _login(client, client_admin.email, "ClientSecret123")
    _hold(client, "Table 1", [(tea, "Tea", "0902", "2", "20", "NOS", "percent", "10"), (rice, "Rice", "1006", "0.75", "60", "KGS", "", "")],
          customer_id=str(buyer_id), bill_discount_kind="amount", bill_discount_value="5")
    _hold(client, "Table 2", [(tea, "Tea", "0902", "1", "20", "NOS", "", "")])
    _hold(client, "Table 3", [(rice, "Rice", "1006", "1.5", "60", "KGS", "", "")])
    first, second, third = _held_ids(db)

    _fresh(db)
    resp = client.get(f"/pos/?resume={first}")
    assert resp.status_code == 200
    prefill = _prefill(resp.data.decode())
    assert [(l["description"], l["qty"], l["loose"], l["disc_kind"], l["disc_value"]) for l in prefill["lines"]] == [
        ("Tea", 2.0, False, "percent", 10.0),
        ("Rice", 0.75, True, "", 0.0),
    ]
    assert prefill["customer"]["name"] == "Ravi"
    assert (prefill["bill_disc_kind"], prefill["bill_disc_value"]) == ("amount", 5.0)
    assert _held_ids(db) == [second, third]

    # the others still resume, one after another
    for bill_id, remaining in ((third, [second]), (second, [])):
        _fresh(db)
        resp = client.get(f"/pos/?resume={bill_id}")
        assert resp.status_code == 200
        assert _prefill(resp.data.decode())["lines"]
        assert _held_ids(db) == remaining


def test_bill_held_by_another_login_resumes(client, db, tenant, client_admin, staff, items):
    tea, _ = items
    _login(client, staff.email, "CashierSecret123")
    _hold(client, "From the counter", [(tea, "Tea", "0902", "1", "20", "NOS", "", "")])
    _hold(client, "Second", [(tea, "Tea", "0902", "3", "20", "NOS", "", "")])
    bill_id = _held_ids(db)[0]
    client.get("/auth/logout")
    _login(client, client_admin.email, "ClientSecret123")
    _fresh(db)
    resp = client.get(f"/pos/?resume={bill_id}")
    assert resp.status_code == 200
    assert _prefill(resp.data.decode())["lines"][0]["qty"] == 1.0


def test_resuming_a_bill_that_is_already_gone_is_harmless(client, db, client_admin, items):
    _login(client, client_admin.email, "ClientSecret123")
    resp = client.get("/pos/?resume=999999")
    assert resp.status_code == 200
    resp = client.get("/pos/?resume=not-a-number")
    assert resp.status_code == 200


def test_a_page_that_fails_to_render_keeps_the_held_bill(client, db, tenant, client_admin, items, monkeypatch):
    """If anything goes wrong while drawing the page, the held bill must
    still be there afterwards - it is only removed once the page is ready."""
    tea, _ = items
    _login(client, client_admin.email, "ClientSecret123")
    _hold(client, "Keep me", [(tea, "Tea", "0902", "1", "20", "NOS", "", "")])
    bill_id = _held_ids(db)[0]

    import app.pos.routes as routes

    def boom(*args, **kwargs):
        raise RuntimeError("render failed")

    monkeypatch.setattr(routes, "render_template", boom)
    _fresh(db)
    client.application.config["PROPAGATE_EXCEPTIONS"] = False
    resp = client.get(f"/pos/?resume={bill_id}")
    assert resp.status_code == 500
    _fresh(db)
    assert _held_ids(db) == [bill_id]


def test_resuming_another_bill_holds_the_open_cart_first(client, db, tenant, client_admin, items):
    tea, rice = items
    _login(client, client_admin.email, "ClientSecret123")
    _hold(client, "Waiting", [(tea, "Tea", "0902", "1", "20", "NOS", "", "")])
    waiting = _held_ids(db)[0]
    # the cart on screen (not yet held) is posted with then_resume=<waiting>
    resp = _hold(client, "", [(rice, "Rice", "1006", "2", "60", "KGS", "", "")], then_resume=str(waiting))
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/pos/?resume={waiting}")
    held = POSBill.query.filter_by(status=POSBillStatus.HELD).all()
    assert sorted(len(b.lines) for b in held) == [1, 1] and len(held) == 2  # nothing lost
