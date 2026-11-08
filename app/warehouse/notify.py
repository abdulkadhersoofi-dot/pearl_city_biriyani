"""Pushes to the ARFA Warehouse phones, like a WhatsApp message: a new order
rings every picker of that warehouse (sound, pop-up, even with the app
closed); a claim, release, cancel or move refreshes their pipelines
silently.

Through an outbox (WarehouseNotice): the notice is written in the same
transaction as the order change and sent once it has committed
(send_pending, called after the commit), so a push never goes out for an
order that was rolled back. A notice that couldn't be sent is tried again
on the next send; the app also polls every 30 seconds while open, so a
lost push never means a missed order.

Sent with Firebase Cloud Messaging (HTTP v1) as high-priority data
messages - the app builds the notification itself on a loud channel. The
server signs in as its own service account (the metadata server's token);
FCM_PROJECT_ID names the Firebase project. Without it (tests, a laptop)
notices are only marked, never sent.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import requests
from flask import current_app

from app.extensions import db
from app.models.mixins import utcnow
from app.models.warehouse import WarehouseLogin, WarehouseNotice, WarehouseSession, warehouse_login_locations

log = logging.getLogger(__name__)

FCM_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
MAX_ATTEMPTS = 5
BATCH = 50


def _queue(tenant_id: int, location_id: int, kind: str, doc_id: int | None, title: str = "", body: str = "",
           silent: bool = True) -> WarehouseNotice:
    notice = WarehouseNotice(tenant_id=tenant_id, location_id=location_id, kind=kind, sales_document_id=doc_id,
                             title=title[:120] or None, body=body[:255] or None, silent=silent)
    db.session.add(notice)
    return notice


def new_order(doc, location, branch: str) -> WarehouseNotice:
    items = sum(int(l.qty) for l in doc.lines)
    return _queue(doc.tenant_id, location.id, "new_order", doc.id,
                  title=f"New order {doc.number}",
                  body=f"{items} item{'s' if items != 1 else ''} · {branch} · {location.name}", silent=False)


def refresh(doc, location_id: int, kind: str) -> WarehouseNotice:
    return _queue(doc.tenant_id, location_id, kind, doc.id)


def receipt(tenant_id: int, location_id: int, title: str, body: str) -> WarehouseNotice:
    return _queue(tenant_id, location_id, "receipt", None, title=title, body=body, silent=False)


def targets(tenant_id: int, location_id: int) -> list[WarehouseSession]:
    """Every live phone of every active login working in that warehouse."""
    return (WarehouseSession.query.join(WarehouseLogin, WarehouseSession.login_id == WarehouseLogin.id)
            .join(warehouse_login_locations, warehouse_login_locations.c.login_id == WarehouseLogin.id)
            .filter(WarehouseSession.tenant_id == tenant_id, WarehouseSession.revoked_at.is_(None),
                    WarehouseSession.fcm_token.isnot(None), WarehouseLogin.is_active.is_(True),
                    warehouse_login_locations.c.location_id == location_id)
            .all())


def _message(notice: WarehouseNotice, token: str) -> dict:
    data = {"kind": notice.kind, "location_id": str(notice.location_id),
            "order_id": str(notice.sales_document_id or ""), "silent": "1" if notice.silent else "0",
            "title": notice.title or "", "body": notice.body or "", "notice_id": str(notice.id)}
    return {"message": {"token": token, "data": data,
                        "android": {"priority": "high", "ttl": "3600s" if not notice.silent else "300s"}}}


def deliver(project: str, payload: dict) -> tuple[bool, str]:
    """One FCM send. (sent, error) - error "UNREGISTERED" means the phone's
    token is dead."""
    from app.apps.store import StoreError, _token

    try:
        token = _token()
    except StoreError as exc:
        return False, str(exc)[:200]
    try:
        res = requests.post(FCM_URL.format(project=project), json=payload, timeout=5,
                            headers={"Authorization": f"Bearer {token}"})
    except requests.RequestException as exc:
        return False, str(exc)[:200]
    if res.status_code == 200:
        return True, ""
    text = res.text[:200]
    if res.status_code == 404 or "UNREGISTERED" in text:
        return False, "UNREGISTERED"
    return False, f"{res.status_code} {text}"


def send_pending(limit: int = BATCH) -> int:
    """Send what the outbox holds (call after a commit). Returns pushes sent."""
    project = current_app.config.get("FCM_PROJECT_ID") or ""
    since = utcnow() - timedelta(hours=6)
    notices = (WarehouseNotice.query.filter(WarehouseNotice.sent_at.is_(None),
                                            WarehouseNotice.attempts < MAX_ATTEMPTS,
                                            WarehouseNotice.created_at >= since)
               .order_by(WarehouseNotice.id).limit(limit).all())
    sent_total = 0
    for notice in notices:
        notice.attempts += 1
        if not project:
            notice.sent_at = utcnow()
            notice.recipients = 0
            notice.last_error = "push not set up (FCM_PROJECT_ID)"
            continue
        phones = targets(notice.tenant_id, notice.location_id)
        ok, failed = 0, []
        for phone in phones:
            sent, error = deliver(project, _message(notice, phone.fcm_token))
            if sent:
                ok += 1
            elif error == "UNREGISTERED":
                phone.fcm_token = None
            else:
                failed.append(error)
        notice.recipients = ok
        if failed and not ok:
            notice.last_error = failed[0][:255]
        else:
            notice.sent_at = utcnow()
            notice.last_error = failed[0][:255] if failed else None
        sent_total += ok
    if notices:
        db.session.commit()
    return sent_total


def send_pending_quietly() -> None:
    """send_pending, never failing the request (call after the commit)."""
    try:
        send_pending()
    except Exception:  # noqa: BLE001 - a push failing must never fail the order
        db.session.rollback()
        log.exception("warehouse: sending pushes failed")


def commit_and_push() -> None:
    """Commit the change, then send its pushes."""
    db.session.commit()
    send_pending_quietly()
