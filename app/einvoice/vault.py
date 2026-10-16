"""Encrypted storage for e-invoice / e-way bill logins (Fernet: AES-128-CBC
+ HMAC-SHA256 from the `cryptography` package).

The key is EINVOICE_SECRET_KEY from the environment when set (any string;
a Fernet key is derived from it), otherwise derived from SECRET_KEY. Either
way, changing it makes every stored login unreadable - the client then
just enters it again under e-Invoice settings; nothing else is lost."""

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


class VaultError(Exception):
    pass


def _fernet() -> Fernet:
    secret = current_app.config.get("EINVOICE_SECRET_KEY") or current_app.config["SECRET_KEY"]
    key = base64.urlsafe_b64encode(hashlib.sha256(f"einvoice-vault:{secret}".encode()).digest())
    return Fernet(key)


def seal(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data, separators=(",", ":")).encode()).decode()


def unseal(token: str | None) -> dict:
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except (InvalidToken, ValueError) as exc:
        raise VaultError("The saved e-invoice login can't be read (the server's secret key changed) - "
                         "enter it again under e-Invoice settings.") from exc
