"""The app's own confirm / question boxes (static/js/dialog.js) instead of
the browser's confirm(), prompt() and alert(). Inside the Android app the
browser's boxes never showed - its WebView answered "Cancel" - so Discard,
Void and the rest did nothing there. No screen may use them again; the
Android app also shows any that slip through (WebChromeClient)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NATIVE = re.compile(r"(?<![\w.$])(?:window\.)?(confirm|prompt|alert)\s*\(")


def _sources():
    for path in (ROOT / "app/templates").rglob("*.html"):
        yield path
    for path in (ROOT / "app/static/js").glob("*.js"):
        yield path


def test_no_screen_uses_the_browsers_own_boxes():
    found = []
    for path in _sources():
        if path.name == "dialog.js":
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            for m in NATIVE.finditer(line):
                # The counter app's Bill sheet has its own confirm() method (Alpine).
                if '@click="confirm()"' in line:
                    continue
                found.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:120]}")
    assert found == []


def test_every_layout_loads_the_dialog_script():
    for layout in ("base.html", "counterapp/_layout.html"):
        assert "js/dialog.js" in (ROOT / "app/templates" / layout).read_text(), layout
    script = (ROOT / "app/static/js/dialog.js").read_text()
    assert "window.ArfaDialog" in script and "data-confirm" in script and "data-prompt" in script


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def test_discarding_a_held_bill_asks_in_the_page(client, db, tenant, client_admin):
    from decimal import Decimal

    from app.models.pos_bill import POSBill, POSBillStatus
    from app.models.product import Product

    tea = Product(tenant_id=tenant.id, name="Tea", hsn_or_sac_code="9963", gst_rate=Decimal("5"), unit="NOS",
                  sale_price=Decimal("20"))
    db.session.add(tea)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    client.post("/pos/hold", data={"line_product_id[]": str(tea.id), "line_description[]": "Tea", "line_hsn[]": "9963",
                                   "line_qty[]": "1", "line_rate[]": "20", "line_gst_rate[]": "5", "line_unit[]": "NOS"})
    held = POSBill.query.filter_by(status=POSBillStatus.HELD).one()
    html = client.get("/pos/").get_data(as_text=True)
    form = re.search(r'<form[^>]*action="/pos/held/%d/discard"[^>]*>' % held.id, html, re.S).group(0)
    assert 'data-confirm="Discard this held bill?"' in form and 'data-confirm-ok="Discard"' in form
    assert "onsubmit" not in form
    # The form itself is unchanged: confirmed in the page, it posts as before.
    assert client.post(f"/pos/held/{held.id}/discard").status_code == 302
    assert POSBill.query.count() == 0


def test_a_reason_is_asked_in_the_page_then_sent(client, db, tenant, client_admin):
    page = (ROOT / "app/templates/pos/day_book.html").read_text()
    assert 'data-prompt="Why is {{ b.bill_number }} being voided?" data-prompt-field="reason"' in page
    assert 'data-prompt="Reason for refunding {{ b.bill_number }}:"' in page
    assert '<input type="hidden" name="reason">' in page


def test_the_android_app_shows_any_box_a_page_still_asks_for():
    activity = (ROOT / "android/app/src/main/java/com/arfa/pos/MainActivity.kt").read_text()
    assert "webView.webChromeClient = AppChromeClient()" in activity
    for hook in ("onJsAlert", "onJsConfirm", "onJsPrompt"):
        assert f"override fun {hook}(" in activity
