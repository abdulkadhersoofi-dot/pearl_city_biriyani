"""Page patterns (redesign stage 3): inline styles replaced by shared
classes (and kept out), credit / debit notes as a tab inside Invoices, and
the menu's links all opening real pages."""

import pathlib
import re

from app.models.tenant import Gstin, RegistrationType, Tenant

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "app" / "templates"
CSS = (TEMPLATES.parent / "static" / "css" / "app.css").read_text()
# Printed documents carry their own styles (WeasyPrint never loads app.css),
# and two partials are shared with the GSTR-1 PDF - those keep inline styles.
PRINTED = ("pdf", "_print", "print.html", "receipt", "kot_print")
PDF_PARTIALS = {"_advances.html", "_b2cl_hsn.html"}


def _screen_templates():
    for f in TEMPLATES.rglob("*.html"):
        if any(s in f.name for s in PRINTED) or f.name in PDF_PARTIALS:
            continue
        yield f


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def test_inline_styles_stay_retired():
    count = sum(len(re.findall(r'\sstyle="', f.read_text())) for f in _screen_templates())
    # 775 before stage 3; the few left are one-offs (an image box, a grid).
    assert count <= 46, count


def test_every_utility_class_used_in_a_template_exists():
    pattern = re.compile(r'class="([^"]*)"')
    utility = re.compile(r"^(m|mt|mb|gap|w|maxw|minw)-\d+$|^(figure(-\w+)?|grow|nowrap|items-\w+|justify-\w+|"
                         r"text-(center|right|danger)|fw-\d+|fs-\w+|d-\w+|break-all|w-full|flex-(wrap|col)|shrink-0)$")
    missing = set()
    for f in _screen_templates():
        for classes in pattern.findall(f.read_text()):
            for cls in classes.split():
                if "{" in cls:
                    continue
                if utility.match(cls) and f".{cls} " not in CSS and f".{cls}," not in CSS:
                    missing.add((f.name, cls))
    assert not missing, sorted(missing)


def test_notes_are_a_tab_inside_invoices(client, db, tenant, client_admin):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    for url in ("/invoices/", "/notes/"):
        html = client.get(url).get_data(as_text=True)
        assert 'aria-label="Invoices and notes"' in html, url
        active = re.findall(r'class="nav-link active"[^>]*>\s*<svg[^>]*>.*?</svg><span class="nav-label">([^<]+)<',
                            html, re.S)
        assert active == ["Invoices"], url


def test_new_menu_has_no_dead_note_action(client, db, tenant, client_admin):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    html = client.get("/invoices/").get_data(as_text=True)
    assert 'href="/notes/new"' not in html
    assert client.get("/notes/new").status_code == 404  # a note starts from an invoice


def test_gst_returns_link_opens_the_primary_gstin(client, db, tenant, client_admin, super_admin):
    _login(client, "admin@acme.example.com", "ClientSecret123")
    for url in ("/reports/gstr1", "/reports/gstr3b"):
        resp = client.get(url)
        assert resp.status_code == 200, url
        assert b"27AAAAA0000A1Z5" in resp.data, url
    other = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id)
    db.session.add(other)
    db.session.flush()
    theirs = Gstin(tenant_id=other.id, gstin="29BBBBB0000B1Z5", state_code="29", state_name="Karnataka",
                   is_primary=True)
    db.session.add(theirs)
    db.session.commit()
    # Another client's GSTIN by id is still refused.
    assert client.get(f"/reports/gstr1?gstin_id={theirs.id}").status_code == 404


def test_every_menu_and_search_link_opens(client, db, tenant, client_admin):
    import json

    for flag in ("inventory", "purchases", "expenses", "receivables", "payables", "cash_bank_book", "business_reports",
                 "sales_documents", "recurring_invoices", "job_cards", "jewellery", "pharmacy", "wholesale",
                 "loyalty", "batch_expiry", "einvoice", "eway_bill", "daily_summary", "categories",
                 "multi_price_list", "offline_pos", "cashier_pin", "day_close", "weighing", "restaurant_mode"):
        tenant.set_feature(flag, True)
    db.session.commit()
    _login(client, "admin@acme.example.com", "ClientSecret123")
    html = client.get("/invoices/").get_data(as_text=True)
    raw = re.search(r'<script type="application/json" id="palette-data">(.*?)</script>', html, re.S).group(1)
    for entry in json.loads(raw):
        if "logout" in entry["url"]:
            continue
        status = client.get(entry["url"]).status_code
        assert status in (200, 302), (entry["label"], entry["url"], status)
