"""A GST Suvidha Provider / ASP that passes the NIC API through as plain
JSON (the GSP does the NIC encryption and holds the whitelisted IP) -
the usual arrangement for a business that doesn't register for direct
access. Everything that differs between GSPs is a setting:

  base_url        the GSP's API address
  auth_path       where to log in (default the NIC path, /eivital/v1.04/auth)
  extra_headers   a JSON object of headers the GSP wants on every call,
                  e.g. {"aspid": "...", "password": "..."} or a
                  subscription key - stored encrypted like the password
  *_path          the IRN / cancel / get / e-way bill paths, defaulting
                  to the NIC ones the GSP mirrors

Login: POST auth_path with UserName / Password (body and headers); the
token is read from Data.AuthToken, or access_token / authtoken. Replies
are NIC-shaped (Status / Data / ErrorDetails for the IRP, status / data /
error for e-way bills). A GSP that does something else entirely is a
subclass overriding authenticate() and _call() - nothing else changes."""

import json
from datetime import datetime, timedelta, timezone

from app.einvoice.providers.base import CredentialField, EwbResult, IrnResult, Provider, ProviderError
from app.einvoice.providers.common import (
    as_dict, ewb_errors, ewb_result, http_json, irn_result, irp_errors, irp_ewb_result, parse_dt,
)

DEFAULT_PATHS = {
    "auth_path": "/eivital/v1.04/auth",
    "irn_path": "/eicore/v1.03/Invoice",
    "cancel_path": "/eicore/v1.03/Invoice/Cancel",
    "get_irn_path": "/eicore/v1.03/Invoice/irn/{irn}",
    "ewb_by_irn_path": "/eiewb/v1.03/ewaybill",
    "ewb_path": "/ewaybillapi/v1.03/ewayapi",
}


class GspProvider(Provider):
    key = "gsp"
    label = "GSP / ASP (your e-invoice service provider)"
    fields = [
        CredentialField("base_url", "GSP API address", help="From your GSP, e.g. https://api.yourgsp.com/einvoice"),
        CredentialField("username", "API user name", help="The API user created on the IRP portal for this GSP."),
        CredentialField("password", "API password", secret=True),
        CredentialField("client_id", "GSP client ID", secret=True, required=False),
        CredentialField("client_secret", "GSP client secret", secret=True, required=False),
        CredentialField("extra_headers", "Extra headers (JSON)", secret=True, required=False, multiline=True,
                        help='Anything else your GSP asks for on every call, e.g. {"aspid": "123"}.'),
    ] + [CredentialField(k, k.replace("_", " ").capitalize(), required=False, help=f"Leave blank for {v}")
         for k, v in DEFAULT_PATHS.items()]

    def _path(self, name: str) -> str:
        return self.config.get(name) or DEFAULT_PATHS[name]

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Gstin": self.gstin, "gstin": self.gstin,
             "user_name": self.config["username"]}
        if self.config.get("client_id"):
            h["client_id"] = self.config["client_id"]
        if self.config.get("client_secret"):
            h["client_secret"] = self.config["client_secret"]
        extra = self.config.get("extra_headers")
        if extra:
            try:
                h.update({str(k): str(v) for k, v in json.loads(extra).items()})
            except (ValueError, AttributeError) as exc:
                raise ProviderError("The extra headers in e-Invoice settings aren't valid JSON.", code="CONFIG") from exc
        if self.session.get("auth_token"):
            h["AuthToken"] = self.session["auth_token"]
            h["authtoken"] = self.session["auth_token"]
        return h

    def _url(self, path: str) -> str:
        return self.config["base_url"].rstrip("/") + path

    def authenticate(self) -> dict:
        headers = self._headers()
        headers["password"] = self.config["password"]
        body = {"UserName": self.config["username"], "Password": self.config["password"],
                "ForceRefreshAccessToken": False}
        reply = http_json(self.http.post(self._url(self._path("auth_path")), json=body, headers=headers,
                                         timeout=self.timeout))
        data = as_dict(reply.get("Data"))
        token = data.get("AuthToken") or reply.get("access_token") or reply.get("authtoken") or reply.get("AuthToken")
        if not token:
            raise irp_errors(reply)
        expiry = parse_dt(data.get("TokenExpiry")) or datetime.now(timezone.utc) + timedelta(hours=6)
        self.session.update({"auth_token": token, "expiry": expiry.isoformat()})
        return self.session

    def _call(self, method: str, path: str, payload: dict | None = None, ewb: bool = False) -> dict:
        url = self._url(path)
        if method == "GET":
            response = self.http.get(url, headers=self._headers(), timeout=self.timeout)
        else:
            response = self.http.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        if response.status_code in (401, 403):
            raise ProviderError(f"Not authorised (HTTP {response.status_code}).", code=f"HTTP{response.status_code}",
                                auth_expired=True)
        reply = http_json(response)
        if ewb:
            if str(reply.get("status") or reply.get("Status")) != "1":
                raise ewb_errors(reply)
            return as_dict(reply.get("data") or reply.get("Data"))
        if str(reply.get("Status")) != "1":
            raise irp_errors(reply)
        return as_dict(reply.get("Data"))

    def generate_irn(self, payload: dict) -> IrnResult:
        return irn_result(self._call("POST", self._path("irn_path"), payload))

    def get_irn(self, irn: str) -> IrnResult:
        return irn_result(self._call("GET", self._path("get_irn_path").format(irn=irn)))

    def cancel_irn(self, irn: str, reason_code: str, remark: str):
        data = self._call("POST", self._path("cancel_path"), {"Irn": irn, "CnlRsn": reason_code, "CnlRem": remark})
        return parse_dt(data.get("CancelDate"))

    def ewb_by_irn(self, data: dict) -> EwbResult:
        return irp_ewb_result(self._call("POST", self._path("ewb_by_irn_path"), data))

    def generate_ewb(self, data: dict) -> EwbResult:
        return ewb_result(self._call("POST", self._path("ewb_path"), {"action": "GENEWAYBILL", **data}, ewb=True))

    def update_part_b(self, data: dict):
        reply = self._call("POST", self._path("ewb_path"), {"action": "VEHEWB", **data}, ewb=True)
        return parse_dt(reply.get("validUpto"))

    def cancel_ewb(self, ewb_no: str, reason_code: str, remark: str):
        reply = self._call("POST", self._path("ewb_path"), {"action": "CANEWB", "ewbNo": int(ewb_no),
                                                            "cancelRsnCode": int(reason_code),
                                                            "cancelRmrk": remark}, ewb=True)
        return parse_dt(reply.get("cancelDate"))
