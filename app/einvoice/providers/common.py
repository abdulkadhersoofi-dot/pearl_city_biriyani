"""Bits every provider shares: the NIC response shapes (a GSP passing the
NIC API through sends the same ones, unencrypted) and IST date parsing."""

import json
from datetime import datetime, timedelta, timezone

from app.einvoice.providers.base import EwbResult, IrnResult, ProviderError

IST = timezone(timedelta(hours=5, minutes=30), "IST")  # no DST, so a fixed offset (works without tzdata)
_FORMATS = ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y")
# NIC: 1005 is "Invalid Token" - log in again and retry once.
AUTH_ERROR_CODES = {"1005"}


def parse_dt(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def as_dict(data):
    if isinstance(data, (bytes, str)):
        try:
            data = json.loads(data)
        except ValueError:
            return {}
    return data if isinstance(data, dict) else {}


def http_json(response) -> dict:
    """The body of a response, or a ProviderError saying what came back."""
    try:
        body = response.json()
    except ValueError:
        raise ProviderError(f"Unexpected reply (HTTP {response.status_code}) - not JSON.",
                            code=f"HTTP{response.status_code}")
    if not isinstance(body, dict):
        raise ProviderError(f"Unexpected reply (HTTP {response.status_code}).", code=f"HTTP{response.status_code}")
    return body


def irp_errors(body: dict) -> ProviderError:
    """An IRP failure: {"Status": 0, "ErrorDetails": [...], "InfoDtls": [...]}."""
    details = body.get("ErrorDetails") or []
    if isinstance(details, str):
        details = as_dict(details) or [{"ErrorMessage": details}]
    if isinstance(details, dict):
        details = [details]
    codes = [str(d.get("ErrorCode") or "").strip() for d in details if isinstance(d, dict)]
    messages = [str(d.get("ErrorMessage") or "").strip() for d in details if isinstance(d, dict)]
    message = "; ".join(m for m in messages if m) or str(body.get("message") or body.get("error") or "Refused.")
    code = ",".join(c for c in codes if c) or None
    return ProviderError(message, code=code, info=body.get("InfoDtls") or [],
                         auth_expired=bool(set(codes) & AUTH_ERROR_CODES))


def irn_result(data: dict) -> IrnResult:
    if not data.get("Irn"):
        raise ProviderError("The IRP answered without an IRN.")
    return IrnResult(
        irn=data["Irn"], ack_no=str(data.get("AckNo") or ""), ack_date=parse_dt(data.get("AckDt")),
        signed_qr=data.get("SignedQRCode"), signed_invoice=data.get("SignedInvoice"),
        ewb_no=str(data["EwbNo"]) if data.get("EwbNo") else None, ewb_date=parse_dt(data.get("EwbDt")),
        ewb_valid_upto=parse_dt(data.get("EwbValidTill")), raw=data,
    )


def irp_ewb_result(data: dict) -> EwbResult:
    if not data.get("EwbNo"):
        raise ProviderError("The IRP answered without an e-way bill number.")
    return EwbResult(ewb_no=str(data["EwbNo"]), ewb_date=parse_dt(data.get("EwbDt")),
                     valid_upto=parse_dt(data.get("EwbValidTill")), raw=data)


def ewb_result(data: dict) -> EwbResult:
    """The e-way bill system's own reply to GENEWAYBILL."""
    number = data.get("ewayBillNo") or data.get("ewbNo")
    if not number:
        raise ProviderError("The e-way bill system answered without an e-way bill number.")
    return EwbResult(ewb_no=str(number), ewb_date=parse_dt(data.get("ewayBillDate") or data.get("ewbDate")),
                     valid_upto=parse_dt(data.get("validUpto")), raw=data)


def ewb_errors(body: dict, decoded_error: dict | None = None) -> ProviderError:
    """An e-way bill system failure: {"status": "0", "error": ...}. The
    error is a list of numeric codes ("errorCodes": "238,312,")."""
    err = decoded_error if decoded_error is not None else as_dict(body.get("error"))
    codes = str(err.get("errorCodes") or body.get("errorCodes") or "").strip(", ")
    message = str(err.get("message") or body.get("message") or "")
    if not message:
        message = (f"Refused with error code(s) {codes}." if codes else "Refused.")
    return ProviderError(message, code=codes or None)
