"""NIC direct API (e-Invoice API spec v1.03 / auth v1.04, and the e-way
bill API's action-style calls). For clients that registered for direct
API access on the IRP portal (API Registration -> "Through own"):

  login  POST /eivital/v1.04/auth - {"Data": RSA(NIC public key,
         {UserName, Password, AppKey, ForceRefreshAccessToken})}
         -> AuthToken and Sek (the session key, AES-256-ECB encrypted
         with our random AppKey)
  calls  {"Data": base64(AES-256-ECB(Sek, json))}, the reply's Data is
         encrypted the same way; headers client_id, client_secret,
         Gstin, user_name, AuthToken

Standalone e-way bills (no IRN - delivery challans, stock transfers, B2C
or when e-invoicing doesn't apply) go to the e-way bill system's own API,
which has its own login and base URL ("action": ACCESSTOKEN, GENEWAYBILL,
VEHEWB, CANEWB). Its base URL and paths come from the client's NIC
onboarding mail, so they are settings, not constants.

Built to the published spec and tested against a simulated NIC server
(tests/test_einvoice_providers.py, a real RSA / AES round trip) - not
against the live sandbox, as no sandbox login was available."""

import base64
import json
import os
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.einvoice.providers.base import CredentialField, EwbResult, IrnResult, Provider, ProviderError
from app.einvoice.providers.common import (
    as_dict, ewb_errors, ewb_result, http_json, irn_result, irp_errors, irp_ewb_result, parse_dt,
)

SANDBOX_BASE = "https://einv-apisandbox.nic.in"
AUTH_PATH = "/eivital/v1.04/auth"
IRN_PATH = "/eicore/v1.03/Invoice"
CANCEL_PATH = "/eicore/v1.03/Invoice/Cancel"
GET_IRN_PATH = "/eicore/v1.03/Invoice/irn/{irn}"
EWB_BY_IRN_PATH = "/eiewb/v1.03/ewaybill"


# --- crypto --------------------------------------------------------------------------

def rsa_encrypt(public_key_pem: str, data: bytes) -> str:
    text = public_key_pem.strip()
    if "BEGIN" not in text:  # NIC sends the key as a bare base64 block too
        text = "-----BEGIN PUBLIC KEY-----\n" + text + "\n-----END PUBLIC KEY-----"
    try:
        key = serialization.load_pem_public_key(text.encode())
    except ValueError as exc:
        raise ProviderError("The NIC public key saved in e-Invoice settings isn't a valid key.", code="KEY") from exc
    return base64.b64encode(key.encrypt(data, rsa_padding.PKCS1v15())).decode()


def aes_encrypt(key: bytes, data: bytes) -> str:
    padder = padding.PKCS7(128).padder()
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return base64.b64encode(enc.update(padder.update(data) + padder.finalize()) + enc.finalize()).decode()


def aes_decrypt(key: bytes, data_b64: str) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    unpadder = padding.PKCS7(128).unpadder()
    try:
        raw = dec.update(base64.b64decode(data_b64)) + dec.finalize()
        return unpadder.update(raw) + unpadder.finalize()
    except ValueError as exc:
        raise ProviderError("Couldn't decrypt the reply - the session key is stale.", auth_expired=True) from exc


class NicProvider(Provider):
    fields = [
        CredentialField("username", "API user name", help="The API user created on the IRP portal (not your "
                        "portal login)."),
        CredentialField("password", "API password", secret=True),
        CredentialField("client_id", "Client ID", secret=True),
        CredentialField("client_secret", "Client secret", secret=True),
        CredentialField("public_key", "NIC public key", secret=True, multiline=True,
                        help="The public key file NIC gives on API registration - paste its contents."),
        CredentialField("ewb_base_url", "e-Way bill API address", required=False,
                        help="Only for e-way bills without an IRN - from your NIC e-way bill API mail."),
        CredentialField("ewb_auth_path", "e-Way bill login path", required=False,
                        help="Leave blank for /ewaybillapi/v1.03/auth."),
        CredentialField("ewb_path", "e-Way bill API path", required=False,
                        help="Leave blank for /ewaybillapi/v1.03/ewayapi."),
    ]

    def base_url(self) -> str:
        raise NotImplementedError

    # --- IRP -----------------------------------------------------------------------

    def _headers(self, with_token=True) -> dict:
        h = {"Content-Type": "application/json", "client_id": self.config["client_id"],
             "client_secret": self.config["client_secret"], "Gstin": self.gstin}
        if with_token:
            h.update({"user_name": self.config["username"], "AuthToken": self.session.get("auth_token", "")})
        return h

    def authenticate(self) -> dict:
        app_key = os.urandom(32)
        body = {"UserName": self.config["username"], "Password": self.config["password"],
                "AppKey": base64.b64encode(app_key).decode(), "ForceRefreshAccessToken": False}
        data = rsa_encrypt(self.config["public_key"], json.dumps(body).encode())
        reply = http_json(self.http.post(self.base_url() + AUTH_PATH, json={"Data": data},
                                         headers=self._headers(with_token=False), timeout=self.timeout))
        if str(reply.get("Status")) != "1":
            raise irp_errors(reply)
        d = as_dict(reply.get("Data"))
        sek = aes_decrypt(app_key, d["Sek"])
        expiry = parse_dt(d.get("TokenExpiry")) or datetime.now(timezone.utc) + timedelta(hours=6)
        self.session.update({"auth_token": d["AuthToken"], "sek": base64.b64encode(sek).decode(),
                             "expiry": expiry.isoformat()})
        return self.session

    def _sek(self) -> bytes:
        if not self.session.get("sek"):
            raise ProviderError("Not logged in.", auth_expired=True)
        return base64.b64decode(self.session["sek"])

    def _irp(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = self.base_url() + path
        if method == "GET":
            response = self.http.get(url, headers=self._headers(), timeout=self.timeout)
        else:
            body = {"Data": aes_encrypt(self._sek(), json.dumps(payload).encode())}
            response = self.http.post(url, json=body, headers=self._headers(), timeout=self.timeout)
        reply = http_json(response)
        if str(reply.get("Status")) != "1":
            raise irp_errors(reply)
        data = reply.get("Data")
        if isinstance(data, str) and data and not data.lstrip().startswith("{"):
            data = aes_decrypt(self._sek(), data)
        return as_dict(data)

    def generate_irn(self, payload: dict) -> IrnResult:
        return irn_result(self._irp("POST", IRN_PATH, payload))

    def get_irn(self, irn: str) -> IrnResult:
        return irn_result(self._irp("GET", GET_IRN_PATH.format(irn=irn)))

    def cancel_irn(self, irn: str, reason_code: str, remark: str):
        data = self._irp("POST", CANCEL_PATH, {"Irn": irn, "CnlRsn": reason_code, "CnlRem": remark})
        return parse_dt(data.get("CancelDate"))

    def ewb_by_irn(self, data: dict) -> EwbResult:
        return irp_ewb_result(self._irp("POST", EWB_BY_IRN_PATH, data))

    # --- e-way bill system (no IRN) ------------------------------------------------

    def _ewb_base(self) -> str:
        base = (self.config.get("ewb_base_url") or "").rstrip("/")
        if not base:
            raise ProviderError("The e-way bill API address isn't set - add it under e-Invoice settings, or "
                                "download the e-way bill JSON and upload it on the portal.", code="CONFIG")
        return base

    def _ewb_headers(self) -> dict:
        return {"Content-Type": "application/json", "client-id": self.config["client_id"],
                "client-secret": self.config["client_secret"], "gstin": self.gstin,
                "authtoken": self.session.get("ewb_token", "")}

    def _ewb_login(self):
        app_key = os.urandom(32)
        body = {"action": "ACCESSTOKEN", "username": self.config.get("ewb_username") or self.config["username"],
                "password": rsa_encrypt(self.config["public_key"],
                                        (self.config.get("ewb_password") or self.config["password"]).encode()),
                "app_key": rsa_encrypt(self.config["public_key"], app_key)}
        path = self.config.get("ewb_auth_path") or "/ewaybillapi/v1.03/auth"
        reply = http_json(self.http.post(self._ewb_base() + path, json=body, headers=self._ewb_headers(),
                                         timeout=self.timeout))
        if str(reply.get("status")) != "1":
            raise ewb_errors(reply)
        self.session["ewb_token"] = reply["authtoken"]
        self.session["ewb_sek"] = base64.b64encode(aes_decrypt(app_key, reply["sek"])).decode()

    def _ewb(self, action: str, payload: dict) -> dict:
        if not self.session.get("ewb_sek"):
            self._ewb_login()
        sek = base64.b64decode(self.session["ewb_sek"])
        body = {"action": action, "data": aes_encrypt(sek, json.dumps(payload).encode())}
        path = self.config.get("ewb_path") or "/ewaybillapi/v1.03/ewayapi"
        reply = http_json(self.http.post(self._ewb_base() + path, json=body, headers=self._ewb_headers(),
                                         timeout=self.timeout))
        if str(reply.get("status")) != "1":
            error = reply.get("error")
            decoded = None
            if isinstance(error, str) and error and not error.lstrip().startswith("{"):
                try:
                    decoded = as_dict(base64.b64decode(error))
                except ValueError:
                    decoded = None
            raise ewb_errors(reply, decoded)
        data = reply.get("data")
        if isinstance(data, str) and data and not data.lstrip().startswith("{"):
            data = aes_decrypt(sek, data)
        return as_dict(data)

    def generate_ewb(self, data: dict) -> EwbResult:
        return ewb_result(self._ewb("GENEWAYBILL", data))

    def update_part_b(self, data: dict):
        return parse_dt(self._ewb("VEHEWB", data).get("validUpto"))

    def cancel_ewb(self, ewb_no: str, reason_code: str, remark: str):
        data = self._ewb("CANEWB", {"ewbNo": int(ewb_no), "cancelRsnCode": int(reason_code), "cancelRmrk": remark})
        return parse_dt(data.get("cancelDate"))


class NicSandboxProvider(NicProvider):
    key = "nic_sandbox"
    label = "NIC sandbox (testing - no real IRNs)"

    def base_url(self) -> str:
        return (self.config.get("base_url") or SANDBOX_BASE).rstrip("/")


class NicProductionProvider(NicProvider):
    key = "nic"
    label = "NIC direct (production)"
    fields = [CredentialField("base_url", "IRP API address", help="From your NIC API registration mail.")] + \
        NicProvider.fields

    def base_url(self) -> str:
        return self.config["base_url"].rstrip("/")
