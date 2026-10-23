"""The business runs on Indian time (UTC+5:30) wherever the server is.
Times are stored as UTC instants; they read back - on screens, receipts,
reports - as Indian time, and "today" and a day's boundaries are India's:
a bill made at 1:30 AM is on that day's day-book, with that time on it."""

import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import text

from app.models.pos_bill import POSBill
from app.models.product import Product

IST = timezone(timedelta(hours=5, minutes=30))


def test_the_server_clock_is_indian_time(app, db):
    assert time.strftime("%z") == "+0530"
    assert datetime.now().astimezone().utcoffset() == timedelta(hours=5, minutes=30)
    assert date.today() == datetime.now(IST).date()
    # Every database connection reads timestamps back in Indian time.
    assert db.session.execute(text("SHOW timezone")).scalar() == "Asia/Kolkata"
    stored = db.session.execute(text("SELECT TIMESTAMPTZ '2026-10-22 20:00:00+00'")).scalar()
    assert stored.utcoffset() == timedelta(hours=5, minutes=30) and (stored.day, stored.hour, stored.minute) == (23, 1, 30)


def _bill_at(client, db, tenant, when_utc):
    tea = Product.query.filter_by(tenant_id=tenant.id, name="Tea").first()
    if tea is None:
        tea = Product(tenant_id=tenant.id, name="Tea", hsn_or_sac_code="9963", gst_rate=Decimal("5"), unit="NOS",
                      sale_price=Decimal("20"))
        db.session.add(tea)
        db.session.commit()
    res = client.post("/pos/checkout", data={
        "line_product_id[]": str(tea.id), "line_description[]": "Tea", "line_hsn[]": "9963", "line_qty[]": "1",
        "line_rate[]": "20", "line_gst_rate[]": "5", "line_unit[]": "NOS", "payment_mode": "cash",
        "checkout_uuid": str(uuid.uuid4())}, headers={"X-Requested-With": "XMLHttpRequest"})
    bill = db.session.get(POSBill, res.get_json()["bill_id"])
    bill.completed_at = when_utc
    db.session.commit()
    return bill


def test_a_bill_after_midnight_reads_and_counts_in_indian_time(client, db, tenant, client_admin):
    client.post("/auth/login", data={"email": "admin@acme.example.com", "password": "ClientSecret123"})
    late = _bill_at(client, db, tenant, datetime(2026, 10, 22, 20, 0, tzinfo=timezone.utc))  # 23 Oct 01:30 IST
    day_book = client.get("/pos/bills?date=2026-10-23").get_data(as_text=True)
    assert late.bill_number in day_book and "01:30" in day_book
    assert late.bill_number not in client.get("/pos/bills?date=2026-10-22").get_data(as_text=True)
    receipt = client.get(f"/pos/bills/{late.id}/receipt").get_data(as_text=True)
    assert "23 Oct 2026 01:30" in receipt and "22 Oct 2026 20:00" not in receipt


def test_the_ist_filter_reads_any_stored_time_in_indian_time(app):
    render = app.jinja_env.from_string("{{ t|ist('%d %b %H:%M') }}").render
    assert render(t=datetime(2026, 10, 22, 20, 0, tzinfo=timezone.utc)) == "23 Oct 01:30"
    assert render(t=None) == ""
