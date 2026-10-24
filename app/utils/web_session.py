"""Sign-in sessions kept in the database (SESSION_TYPE = "database") - for
Google Cloud Run, where there is no Redis: every instance reads the same
table, so a user signed in on one instance is signed in on all of them.

The cookie carries only a random session id (32 bytes); the table keeps
its SHA-256, so a copy of the table can't be used to sign in as anyone.

Load on the database, per request: one primary-key read when the request
carries a session cookie. A write (one upsert) only when the session
changed, or once SESSION_REFRESH_MINUTES have passed since the last
write, to push the expiry forward - not on every request. Requests
without a cookie and sessions with nothing in them cost nothing.

The session shares the request's own connection (no second connection
per request). It is saved after the view has finished; anything the
view left uncommitted is rolled back first - which is what the end of the
request would do with it anyway - so saving a session never commits a
view's half-done work.

Expired rows are deleted by `flask sessions-cleanup` (daily)."""

import hashlib
import secrets
import time
from datetime import datetime, timezone

from flask.json.tag import TaggedJSONSerializer
from flask.sessions import SessionInterface, SessionMixin
from flask_login import user_logged_in
from sqlalchemy import Column, DateTime, LargeBinary, String, Table, text
from werkzeug.datastructures import CallbackDict

from app.extensions import db

web_sessions = Table(
    "web_sessions",
    db.metadata,
    Column("id_hash", String(64), primary_key=True),
    Column("data", LargeBinary, nullable=False),
    Column("expiry", DateTime(timezone=True), nullable=False, index=True),
)

_serializer = TaggedJSONSerializer()
_WRITTEN_AT = "_w"   # when the session was last written (epoch seconds)
_BOOKKEEPING = ("_permanent", "_fresh", _WRITTEN_AT)


def _hash(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


class DatabaseSession(CallbackDict, SessionMixin):
    def __init__(self, initial=None, sid=None, new=False):
        def on_update(self):
            self.modified = True

        super().__init__(initial, on_update)
        self.sid = sid
        self.new = new
        self.modified = False


class DatabaseSessionInterface(SessionInterface):
    def __init__(self, refresh_minutes: int = 10):
        self.refresh_seconds = refresh_minutes * 60

    def open_session(self, app, request):
        # Permanent (a dated cookie, PERMANENT_SESSION_LIFETIME) unless the
        # app says otherwise - set without marking the session changed.
        fresh = {"_permanent": bool(app.config.get("SESSION_PERMANENT", True))}
        sid = request.cookies.get(self.get_cookie_name(app))
        if not sid or len(sid) > 128:
            return DatabaseSession(fresh, sid=secrets.token_urlsafe(32), new=True)
        row = db.session.execute(
            text("SELECT data FROM web_sessions WHERE id_hash = :h AND expiry > now()"), {"h": _hash(sid)}
        ).first()
        if row is None:
            return DatabaseSession(fresh, sid=secrets.token_urlsafe(32), new=True)
        try:
            data = _serializer.loads(bytes(row.data).decode())
        except (ValueError, TypeError):
            data = {}
        return DatabaseSession({**fresh, **data}, sid=sid)

    def save_session(self, app, session, response):
        name = self.get_cookie_name(app)
        domain, path = self.get_cookie_domain(app), self.get_cookie_path(app)
        # Only bookkeeping keys (Flask-Login marks even a visitor "_fresh"):
        # nothing worth a row - a visitor who never signed in or opened a
        # form costs nothing.
        if not any(k not in _BOOKKEEPING for k in session):
            if session.modified and not session.new:
                db.session.rollback()
                db.session.execute(text("DELETE FROM web_sessions WHERE id_hash = :h"), {"h": _hash(session.sid)})
                db.session.commit()
                response.delete_cookie(name, domain=domain, path=path)
            return
        now = time.time()
        due = now - float(session.get(_WRITTEN_AT) or 0) >= self.refresh_seconds
        if not (session.modified or session.new or due):
            return
        lifetime = app.permanent_session_lifetime
        expires = datetime.now(timezone.utc) + lifetime
        payload = dict(session)
        payload[_WRITTEN_AT] = now
        db.session.rollback()
        db.session.execute(
            text("INSERT INTO web_sessions (id_hash, data, expiry) VALUES (:h, :d, :e) "
                 "ON CONFLICT (id_hash) DO UPDATE SET data = EXCLUDED.data, expiry = EXCLUDED.expiry"),
            {"h": _hash(session.sid), "d": _serializer.dumps(payload).encode(), "e": expires},
        )
        db.session.commit()
        response.set_cookie(
            name, session.sid,
            expires=self.get_expiration_time(app, session) if session.permanent else None,
            httponly=self.get_cookie_httponly(app), domain=domain, path=path,
            secure=self.get_cookie_secure(app), samesite=self.get_cookie_samesite(app),
        )
        response.vary.add("Cookie")

    def regenerate(self, session) -> None:
        """A new id for the same session (after signing in), so an id
        planted before sign-in is worthless after it."""
        if not session.new:
            db.session.execute(text("DELETE FROM web_sessions WHERE id_hash = :h"), {"h": _hash(session.sid)})
        session.sid = secrets.token_urlsafe(32)
        session.new = True
        session.modified = True


def _new_id_on_sign_in(sender, user, **extra):
    """An id handed out before sign-in (or planted by someone else) is
    worthless after it. Connected once, for every app, so no app is held
    in memory by the signal."""
    from flask import session

    interface = getattr(sender, "session_interface", None)
    if isinstance(interface, DatabaseSessionInterface):
        interface.regenerate(session)


user_logged_in.connect(_new_id_on_sign_in)


def delete_expired() -> int:
    result = db.session.execute(text("DELETE FROM web_sessions WHERE expiry <= now()"))
    db.session.commit()
    return result.rowcount or 0
