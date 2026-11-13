"""The SAP link (app/integrations/sap): purchase orders fetched from SAP's
OData service (V2 or V4), SAP's codes matched to the business's suppliers
and items, orders made and kept up to date - read only, every call logged."""

from datetime import date, timezone
from decimal import Decimal

import pytest
import requests

from app.integrations.sap import client as sap
from app.integrations.sap import sync
from app.models.audit_log import AuditLog
from app.models.product import Product
from app.models.purchase import PurchaseOrder, PurchaseOrderStatus, Supplier
from app.models.sap import SapCall, SapConnection, SapMapping, SapOrder
from app.purchases import matching
from app.purchases import services as ps
from tests.test_po_matching import _bill_for, _pline
from tests.test_scan import SELLER

D = Decimal
S = PurchaseOrderStatus
BASE = "https://sap.example.com"


class Answer:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data, self.text = status, data, text

    def json(self):
        if self._data is None:
            raise ValueError("no JSON")
        return self._data


def v2_order(number="4500000001", supplier="0000100234", items=None, changed=1762000000000, deleted="", **over):
    return {"PurchaseOrder": number, "Supplier": supplier, "CompanyCode": "1000", "DocumentCurrency": "INR",
            "PurchaseOrderDate": "/Date(1761955200000)/", "LastChangeDateTime": f"/Date({changed}+0000)/",
            "PurchasingDocumentDeletionCode": deleted,
            "to_PurchaseOrderItem": {"results": items if items is not None else [v2_item()]}, **over}


def v2_item(item="00010", material="000000000000004711", text="CUMIN SEEDS 1KG PKT", qty="25.000", unit="KG",
            price="400.00", per="1", deleted="", **over):
    return {"PurchaseOrderItem": item, "Material": material, "PurchaseOrderItemText": text, "OrderQuantity": qty,
            "PurchaseOrderQuantityUnit": unit, "NetPriceAmount": price, "NetPriceQuantity": per, "Plant": "1010",
            "TaxCode": "G5", "PurchasingDocumentDeletionCode": deleted, **over}


class FakeSap:
    """Answers like an S/4HANA system: the purchase order service (V2 and
    V4 shapes), its $metadata, and the Business Partner service."""

    def __init__(self, orders=None, partners=None, fail=None, token=False):
        self.orders, self.partners, self.fail = orders or [], partners or {}, fail
        self.gets, self.posts, self.token = [], [], token

    def post(self, url, data=None, auth=None, timeout=None, allow_redirects=True):
        self.posts.append((url, data, auth))
        return Answer(200, {"access_token": "tok-123"})

    def get(self, url, params=None, headers=None, auth=None, timeout=None, allow_redirects=True):
        params = params or {}
        self.gets.append({"url": url, "params": dict(params), "auth": auth, "headers": headers or {}})
        if self.fail == "timeout":
            raise requests.Timeout()
        if isinstance(self.fail, int):
            return Answer(self.fail, {})
        if url.endswith("/$metadata"):
            names = sap.HEADER_FIELDS + sap.ITEM_FIELDS
            return Answer(200, text="".join(f'<Property Name="{n}" Type="Edm.String"/>' for n in names
                                            if n != self.fail))
        if "API_BUSINESS_PARTNER" in url:
            code = (url.split("('")[1].split("')")[0] if "A_Supplier(" in url
                    else params["$filter"].split("'")[1])
            partner = self.partners.get(code.lstrip("0"))
            if partner is None:
                return Answer(404, {})
            if "A_Supplier(" in url:
                return Answer(200, {"d": {"Supplier": code, "SupplierName": partner["name"]}})
            rows = [{"BusinessPartner": code, "BPTaxType": "IN3", "BPTaxNumber": partner["gstin"]}] \
                if partner.get("gstin") else []
            return Answer(200, {"d": {"results": rows}})
        skip, top = int(params.get("$skip", 0)), int(params.get("$top", sap.PAGE))
        page = self.orders[skip:skip + top]
        if "/odata4/" in url:
            return Answer(200, {"value": [v4(o) for o in page]})
        return Answer(200, {"d": {"results": page}})


def v4(order: dict) -> dict:
    out = {k: v for k, v in order.items() if k != "to_PurchaseOrderItem"}
    out["_PurchaseOrderItem"] = order["to_PurchaseOrderItem"]["results"]
    out["PurchaseOrderDate"], out["LastChangeDateTime"] = "2026-11-01", "2026-11-01T12:26:40Z"
    return out


@pytest.fixture()
def linked(db, tenant, client_admin):
    for key in ("purchases", "inventory", "sap_link"):
        tenant.set_feature(key, True)
    supplier = Supplier(tenant_id=tenant.id, name="Tumkur Spices Pvt Ltd", gstin=SELLER, state_code="29",
                        state_name="Karnataka")
    cumin = Product(tenant_id=tenant.id, name="Cumin seeds 1kg", hsn_or_sac_code="0909", gst_rate=D("5"), unit="KGS",
                    sale_price=D("500"), sku="4711")
    tin = Product(tenant_id=tenant.id, name="Steel container", hsn_or_sac_code="7323", gst_rate=D("18"), unit="NOS",
                  sale_price=D("700"))
    db.session.add_all([supplier, cumin, tin])
    conn = sync.save_connection(tenant, client_admin, _Form(base_url=BASE + "/", username="ARFA_READ", secret="s3cret",
                                                            api="v2", enabled="1", lookup_partners="1"))
    db.session.commit()
    return type("L", (), dict(tenant=tenant, admin=client_admin, supplier=supplier, cumin=cumin, tin=tin, conn=conn))


class _Form(dict):
    def get(self, key, default=None, type=None):
        value = super().get(key, default)
        if type is not None and value not in (None, ""):
            try:
                return type(value)
            except (TypeError, ValueError):
                return default
        return value


PARTNERS = {"100234": {"name": "TUMKUR SPICES PRIVATE LIMITED", "gstin": SELLER}}


# --- reading SAP ---------------------------------------------------------------------------------------

def test_sap_values_are_read_in_both_odata_versions():
    assert sap.parse_when("/Date(1762000000000+0000)/").isoformat() == "2025-11-01T12:26:40+00:00"
    assert sap.parse_date("2026-11-01") == date(2026, 11, 1) and sap.parse_when("") is None
    assert sap.code("0000100234") == "100234" and sap.code("MAT-01") == "MAT-01"
    for api, raw in (("v2", v2_order()), ("v4", v4(v2_order()))):
        order = sap.normalise(raw, api)
        assert (order["number"], order["supplier"], order["deleted"]) == ("4500000001", "100234", False)
        assert order["items"] == [{"item": "10", "material": "4711", "text": "CUMIN SEEDS 1KG PKT", "qty": "25.000",
                                   "unit": "KG", "rate": "400.00", "plant": "1010", "tax_code": "G5", "deleted": False,
                                   "supplier_material": ""}]
    # A price "per 100", and a price unit that isn't the order unit.
    per = sap.normalise(v2_order(items=[v2_item(price="4500.00", per="100", OrderPriceUnitToOrderUnitNmrtr="2",
                                                OrdPriceUnitToOrderUnitDnmntr="1")]), "v2")
    assert per["items"][0]["rate"] == "90.00"


@pytest.mark.parametrize("url, message", [
    ("http://sap.example.com", "https://"), ("https://user:pw@sap.example.com", "user name or password"),
    ("https://10.1.2.3", "public address"), ("https://localhost", "public address"), ("sap.example.com", "https://"),
])
def test_only_a_public_https_address_is_accepted(url, message):
    with pytest.raises(sap.SapError, match=message):
        sap.check_url(url)
    assert sap.check_url("https://10.1.2.3/", allow_private=True) == "https://10.1.2.3"
    with pytest.raises(sap.SapError, match="isn't on the list"):
        sap.check_url(BASE, allowed_hosts=("other.example.com",))


def test_the_request_asks_for_changes_since_the_last_fetch_and_pages(app):
    from datetime import datetime, timezone

    fake = FakeSap([v2_order(number=str(4500000000 + i)) for i in range(sap.PAGE + 5)])
    client = sap.Client(sap.Settings(base_url=BASE, username="U", secret="P", sap_client="100", company_code="1000"),
                        http=fake)
    got = list(client.orders(datetime(2026, 11, 1, tzinfo=timezone.utc)))
    assert len(got) == sap.PAGE + 5 and len(fake.gets) == 2
    first = fake.gets[0]
    assert first["url"] == BASE + "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder"
    assert first["params"]["$filter"] == ("LastChangeDateTime gt datetimeoffset'2026-11-01T00:00:00Z' "
                                          "and CompanyCode eq '1000'")
    assert first["params"]["$expand"] == "to_PurchaseOrderItem" and first["params"]["sap-client"] == "100"
    assert first["auth"] == ("U", "P") and fake.gets[1]["params"]["$skip"] == str(sap.PAGE)
    assert [c.records for c in client.calls] == [sap.PAGE, 5]

    fake4 = FakeSap([v2_order()], token=True)
    client4 = sap.Client(sap.Settings(base_url=BASE, api="v4", auth="oauth", username="cid", secret="cs",
                                      token_url="https://auth.example.com/oauth/token"), http=fake4)
    assert [o["number"] for o in client4.orders(datetime(2026, 11, 1, tzinfo=timezone.utc))] == ["4500000001"]
    assert fake4.gets[0]["url"].endswith("/sap/opu/odata4/sap/api_purchaseorder_2/srvd_a2x/sap/purchaseorder/0001/PurchaseOrder")
    assert fake4.gets[0]["params"]["$filter"] == "LastChangeDateTime gt 2026-11-01T00:00:00Z"
    assert fake4.gets[0]["params"]["$expand"] == "_PurchaseOrderItem" and fake4.gets[0]["auth"] is None
    assert fake4.gets[0]["headers"]["Authorization"] == "Bearer tok-123"
    assert fake4.posts == [("https://auth.example.com/oauth/token", {"grant_type": "client_credentials"}, ("cid", "cs"))]


def test_a_next_page_on_another_host_is_not_followed(app):
    class Elsewhere(FakeSap):
        def get(self, url, **kw):
            self.gets.append({"url": url})
            return Answer(200, {"d": {"results": [v2_order()], "__next": "https://evil.example.net/steal?x=1"}})

    client = sap.Client(sap.Settings(base_url=BASE, username="U", secret="P"), http=Elsewhere())
    with pytest.raises(sap.SapError, match="another address"):
        list(client.orders())
    assert len(client.http.gets) == 1


# --- the fetch ----------------------------------------------------------------------------------------------

def test_an_order_comes_in_with_its_supplier_matched_by_gstin_and_item_by_sku(db, linked):
    fake = FakeSap([v2_order(items=[v2_item(), v2_item(item="00020", material="000000000000009000",
                                                       text="SS CONTAINER 5L", qty="2", unit="EA", price="500")])],
                   PARTNERS)
    counts = sync.pull(linked.tenant, linked.admin, http=fake)
    db.session.commit()
    assert counts == {"fetched": 1, "new": 1, "changed": 0, "imported": 1, "updated": 0, "waiting": 0, "cancelled": 0}
    order = PurchaseOrder.query.one()
    assert (order.number, order.source, order.external_ref, order.supplier_id, order.status) == \
        ("4500000001", "sap", "4500000001", linked.supplier.id, S.OPEN)
    assert order.order_date == date(2025, 11, 1) and order.from_sap
    cumin, tin = order.lines
    assert (cumin.product_id, cumin.description, cumin.qty, cumin.unit, cumin.rate, cumin.gst_rate,
            cumin.hsn_or_sac_code, cumin.external_ref, cumin.external_code) == \
        (linked.cumin.id, "Cumin seeds 1kg", D("25"), "KGS", D("400"), D("5"), "0909", "10", "4711")
    # The item SAP knows by a number ARFA doesn't: in by SAP's own words, waiting to be matched.
    assert (tin.product_id, tin.description, tin.unit, tin.gst_rate) == (None, "SS CONTAINER 5L", "NOS", D("0"))
    maps = {(m.kind, m.sap_code): m for m in SapMapping.query.all()}
    assert maps[("supplier", "100234")].how == "gstin" and maps[("supplier", "100234")].sap_name.startswith("TUMKUR")
    assert maps[("material", "4711")].how == "sku" and not maps[("material", "9000")].mapped
    conn = SapConnection.query.one()
    assert conn.last_pull_ok and conn.cursor.astimezone(timezone.utc).isoformat().startswith("2025-11-01T12:26:40")
    # Every call is on record, without the login.
    calls = SapCall.query.order_by(SapCall.id).all()
    assert [c.purpose for c in calls] == ["orders", "supplier", "supplier"] and calls[0].records == 1
    assert all("s3cret" not in (c.path + (c.error or "")) for c in calls)
    assert "s3cret" not in (conn.secrets or "") and sync.settings_of(conn).secret == "s3cret"


def test_an_unknown_supplier_waits_and_comes_in_once_matched(client, db, linked):
    fake = FakeSap([v2_order(supplier="0000555000")])            # SAP has no GSTIN for this vendor
    counts = sync.pull(linked.tenant, linked.admin, http=fake)
    db.session.commit()
    assert counts["waiting"] == 1 and counts["imported"] == 0 and PurchaseOrder.query.count() == 0
    held = SapOrder.query.one()
    assert held.status == "waiting" and "555000" in held.note

    client.post("/auth/login", data={"email": "admin@acme.example.com", "password": "ClientSecret123"})
    page = client.get("/sap/map").get_data(as_text=True)
    assert "555000" in page and "1 to match" in page and "Tumkur Spices Pvt Ltd" in page
    row = SapMapping.query.filter_by(kind="supplier").one()
    res = client.post("/sap/map", data={"mapping_id": row.id, "target_id": linked.supplier.id}, follow_redirects=True)
    assert "matched. 1 order came in." in res.get_data(as_text=True)
    order = PurchaseOrder.query.one()
    assert order.supplier_id == linked.supplier.id and SapOrder.query.one().status == "imported"
    assert AuditLog.query.filter_by(action="sap_code_mapped").count() == 1
    # Only the business's own suppliers can be picked.
    res = client.post("/sap/map", data={"mapping_id": row.id, "target_id": 999999}, follow_redirects=True)
    assert "Pick one of your own" in res.get_data(as_text=True)


def test_fetching_again_updates_and_never_doubles(db, linked):
    fake = FakeSap([v2_order()], PARTNERS)
    sync.pull(linked.tenant, linked.admin, http=fake)
    db.session.commit()
    # Changed in SAP: more ordered, dearer, and a second item added.
    fake.orders = [v2_order(changed=1762100000000, items=[
        v2_item(qty="40", price="410"), v2_item(item="00020", material="", text="Packing", qty="1", unit="EA", price="50")])]
    counts = sync.pull(linked.tenant, linked.admin, http=fake)
    db.session.commit()
    assert counts["changed"] == 1 and counts["updated"] == 1 and counts["imported"] == 0
    order = PurchaseOrder.query.one()
    assert [(l.description, l.qty, l.rate) for l in order.lines] == [("Cumin seeds 1kg", D("40"), D("410")),
                                                                    ("Packing", D("1"), D("50"))]
    assert fake.gets[-1]["params"]["$filter"].startswith("LastChangeDateTime gt datetimeoffset'2025-11-01T12:24:40Z'")
    assert SapOrder.query.count() == 1


def test_what_is_billed_survives_changes_in_sap(db, linked):
    fake = FakeSap([v2_order()], PARTNERS)
    sync.pull(linked.tenant, linked.admin, http=fake)
    db.session.commit()
    order = PurchaseOrder.query.one()
    b = type("B", (), dict(tenant=linked.tenant, admin=linked.admin, supplier=linked.supplier))
    _bill_for(db, b, order, _pline(linked.cumin, 10, 400))
    assert order.status == S.PARTLY
    # SAP cuts the order below what is billed, then deletes it.
    fake.orders = [v2_order(changed=1762100000000, items=[v2_item(qty="6")])]
    sync.pull(linked.tenant, linked.admin, http=fake)
    db.session.commit()
    assert order.lines[0].qty == D("10") and order.lines[0].billed_qty == D("10") and order.status == S.CONVERTED
    fake.orders = [v2_order(changed=1762200000000, deleted="L")]
    sync.pull(linked.tenant, linked.admin, http=fake)
    db.session.commit()
    assert order.status == S.CONVERTED and len(order.bills) == 1


def test_an_order_deleted_in_sap_is_cancelled_here_and_cannot_be_cancelled_from_arfa(db, linked):
    fake = FakeSap([v2_order()], PARTNERS)
    sync.pull(linked.tenant, linked.admin, http=fake)
    db.session.commit()
    order = PurchaseOrder.query.one()
    with pytest.raises(ps.PurchaseError, match="came from SAP"):
        ps.cancel_purchase_order(order, "not needed")
    fake.orders = [v2_order(changed=1762100000000, deleted="L")]
    assert sync.pull(linked.tenant, linked.admin, http=fake)["cancelled"] == 1
    db.session.commit()
    assert order.status == S.CANCELLED and order.cancel_reason == "Deleted in SAP."
    # An order deleted before it was ever fetched isn't brought in at all.
    fake.orders = [v2_order(number="4500000002", changed=1762300000000, deleted="L")]
    sync.pull(linked.tenant, linked.admin, http=fake)
    db.session.commit()
    assert PurchaseOrder.query.count() == 1 and SapOrder.query.filter_by(number="4500000002").one().status == "skipped"


def test_a_scanned_bill_finds_the_sap_order_by_its_number(db, linked):
    sync.pull(linked.tenant, linked.admin, http=FakeSap([v2_order()], PARTNERS))
    db.session.commit()
    rows = [matching.Row("Cumin seeds 1kg", D("10"), D("400"), D("5"), linked.cumin.id, "0909")]
    found = matching.candidates(linked.tenant, linked.supplier, "PO No. 45 0000 0001", rows, date(2026, 11, 13))
    assert found.proposed.order.number == "4500000001" and found.proposed.by == "number" and found.proposed.clean


@pytest.mark.parametrize("fail, message", [(401, "refused the login"), (404, "no such service"),
                                           (500, "answered with an error"), ("timeout", "didn't answer")])
def test_a_failed_fetch_says_why_and_changes_nothing(db, linked, fail, message):
    with pytest.raises(sync.SyncError, match=message):
        sync.pull(linked.tenant, linked.admin, http=FakeSap([v2_order()], fail=fail))
    conn = SapConnection.query.one()
    assert conn.last_pull_ok is False and message in conn.last_pull_message and conn.cursor is None
    assert PurchaseOrder.query.count() == 0 and SapOrder.query.count() == 0
    assert SapCall.query.count() == 1 and SapCall.query.one().error


# --- the screens ------------------------------------------------------------------------------------------

def _login(client):
    client.post("/auth/login", data={"email": "admin@acme.example.com", "password": "ClientSecret123"})


def test_the_connection_page_saves_tests_and_fetches(client, db, tenant, client_admin, monkeypatch):
    for key in ("purchases", "sap_link"):
        tenant.set_feature(key, True)
    db.session.commit()
    _login(client)
    page = client.get("/sap/").get_data(as_text=True)
    assert "SAP link" in page and "nothing in SAP is ever changed" in page and 'action="/sap/fetch"' not in page
    bad = client.post("/sap/", data={"base_url": "http://sap.example.com", "username": "U", "secret": "P"})
    assert bad.status_code == 400 and "https://" in bad.get_data(as_text=True)
    ok = client.post("/sap/", data={"base_url": BASE, "api": "v4", "auth": "basic", "username": "ARFA_READ",
                                    "secret": "s3cret", "sap_client": "100", "enabled": "1"})
    assert ok.status_code == 302
    conn = SapConnection.query.one()
    assert (conn.base_url, conn.api, conn.username, conn.sap_client, conn.enabled) == (BASE, "v4", "ARFA_READ", "100", True)
    page = client.get("/sap/").get_data(as_text=True)
    assert "s3cret" not in page and "Saved - leave empty to keep it" in page and 'action="/sap/fetch"' in page
    # Saving again without typing the password keeps it.
    client.post("/sap/", data={"base_url": BASE, "api": "v2", "username": "ARFA_READ", "secret": ""})
    assert sync.settings_of(SapConnection.query.one()).secret == "s3cret"

    fake = FakeSap([v2_order()], PARTNERS, fail="NetPriceAmount")
    monkeypatch.setattr(sync, "transport", fake)
    page = client.post("/sap/test", follow_redirects=True).get_data(as_text=True)
    assert "lacks: NetPriceAmount" in page
    fake.fail = None
    assert "has every field ARFA reads" in client.post("/sap/test", follow_redirects=True).get_data(as_text=True)
    page = client.post("/sap/fetch", follow_redirects=True).get_data(as_text=True)
    assert "1 order read from SAP" in page and "waiting for a supplier to be matched" in page
    assert "Calls made to SAP" in page and AuditLog.query.filter_by(action="sap_orders_fetched").count() == 1
    saved = AuditLog.query.filter_by(action="sap_connection_saved").first()
    assert "s3cret" not in str(saved.details)


def test_sap_orders_carry_a_tag_and_no_cancel_button(client, db, linked):
    sync.pull(linked.tenant, linked.admin, http=FakeSap([v2_order()], PARTNERS))
    db.session.commit()
    _login(client)
    order = PurchaseOrder.query.one()
    listing = client.get("/purchases/orders").get_data(as_text=True)
    assert '4500000001</a> <span class="tag">SAP</span>' in listing and "Fetch from SAP" in listing
    page = client.get(f"/purchases/orders/{order.id}").get_data(as_text=True)
    assert "Fetched from SAP" in page and "Cancel order" not in page and "SAP 4711" in page


def test_the_link_is_a_module_for_the_owner_only(client, db, tenant, client_admin, staff):
    tenant.set_feature("purchases", True)
    db.session.commit()
    _login(client)
    assert client.get("/sap/").status_code == 404                      # the module is off
    tenant.set_feature("sap_link", True)
    db.session.commit()
    assert client.get("/sap/").status_code == 200
    assert "SAP link" in client.get("/setup").get_data(as_text=True)
    client.get("/auth/logout")
    client.post("/auth/login", data={"email": staff.email, "password": "CashierSecret123"})
    assert client.get("/sap/").status_code == 403 and client.post("/sap/fetch").status_code == 403


def test_the_schedule_fetches_for_every_business_with_the_link_on(app, db, linked, monkeypatch):
    fake = FakeSap([v2_order()], PARTNERS)
    monkeypatch.setattr(sync, "transport", fake)
    results = sync.pull_all()
    assert len(results) == 1 and results[0][0] == linked.tenant.id and results[0][1].startswith("ok")
    assert PurchaseOrder.query.one().created_by_id == linked.admin.id          # entered in the owner's name
    linked.conn.enabled = False
    db.session.commit()
    assert sync.pull_all() == []
    assert app.test_cli_runner().invoke(args=["sap-pull"]).output.strip() == "No business has the SAP link switched on."
