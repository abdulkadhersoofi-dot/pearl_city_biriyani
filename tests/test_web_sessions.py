"""Sign-in sessions kept in the database (SESSION_TYPE=database, for Google
Cloud Run): the table holds only a hash of the id, signing in issues a new
id, a request that changes nothing writes nothing, expired sessions are
refused and cleaned up, and saving a session never commits work a view
left unfinished."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app import create_app
from app.config import TestingConfig
from app.extensions import db as _db


def _rows(db):
    return db.session.execute(text("SELECT id_hash, data, expiry FROM web_sessions")).all()


def _cookie(client, name="gstapp_session"):
    cookie = client.get_cookie(name)
    return cookie.value if cookie else None


def _login(client, email="admin@acme.example.com", password="ClientSecret123"):
    return client.post("/auth/login", data={"email": email, "password": password})


def test_the_table_keeps_a_hash_never_the_id(client, db, client_admin):
    client.get("/auth/login")
    before = _cookie(client)
    assert _login(client).status_code == 302
    sid = _cookie(client)
    assert sid and sid != before                       # a new id on signing in
    rows = _rows(db)
    assert len(rows) >= 1 and all(r.id_hash != sid and sid not in bytes(r.data).decode() for r in rows)
    assert client.get("/invoices/").status_code == 200  # still signed in with it


def test_a_request_that_changes_nothing_writes_nothing(client, db, client_admin):
    _login(client)
    client.get("/invoices/")
    first = {r.id_hash: (r.data, r.expiry) for r in _rows(db)}
    client.get("/invoices/")
    client.get("/invoices/")
    assert {r.id_hash: (r.data, r.expiry) for r in _rows(db)} == first


def test_an_expired_session_is_refused_and_cleaned_up(app, client, db, client_admin):
    _login(client)
    db.session.execute(text("UPDATE web_sessions SET expiry = :e"),
                       {"e": datetime.now(timezone.utc) - timedelta(minutes=1)})
    db.session.commit()
    from flask import g

    g.pop("_login_user", None)     # the test keeps one app context open; a real request starts afresh
    assert client.get("/invoices/").status_code == 302          # back to sign-in
    result = app.test_cli_runner().invoke(args=["sessions-cleanup"])
    assert "expired session(s) deleted" in result.output
    assert db.session.execute(text("SELECT count(*) FROM web_sessions WHERE expiry <= now()")).scalar() == 0


def test_signing_out_ends_the_session(client, db, client_admin):
    _login(client)
    client.get("/auth/logout")
    assert client.get("/invoices/").status_code == 302


def test_saving_a_session_never_commits_a_views_unfinished_work(app, client, db, tenant):
    from app.models.customer import Customer

    def half_done():
        from flask import session

        _db.session.add(Customer(tenant_id=tenant.id, name="Never saved"))
        _db.session.flush()
        session["touched"] = True                    # the session is saved after this
        return "ok"

    app.add_url_rule("/_test/half-done", "half_done", half_done)
    assert client.get("/_test/half-done").data == b"ok"
    _db.session.remove()
    assert Customer.query.filter_by(name="Never saved").count() == 0


def test_the_firebase_cookie_name_works():
    class Firebase(TestingConfig):
        SESSION_COOKIE_NAME = "__session"

    app = create_app(Firebase)
    with app.app_context():
        _db.create_all()
        try:
            from app.models.user import User, UserRole

            user = User(name="Firm", email="firm2@example.com", role=UserRole.SUPER_ADMIN, must_change_password=False)
            user.set_password("SuperSecret123")
            _db.session.add(user)
            _db.session.commit()
            client = app.test_client()
            client.post("/auth/login", data={"email": "firm2@example.com", "password": "SuperSecret123"})
            assert _cookie(client, "__session") and _cookie(client, "gstapp_session") is None
            assert client.get("/").headers.get("Location") == "/admin/today"   # signed in, not sent to sign-in
        finally:
            _db.session.remove()
            _db.drop_all()


def test_a_signed_in_session_lasts_its_lifetime_and_a_passer_by_costs_nothing(client, db, client_admin):
    client.get("/healthz")
    assert _rows(db) == [] and _cookie(client) is None        # nothing kept for a visitor who did nothing
    _login(client)
    cookie = client.get_cookie("gstapp_session")
    assert cookie.expires is not None                          # a dated cookie (12 hours), as with Redis
