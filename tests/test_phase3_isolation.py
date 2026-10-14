"""Tenant isolation for every Phase 3 table: tenant A can never read or
write tenant B's stock locations, movements, batches, serials, stock
adjustments/transfers, suppliers, purchase bills/returns/orders, GSTR-2B
imports/entries or private attachments - not by guessing ids in a URL,
and not by smuggling B's ids into A's own forms."""

from datetime import date
from decimal import Decimal

import pytest

from app.inventory import ledger
from app.inventory import services as inv
from app.models.inventory import (
    AdjustmentReason,
    LocationKind,
    ProductBatch,
    ProductSerial,
    StockLocation,
    StockMovement,
)
from app.models.product import Product
from app.models.purchase import Gstr2bEntry, Gstr2bImport, Supplier
from app.models.tenant import Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.purchases import gstr2b
from app.purchases import services as ps
from app.utils.tenant_scope import tenant_query

D = Decimal
ALL_FLAGS = ("inventory", "purchases", "batch_expiry", "serial_imei", "variants")


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


@pytest.fixture()
def world(db, tenant, client_admin):
    """Tenant B with one of everything, and tenant A (the logged-in one)
    with the modules on."""
    for key in ALL_FLAGS:
        tenant.set_feature(key, True)
    b = Tenant(legal_name="Beta Corp", registration_type=RegistrationType.REGULAR)
    db.session.add(b)
    db.session.flush()
    for key in ALL_FLAGS:
        b.set_feature(key, True)
    db.session.add(Gstin(tenant_id=b.id, gstin="29BBBBB0000B1Z5", state_code="29", state_name="Karnataka", is_primary=True))
    admin_b = User(tenant_id=b.id, name="Beta Admin", email="admin@beta.example.com", role=UserRole.CLIENT_ADMIN,
                   must_change_password=False)
    admin_b.set_password("BetaSecret123")
    db.session.add(admin_b)
    product_b = Product(tenant_id=b.id, name="Beta Phone", hsn_or_sac_code="8517", gst_rate=D("18"), track_serial=True,
                        track_batch=True)
    db.session.add(product_b)
    db.session.commit()

    supplier_b = Supplier(tenant_id=b.id, name="Beta Supplier", gstin="29CCCCC1111C1Z5", state_code="29", state_name="Karnataka")
    db.session.add(supplier_b)
    db.session.flush()
    store_b = ledger.default_location(b.id)
    godown_b = inv.create_location(b, "Beta godown", LocationKind.GODOWN)
    bill_b = ps.create_purchase_bill(b, admin_b, ps.PurchaseInput(
        supplier_id=supplier_b.id, gstin_id=b.gstins.first().id, supplier_invoice_no="B-1",
        supplier_invoice_date=date.today(), location_id=store_b.id,
        lines=[ps.PurchaseLineInput(product_id=product_b.id, description="Beta Phone", hsn_or_sac_code="8517",
                                    qty=D("1"), unit="PCS", rate=D("100"), gst_rate=D("18"), batch_no="BB1",
                                    serials=["BETA-IMEI"])],
    ))
    ret_b = ps.create_purchase_return(bill_b, admin_b, {bill_b.lines[0].id: D("1")}, "faulty")
    order_b = ps.create_purchase_order(b, admin_b, supplier_b.id, b.gstins.first().id,
                                       [ps.PurchaseLineInput(product_id=product_b.id, description="Beta Phone",
                                                             hsn_or_sac_code="8517", qty=D("1"), unit="PCS",
                                                             rate=D("100"), gst_rate=D("18"))])
    adj_b = inv.create_adjustment(b, admin_b, store_b.id, AdjustmentReason.FOUND,
                                  [inv.StockLineInput(product_b.id, D("2"))])
    transfer_b = inv.create_transfer(b, admin_b, store_b.id, godown_b.id, [inv.StockLineInput(product_b.id, D("1"))])
    _, rows = gstr2b.parse("b.json", b'{"data":{"docdata":{"b2b":[{"ctin":"29CCCCC1111C1Z5","inv":[{"inum":"B-1","val":118,"items":[{"txval":100,"cgst":9,"sgst":9}]}]}]}}}')
    imp_b = gstr2b.save_import(b, b.gstins.first(), date.today().strftime("%Y-%m"), "b.json", "json", rows, admin_b)
    from app.models.media import UploadedImage

    att = UploadedImage(content_type="application/pdf", data=b"%PDF-secret", is_private=True)
    db.session.add(att)
    db.session.flush()
    bill_b.attachment_id = att.id
    db.session.commit()

    # Tenant A's own things, for the "smuggle B's id into A's form" checks.
    product_a = Product(tenant_id=tenant.id, name="Alpha Soap", hsn_or_sac_code="3401", gst_rate=D("18"))
    supplier_a = Supplier(tenant_id=tenant.id, name="Alpha Supplier", state_code="27", state_name="Maharashtra")
    db.session.add_all([product_a, supplier_a])
    db.session.commit()
    ledger.default_location(tenant.id)
    db.session.commit()
    return {
        "b": b, "admin_b": admin_b, "product_b": product_b, "supplier_b": supplier_b, "store_b": store_b,
        "godown_b": godown_b, "bill_b": bill_b, "ret_b": ret_b, "order_b": order_b, "adj_b": adj_b,
        "transfer_b": transfer_b, "imp_b": imp_b, "entry_b": imp_b.entries[0], "att_b": att,
        "product_a": product_a, "supplier_a": supplier_a,
    }


def test_guessing_ids_in_urls_finds_nothing(client, world, client_admin):
    w = world
    _login(client, client_admin.email, "ClientSecret123")
    gets = [
        f"/inventory/items/{w['product_b'].id}",
        f"/inventory/adjustments/{w['adj_b'].id}",
        f"/inventory/transfers/{w['transfer_b'].id}",
        f"/inventory/transfers/{w['transfer_b'].id}/challan.pdf",
        f"/purchases/{w['bill_b'].id}",
        f"/purchases/{w['bill_b'].id}/attachment",
        f"/purchases/{w['bill_b'].id}/return",
        f"/purchases/returns/{w['ret_b'].id}",
        f"/purchases/orders/{w['order_b'].id}",
        f"/purchases/new?order={w['order_b'].id}",
        f"/purchases/suppliers/{w['supplier_b'].id}/edit",
        f"/purchases/gstr2b/{w['imp_b'].id}",
        f"/media/{w['att_b'].id}",
    ]
    for url in gets:
        assert client.get(url).status_code == 404, url
    posts = [
        (f"/inventory/locations/{w['godown_b'].id}/deactivate", {}),
        (f"/inventory/adjustments/{w['adj_b'].id}/void", {"reason": "x"}),
        (f"/inventory/transfers/{w['transfer_b'].id}/void", {"reason": "x"}),
        (f"/purchases/{w['bill_b'].id}/void", {"reason": "x"}),
        (f"/purchases/{w['bill_b'].id}/return", {"reason": "x"}),
        (f"/purchases/returns/{w['ret_b'].id}/void", {"reason": "x"}),
        (f"/purchases/orders/{w['order_b'].id}/cancel", {"reason": "x"}),
        (f"/purchases/suppliers/{w['supplier_b'].id}/deactivate", {}),
        (f"/purchases/gstr2b/entries/{w['entry_b'].id}/match", {"bill_id": w["bill_b"].id}),
        (f"/purchases/gstr2b/entries/{w['entry_b'].id}/unmatch", {}),
        (f"/purchases/gstr2b/{w['imp_b'].id}/rematch", {}),
    ]
    for url, data in posts:
        assert client.post(url, data=data).status_code == 404, url
    # Nothing of B's changed.
    assert w["godown_b"].is_active and w["supplier_b"].is_active
    assert w["adj_b"].status.value == "completed" and w["ret_b"].status.value == "posted"


def test_lists_never_show_another_clients_rows(client, world, client_admin):
    _login(client, client_admin.email, "ClientSecret123")
    pages = ["/inventory/", "/inventory/locations", "/inventory/adjustments", "/inventory/transfers",
             "/inventory/by-location", "/inventory/serials?q=BETA", "/inventory/expiry", "/inventory/labels",
             "/purchases/", "/purchases/returns", "/purchases/orders", "/purchases/suppliers", "/purchases/gstr2b",
             "/purchases/new", "/inventory/adjustments/new"]
    for url in pages:
        body = client.get(url).data
        for secret in (b"Beta Phone", b"Beta Supplier", b"Beta godown", b"BETA-IMEI", b"29BBBBB0000B1Z5"):
            assert secret not in body, (url, secret)


def test_forms_refuse_another_clients_ids(db, tenant, client_admin, world):
    w = world
    store_a = ledger.default_location(tenant.id)
    gstin_a = tenant.gstins.first()
    # Stock documents: B's location or B's product.
    with pytest.raises(ledger.StockError):
        inv.create_adjustment(tenant, client_admin, w["store_b"].id, AdjustmentReason.FOUND,
                              [inv.StockLineInput(w["product_a"].id, D("1"))])
    with pytest.raises(ledger.StockError):
        inv.create_adjustment(tenant, client_admin, store_a.id, AdjustmentReason.FOUND,
                              [inv.StockLineInput(w["product_b"].id, D("1"))])
    with pytest.raises(ledger.StockError):
        inv.create_transfer(tenant, client_admin, store_a.id, w["godown_b"].id,
                            [inv.StockLineInput(w["product_a"].id, D("1"))])
    db.session.rollback()

    def bill_input(**over):
        data = dict(supplier_id=w["supplier_a"].id, gstin_id=gstin_a.id, supplier_invoice_no="A-1",
                    supplier_invoice_date=date.today(), location_id=store_a.id,
                    lines=[ps.PurchaseLineInput(product_id=w["product_a"].id, description="Soap",
                                                hsn_or_sac_code="3401", qty=D("1"), unit="PCS", rate=D("10"),
                                                gst_rate=D("18"))])
        data.update(over)
        return ps.PurchaseInput(**data)

    for over in ({"supplier_id": w["supplier_b"].id}, {"gstin_id": w["b"].gstins.first().id},
                 {"location_id": w["store_b"].id},
                 {"lines": [ps.PurchaseLineInput(product_id=w["product_b"].id, description="x", hsn_or_sac_code="8517",
                                                 qty=D("1"), unit="PCS", rate=D("1"), gst_rate=D("18"))]}):
        with pytest.raises(ps.PurchaseError):
            ps.create_purchase_bill(tenant, client_admin, bill_input(**over))
        db.session.rollback()
    with pytest.raises(ps.PurchaseError):
        ps.create_purchase_order(tenant, client_admin, w["supplier_b"].id, gstin_a.id, bill_input().lines)
    db.session.rollback()

    # GSTR-2B: A's import can't be matched to B's bill.
    _, rows = gstr2b.parse("a.json", b'{"data":{"docdata":{"b2b":[{"ctin":"27CCCCC1111C1Z5","inv":[{"inum":"X","val":1}]}]}}}')
    imp_a = gstr2b.save_import(tenant, gstin_a, "2026-01", "a.json", "json", rows, client_admin)
    with pytest.raises(gstr2b.Gstr2bError):
        gstr2b.manual_match(imp_a.entries[0], w["bill_b"])
    # B's serial number can be bought again by A - serials are unique per client only.
    assert ProductSerial.query.filter_by(serial_no="BETA-IMEI").count() == 1


def test_stock_numbers_are_per_client(db, tenant, client_admin, world):
    w = world
    assert ledger.stock_on_hand(tenant.id) == {}
    assert ledger.stock_on_hand(w["b"].id, [w["product_b"].id])[w["product_b"].id] == D("2")  # 1 bought - 1 returned + 2 found
    assert ledger.stock_on_hand(tenant.id, [w["product_b"].id]) == {}


def test_tenant_query_scopes_every_new_table(app, db, tenant, client_admin, world):
    from flask_login import login_user

    from app.models.inventory import (
        StockAdjustment,
        StockAdjustmentLine,  # noqa: F401 - lines are reached only through their tenant-scoped document
        StockTransfer,
    )
    from app.models.purchase import PurchaseBill, PurchaseOrder, PurchaseReturn

    with app.test_request_context():
        login_user(client_admin)
        for model in (StockLocation, StockMovement, ProductBatch, ProductSerial, StockAdjustment, StockTransfer,
                      Supplier, PurchaseBill, PurchaseReturn, PurchaseOrder, Gstr2bImport, Gstr2bEntry):
            rows = tenant_query(model).all()
            assert all(r.tenant_id == tenant.id for r in rows), model.__name__
            assert not any(r.tenant_id == world["b"].id for r in rows), model.__name__
