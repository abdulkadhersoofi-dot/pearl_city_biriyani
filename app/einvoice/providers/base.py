"""The provider interface and what it returns - see app.einvoice.providers."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class CredentialField:
    name: str
    label: str
    secret: bool = False
    required: bool = True
    help: str = ""
    multiline: bool = False


class ProviderError(Exception):
    """The provider refused or failed. `code` and `message` are exactly
    what it sent back (or what went wrong on the wire); `info` carries
    any extra details it returned (e.g. the existing IRN on a duplicate)."""

    def __init__(self, message: str, code: str | None = None, info=None, auth_expired: bool = False):
        super().__init__(message)
        self.message = message
        self.code = code
        self.info = info or []
        self.auth_expired = auth_expired


@dataclass
class IrnResult:
    irn: str
    ack_no: str
    ack_date: datetime | None
    signed_qr: str | None = None
    signed_invoice: str | None = None
    ewb_no: str | None = None
    ewb_date: datetime | None = None
    ewb_valid_upto: datetime | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class EwbResult:
    ewb_no: str
    ewb_date: datetime | None
    valid_upto: datetime | None
    raw: dict = field(default_factory=dict)


class Provider:
    key = ""
    label = ""
    fields: list[CredentialField] = []

    def __init__(self, gstin: str, config: dict, session: dict | None = None, http=None):
        import requests

        self.gstin = gstin
        self.config = config
        self.session = dict(session or {})
        self.http = http or requests.Session()
        self.timeout = 30

    # Each returns the new session dict to store (token, keys, expiry).
    def authenticate(self) -> dict:
        raise NotImplementedError

    def generate_irn(self, payload: dict) -> IrnResult:
        raise NotImplementedError

    def get_irn(self, irn: str) -> IrnResult:
        raise NotImplementedError

    def cancel_irn(self, irn: str, reason_code: str, remark: str) -> datetime | None:
        raise NotImplementedError

    def ewb_by_irn(self, data: dict) -> EwbResult:
        raise NotImplementedError

    def generate_ewb(self, data: dict) -> EwbResult:
        raise NotImplementedError

    def update_part_b(self, data: dict) -> datetime | None:
        """Returns the new validity."""
        raise NotImplementedError

    def cancel_ewb(self, ewb_no: str, reason_code: str, remark: str) -> datetime | None:
        raise NotImplementedError
