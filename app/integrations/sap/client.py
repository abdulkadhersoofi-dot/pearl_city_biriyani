"""SAP S/4HANA's Purchase Order OData service, read only.

Two versions of the service exist and a system exposes one or both:

  v2  API_PURCHASEORDER_PROCESS_SRV  /sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder
      (OData V2; items under to_PurchaseOrderItem; SAP marks it deprecated)
  v4  API_PURCHASEORDER_2            /sap/opu/odata4/sap/api_purchaseorder_2/srvd_a2x/sap/purchaseorder/0001/PurchaseOrder
      (OData V4, its successor; items under _PurchaseOrderItem)

The field names are the same in both (PurchaseOrder, Supplier,
PurchaseOrderDate, LastChangeDateTime; items: PurchaseOrderItem, Material,
PurchaseOrderItemText, OrderQuantity, PurchaseOrderQuantityUnit,
NetPriceAmount, NetPriceQuantity, Plant, PurchasingDocumentDeletionCode);
check() reads the service's own $metadata and says which, if any, a
particular system lacks.

Only GET requests are ever made (and one POST for an OAuth token). Every
request goes to the one https host the business entered - a "next page" link
pointing anywhere else is not followed - and is logged without its login.
"""

from __future__ import annotations

import ipaddress
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlsplit

import requests

V2_PATH = "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV"
V4_PATH = "/sap/opu/odata4/sap/api_purchaseorder_2/srvd_a2x/sap/purchaseorder/0001"
PARTNER_PATH = "/sap/opu/odata/sap/API_BUSINESS_PARTNER"
PAGE = 200
MAX_PAGES = 50
HEADER_FIELDS = ("PurchaseOrder", "Supplier", "PurchaseOrderDate", "LastChangeDateTime")
ITEM_FIELDS = ("PurchaseOrderItem", "Material", "PurchaseOrderItemText", "OrderQuantity",
               "PurchaseOrderQuantityUnit", "NetPriceAmount", "NetPriceQuantity")


class SapError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class Settings:
    base_url: str
    api: str = "v2"
    auth: str = "basic"
    username: str = ""
    secret: str = ""
    token_url: str = ""
    sap_client: str = ""
    company_code: str = ""
    purchasing_org: str = ""
    plant: str = ""
    timeout: float = 30.0
    allowed_hosts: tuple = ()           # when set, the only hosts SAP may be on (SAP_ALLOWED_HOSTS)
    allow_private: bool = False         # a private address (a VPN to an on-premise system)


@dataclass
class Call:
    purpose: str
    path: str
    status: int | None = None
    ms: int = 0
    records: int | None = None
    error: str | None = None


# --- where SAP may be -------------------------------------------------------------------------------

def check_url(url: str, allowed_hosts=(), allow_private: bool = False, what: str = "SAP address") -> str:
    """The address, tidied - or SapError saying what is wrong with it."""
    url = (url or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise SapError(f"The {what} must start with https:// - SAP is only reached over an encrypted connection.")
    if parts.username or parts.password:
        raise SapError(f"The {what} must not carry a user name or password - they go in their own boxes.")
    host = parts.hostname.lower()
    if allowed_hosts and host not in {h.lower() for h in allowed_hosts}:
        raise SapError(f"{host} isn't on the list of SAP hosts ARFA may reach - ask ARFA to add it.")
    if not allow_private:
        if host in ("localhost",) or host.endswith((".local", ".internal", ".localhost")):
            raise SapError(f"The {what} must be a public address.")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved):
            raise SapError(f"The {what} must be a public address.")
    return url


# --- reading SAP's values -----------------------------------------------------------------------------

_V2_DATE = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")


def parse_when(value) -> datetime | None:
    """OData V2 "/Date(1700000000000)/", or an ISO date or timestamp (V4)."""
    if not value:
        return None
    text = str(value)
    m = _V2_DATE.match(text)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc)
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def parse_date(value) -> date | None:
    when = parse_when(value)
    return when.date() if when else None


def number(value, default="0") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else default))
    except InvalidOperation:
        return Decimal(default)


def code(value) -> str:
    """SAP pads numeric codes with zeros ("0000100234"): the same code without them."""
    text = str(value or "").strip()
    return text.lstrip("0") or text if text.isdigit() else text


def normalise(raw: dict, api: str) -> dict:
    """One purchase order as SAP sent it -> the few things ARFA keeps."""
    items_key = "_PurchaseOrderItem" if api == "v4" else "to_PurchaseOrderItem"
    items = raw.get(items_key) or []
    if isinstance(items, dict):                      # V2 wraps expanded lists in {"results": [...]}
        items = items.get("results") or []
    changed = parse_when(raw.get("LastChangeDateTime"))
    out_items = []
    for it in items:
        per = number(it.get("NetPriceQuantity"), "1") or Decimal("1")
        num = number(it.get("OrderPriceUnitToOrderUnitNmrtr"), "1") or Decimal("1")
        den = number(it.get("OrdPriceUnitToOrderUnitDnmntr"), "1") or Decimal("1")
        rate = number(it.get("NetPriceAmount")) / per * num / den
        out_items.append({
            "item": str(it.get("PurchaseOrderItem") or "").lstrip("0") or "0",
            "material": code(it.get("Material")),
            "text": str(it.get("PurchaseOrderItemText") or "").strip(),
            "qty": str(number(it.get("OrderQuantity"))),
            "unit": str(it.get("PurchaseOrderQuantityUnit") or "").strip().upper(),
            "rate": str(rate.quantize(Decimal("0.01"))),
            "plant": str(it.get("Plant") or "").strip(),
            "tax_code": str(it.get("TaxCode") or "").strip(),
            "deleted": bool(str(it.get("PurchasingDocumentDeletionCode") or "").strip()),
            "supplier_material": str(it.get("SupplierMaterialNumber") or "").strip(),
        })
    order_date = parse_date(raw.get("PurchaseOrderDate")) or parse_date(raw.get("CreationDate"))
    return {
        "number": str(raw.get("PurchaseOrder") or "").strip(),
        "supplier": code(raw.get("Supplier")),
        "date": order_date.isoformat() if order_date else None,
        "changed": changed.isoformat() if changed else None,
        "company_code": str(raw.get("CompanyCode") or "").strip(),
        "currency": str(raw.get("DocumentCurrency") or "").strip(),
        "deleted": bool(str(raw.get("PurchasingDocumentDeletionCode") or "").strip()),
        "supplier_name": str(raw.get("AddressName") or "").strip(),
        "items": out_items,
    }


# --- the client ---------------------------------------------------------------------------------------

@dataclass
class Client:
    settings: Settings
    http: object = requests
    calls: list = field(default_factory=list)
    _token: str = ""

    def __post_init__(self):
        s = self.settings
        s.base_url = check_url(s.base_url, s.allowed_hosts, s.allow_private)
        if s.auth == "oauth":
            s.token_url = check_url(s.token_url, (), s.allow_private, "token address")
        self.host = urlsplit(s.base_url).hostname

    # ---- requests

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.settings.auth == "oauth":
            headers["Authorization"] = f"Bearer {self._oauth_token()}"
        return headers

    def _oauth_token(self) -> str:
        if self._token:
            return self._token
        s = self.settings
        call = Call("token", urlsplit(s.token_url).path or "/")
        started = time.monotonic()
        try:
            resp = self.http.post(s.token_url, data={"grant_type": "client_credentials"},
                                  auth=(s.username, s.secret), timeout=s.timeout, allow_redirects=False)
            call.status = resp.status_code
            if resp.status_code != 200:
                raise SapError(f"SAP's sign-in service refused the client id and secret ({resp.status_code}).")
            self._token = str(resp.json().get("access_token") or "")
            if not self._token:
                raise SapError("SAP's sign-in service answered without a token.")
        except SapError as exc:
            call.error = exc.message
            raise
        except (requests.RequestException, ValueError) as exc:
            call.error = type(exc).__name__
            raise SapError("SAP's sign-in service couldn't be reached.") from exc
        finally:
            call.ms = int((time.monotonic() - started) * 1000)
            self.calls.append(call)
        return self._token

    def _get(self, purpose: str, url: str, params: dict | None = None, expect_json: bool = True):
        if urlsplit(url).hostname != self.host or urlsplit(url).scheme != "https":
            raise SapError("SAP pointed to another address for the next page - not followed.")
        s = self.settings
        params = dict(params or {})
        if s.sap_client and "sap-client" not in url:
            params["sap-client"] = s.sap_client
        call = Call(purpose, urlsplit(url).path)
        started = time.monotonic()
        try:
            resp = self.http.get(url, params=params or None, headers=self._headers(),
                                 auth=(s.username, s.secret) if s.auth == "basic" else None,
                                 timeout=s.timeout, allow_redirects=False)
            call.status = resp.status_code
            if resp.status_code in (401, 403):
                raise SapError(f"SAP refused the login ({resp.status_code}) - check the user, its password and "
                               "that it is allowed to read purchase orders.")
            if resp.status_code == 404 and purpose == "supplier":
                raise SapError("SAP's Business Partner service has no supplier under that number (404).")
            if resp.status_code == 404:
                raise SapError("SAP has no such service at that address (404) - check the address and which "
                               "version of the purchase order service the system exposes.")
            if resp.status_code in (301, 302, 303, 307, 308):
                raise SapError("SAP redirected the request elsewhere - not followed. Check the address.")
            if resp.status_code >= 400:
                raise SapError(f"SAP answered with an error ({resp.status_code}).")
            return resp.json() if expect_json else resp.text
        except SapError as exc:
            call.error = exc.message
            raise
        except requests.Timeout as exc:
            call.error = "timeout"
            raise SapError(f"SAP didn't answer within {int(s.timeout)} seconds.") from exc
        except requests.RequestException as exc:
            call.error = type(exc).__name__
            raise SapError("SAP couldn't be reached - check the address, and that SAP lets ARFA's servers in.") from exc
        except ValueError as exc:
            call.error = "not JSON"
            raise SapError("SAP's answer wasn't in the expected form.") from exc
        finally:
            call.ms = int((time.monotonic() - started) * 1000)
            self.calls.append(call)

    # ---- purchase orders

    def _orders_url(self) -> str:
        s = self.settings
        return s.base_url + (V4_PATH + "/PurchaseOrder" if s.api == "v4" else V2_PATH + "/A_PurchaseOrder")

    def _filter(self, since: datetime | None) -> str:
        s = self.settings
        parts = []
        if since is not None:
            stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            parts.append(f"LastChangeDateTime gt {stamp}" if s.api == "v4"
                         else f"LastChangeDateTime gt datetimeoffset'{stamp}'")
        if s.company_code:
            parts.append(f"CompanyCode eq '{_quote(s.company_code)}'")
        if s.purchasing_org:
            parts.append(f"PurchasingOrganization eq '{_quote(s.purchasing_org)}'")
        return " and ".join(parts)

    def orders(self, since: datetime | None = None, max_pages: int = MAX_PAGES):
        """Purchase orders changed after `since`, oldest change first, each
        normalised. Stops after max_pages pages: the next fetch carries on
        from the last change seen."""
        s = self.settings
        params = {"$expand": "_PurchaseOrderItem" if s.api == "v4" else "to_PurchaseOrderItem",
                  "$orderby": "LastChangeDateTime asc", "$top": str(PAGE), "$skip": "0"}
        if s.api != "v4":
            params["$format"] = "json"
        flt = self._filter(since)
        if flt:
            params["$filter"] = flt
        url, skip = self._orders_url(), 0
        for _ in range(max_pages):
            data = self._get("orders", url, params)
            if s.api == "v4":
                rows, nxt = data.get("value") or [], data.get("@odata.nextLink")
            else:
                d = data.get("d") or {}
                rows, nxt = (d.get("results") if isinstance(d, dict) else d) or [], \
                    (d.get("__next") if isinstance(d, dict) else None)
            self.calls[-1].records = len(rows)
            for raw in rows:
                order = normalise(raw, s.api)
                if order["number"]:
                    if s.plant:
                        order["items"] = [i for i in order["items"] if not i["plant"] or i["plant"] == s.plant]
                    yield order
            if nxt:
                url, params = urljoin(url, nxt), None
            elif len(rows) < PAGE:
                return
            else:
                skip += len(rows)
                url, params = self._orders_url(), {**(params or {}), "$skip": str(skip)}
                if "$expand" not in params:      # after a next link: build the query again
                    params = {"$expand": "_PurchaseOrderItem" if s.api == "v4" else "to_PurchaseOrderItem",
                              "$orderby": "LastChangeDateTime asc", "$top": str(PAGE), "$skip": str(skip),
                              **({"$filter": flt} if flt else {}), **({} if s.api == "v4" else {"$format": "json"})}

    # ---- the supplier behind a vendor code

    def supplier(self, vendor: str) -> dict:
        """{"name", "gstin"} of an SAP supplier, from the Business Partner
        service when the technical user may read it; {} otherwise."""
        s = self.settings
        padded = vendor.zfill(10) if vendor.isdigit() else vendor
        out = {}
        try:
            data = self._get("supplier", f"{s.base_url}{PARTNER_PATH}/A_Supplier('{_quote(padded)}')",
                             {"$format": "json", "$select": "Supplier,SupplierName"})
            out["name"] = str(((data.get("d") or {}).get("SupplierName")) or "").strip()
            data = self._get("supplier", f"{s.base_url}{PARTNER_PATH}/A_BusinessPartnerTaxNumber",
                             {"$format": "json", "$filter": f"BusinessPartner eq '{_quote(padded)}' and BPTaxType eq 'IN3'"})
            rows = ((data.get("d") or {}).get("results")) or []
            if rows:
                out["gstin"] = str(rows[0].get("BPTaxNumber") or "").strip().upper()
        except SapError:
            pass                        # a name and GSTIN are a help, not a need
        return out

    # ---- "Test connection"

    def check(self) -> dict:
        """Reaches the service and reads its $metadata: {"ok", "missing":
        [the fields ARFA reads that this system's service doesn't have]}."""
        s = self.settings
        base = s.base_url + (V4_PATH if s.api == "v4" else V2_PATH)
        text = self._get("test", base + "/$metadata", expect_json=False)
        names = set(re.findall(r'<Property[^>]*\bName="([^"]+)"', text))
        missing = [f for f in HEADER_FIELDS + ITEM_FIELDS if f not in names]
        return {"ok": not missing, "missing": missing, "properties": len(names)}


def _quote(value: str) -> str:
    """A value inside an OData string literal."""
    return re.sub(r"[^A-Za-z0-9_\-./ ]", "", str(value)).replace("'", "")
