"""The dashboard's branch skyline and 3D view: the towers are today's sales
branch by branch by the one definition of sales (they add up to the Sales
today tile), every figure stays text and a link, and the 3D script and
three.js are served from ARFA's own files."""

import os
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from app.finance import sales_figures
from app.finance.dashboard import dashboard
from app.models.pos_bill import PaymentMode
from app.models.user import User, UserRole
from app.pos.services import CartInput, CartLineInput, checkout

D = Decimal
TODAY = date.today()
WEEK_AGO = TODAY - timedelta(days=7)


def _branch(db, tenant, name, email):
    user = User(tenant_id=tenant.id, name=name, email=email, role=UserRole.STAFF, must_change_password=False)
    user.set_password("BranchSecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _sale(db, tenant, by, amount, on=TODAY):
    bill = checkout(tenant, by, CartInput(lines=[CartLineInput("Meals", "9963", D("1"), D(amount), D("0"))]),
                    PaymentMode.CASH)
    db.session.commit()
    if on != TODAY:
        bill.completed_at = datetime.combine(on, time(13, 0)).astimezone()
        db.session.commit()
    return bill


def _business(db, tenant, client_admin):
    for key in ("business_reports", "pos"):
        tenant.set_feature(key, True)
    db.session.commit()
    anna = _branch(db, tenant, "Anna Nagar", "anna@acme.example.com")
    velachery = _branch(db, tenant, "Velachery", "vel@acme.example.com")
    quiet = _branch(db, tenant, "T Nagar", "tn@acme.example.com")
    _sale(db, tenant, anna, "1200")
    _sale(db, tenant, anna, "300")
    _sale(db, tenant, velachery, "400")
    _sale(db, tenant, client_admin, "250")                      # head office: rung up by the owner
    _sale(db, tenant, anna, "1000", WEEK_AGO)
    _sale(db, tenant, velachery, "900", WEEK_AGO)
    return anna, velachery, quiet


def test_the_towers_are_each_branchs_sales_and_add_up_to_the_tile(db, tenant, client_admin):
    anna, velachery, quiet = _business(db, tenant, client_admin)
    d = dashboard(tenant)
    rows = {r["name"]: r for r in d["branches"]}
    assert [r["name"] for r in d["branches"]] == ["Anna Nagar", "T Nagar", "Velachery", "Head office"]
    assert (rows["Anna Nagar"]["today"], rows["Anna Nagar"]["before"]) == (D("1500.00"), D("1000.00"))
    assert rows["Anna Nagar"]["change"] == {"pct": D("50"), "up": True, "same": False}
    assert rows["Velachery"]["change"]["up"] is False and rows["Velachery"]["today"] == D("400.00")
    assert rows["T Nagar"]["today"] == 0 and rows["T Nagar"]["change"] is None
    assert rows["Head office"]["today"] == D("250.00") and rows["Head office"]["id"] is None
    # The parts are the whole, and each part is what the Sales report shows for that branch.
    assert sum(r["today"] for r in d["branches"]) == d["sales_today"] == D("2150.00")
    for branch in (anna, velachery, quiet):
        assert rows[branch.name]["today"] == sales_figures.total(tenant, TODAY, TODAY, [branch.id])


def test_a_business_without_branches_has_no_skyline(client, db, tenant, client_admin):
    for key in ("business_reports", "pos"):
        tenant.set_feature(key, True)
    db.session.commit()
    _sale(db, tenant, client_admin, "500")
    assert dashboard(tenant)["branches"] == []
    client.post("/auth/login", data={"email": "admin@acme.example.com", "password": "ClientSecret123"})
    page = client.get("/reports/dashboard").get_data(as_text=True)
    assert "Branches today" not in page and "data-ribbon3d" in page and "dash3d.js" in page


def test_the_page_keeps_every_figure_as_text_and_a_link(client, db, tenant, client_admin):
    anna, _velachery, _quiet = _business(db, tenant, client_admin)
    client.post("/auth/login", data={"email": "admin@acme.example.com", "password": "ClientSecret123"})
    page = client.get("/reports/dashboard").get_data(as_text=True)
    today = TODAY.isoformat()
    assert "Branches today" in page and "data-sky3d hidden" in page and "data-sky-flat" in page
    item = page.split('data-value="1500.00" data-tone="up"', 1)[1].split("</li>", 1)[0]
    assert f'href="/reports/sales?start={today}&amp;end={today}&amp;branch_id={anna.id}"' in item
    assert "Anna Nagar" in item and "Rs. 1,500.00" in item and "50%" in item
    assert 'data-tone="down"' in page and 'data-tone="none"' in page
    # The tile opens the report that proves it; the report says the same figure.
    assert f'<a href="/reports/sales?start={today}&amp;end={today}" class="stat-value">' in page
    report = client.get(f"/reports/sales?start={today}&end={today}&branch_id={anna.id}").get_data(as_text=True)
    assert "1500.00" in report and "Anna Nagar" in report
    # The flat chart and the table for screen readers stay in the page.
    assert "data-ribbon-flat" in page and "<caption>Sales by day</caption>" in page
    assert 'data-dash-view hidden' in page


def test_the_3d_script_and_three_js_are_served_by_arfa(app, client, db, tenant, client_admin):
    tenant.set_feature("business_reports", True)
    db.session.commit()
    client.post("/auth/login", data={"email": "admin@acme.example.com", "password": "ClientSecret123"})
    resp = client.get("/reports/dashboard")
    page = resp.get_data(as_text=True)
    assert 'type="module" src="/static/js/dash3d.js' in page
    assert 'data-three="/static/vendor/three/three.module.min.js' in page
    assert "http://" not in page.split('id="dash3d-script"')[0].rsplit("<script", 1)[1]
    for path in ("/static/js/dash3d.js", "/static/vendor/three/three.module.min.js",
                 "/static/vendor/three/three.core.min.js"):
        got = client.get(path)
        assert got.status_code == 200 and "javascript" in got.headers["Content-Type"]
    # three.js imports its other half from beside itself, never from another server.
    vendor = os.path.join(app.static_folder, "vendor", "three")
    with open(os.path.join(vendor, "three.module.min.js"), encoding="utf-8") as fh:
        module = fh.read()
    assert 'from"./three.core.min.js"' in module and "https://" not in module.split("*/", 1)[1][:2000]
    with open(os.path.join(app.static_folder, "js", "dash3d.js"), encoding="utf-8") as fh:
        script = fh.read()
    assert "prefers-reduced-motion" in script and "saveData" in script and "http" not in script.replace("https://", "")
