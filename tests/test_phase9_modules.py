"""Phase 9: business-type modules - restaurant (order types, tables, KOT,
charges, sold out), weighing-scale labels, pharmacy (schedules, packs,
substitutes, register), electronics (warranty from sale), jewellery
(metal-rate pricing, job-work making, old gold after GST), job cards,
wholesale (groups, slabs, salesmen), loyalty - each behind its flag, with
tenant isolation for every new table."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.inventory import ledger
from app.inventory import services as inv
from app.jewellery import services as jewel
from app.jobs import services as jobs
from app.loyalty import points
from app.models.business import (
    CustomerGroup, DiningTable, JobCard, JobStatus, KitchenTicket, KotStatus, LoyaltyEntry, MetalRate, OldGoldPurchase,
    OldGoldStatus, PriceSlab, Salesman,
)
from app.models.customer import Customer
from app.models.inventory import AdjustmentReason, ProductSerial, SerialStatus
from app.models.pos_bill import PaymentMode, POSBill, POSBillStatus
from app.models.price_list import PriceList, PriceListItem
from app.models.product import Product
from app.models.tenant import BillingCycle, Gstin, RegistrationType, Tenant
from app.models.user import User, UserRole
from app.pos.services import CartInput, CartLineInput, PaymentInput, POSValidationError, checkout, void_bill
from app.restaurant import services as rest
from app.utils import gst_config
from app.utils.gst import compute_line
from app.utils.jewellery import JewelleryError, derived_rate, old_gold_value, price
from app.utils.module_settings import save_module_settings
from app.utils.scale_barcode import ean13_check_digit, parse
from app.wholesale.pricing import unit_price

D = Decimal
TODAY = date.today()


def _login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _on(db, tenant, *flags):
    for f in flags:
        tenant.set_feature(f, True)
    db.session.commit()


def _product(db, tenant, name, price_, rate="5", unit="NOS", **kw):
    p = Product(tenant_id=tenant.id, name=name, hsn_or_sac_code=kw.pop("hsn", "2106"), gst_rate=D(rate), unit=unit,
                sale_price=D(price_), **kw)
    db.session.add(p)
    db.session.commit()
    return p


def _line(p, qty="1", **kw):
    return CartLineInput(description=p.name, hsn_or_sac_code=p.hsn_or_sac_code, qty=D(qty), rate=p.sale_price,
                         gst_rate=p.gst_rate, product_id=p.id, unit=p.unit, **kw)


def _stock(db, tenant, user, product, qty, cost="10", **kw):
    loc = ledger.default_location(tenant.id)
    inv.create_adjustment(tenant, user, loc.id, AdjustmentReason.OPENING,
                          [inv.StockLineInput(product.id, D(qty), D(cost), **kw)])
    db.session.commit()


def _on_hand(tenant, product):
    return ledger.stock_on_hand(tenant.id, [product.id]).get(product.id, D("0"))


# --- jewellery pricing (the one module) ---------------------------------------------------

def test_jewellery_price_making_kinds_and_stones():
    p = price("gold", "22K", "10.255", "6500", "per_gram", "450", "1200")
    assert p.net_weight == D("10.255") and p.metal_value == D("66657.50")
    assert p.making == D("4614.75") and p.stones == D("1200.00") and p.total == D("72472.25")
    assert price("gold", "22K", "10", "6500", "percent", "12", 0).making == D("7800.00")
    assert price("gold", "22K", "10", "6500", "flat", "999.5", 0).making == D("999.50")
    assert price("gold", "22K", "10", "6500", None, None, None).total == D("65000.00")
    with pytest.raises(JewelleryError):
        price("gold", "22K", "0", "6500", None, None, None)
    with pytest.raises(JewelleryError):
        price("gold", "22K", "5", "0", None, None, None)


def test_jewellery_gst_three_percent_intra_inter_and_rounding():
    taxable = price("gold", "22K", "3.333", "6543.21", "per_gram", "333.33", "0").total
    intra = compute_line(qty=D(1), rate=taxable, discount_percent=D(0), gst_rate=gst_config.JEWELLERY_GST_RATE,
                         is_intra_state=True, charge_gst=True)
    inter = compute_line(qty=D(1), rate=taxable, discount_percent=D(0), gst_rate=gst_config.JEWELLERY_GST_RATE,
                         is_intra_state=False, charge_gst=True)
    assert intra["cgst_amount"] == intra["sgst_amount"] and intra["igst_amount"] == 0
    assert inter["igst_amount"] == (taxable * D("0.03")).quantize(D("0.01"))
    assert intra["cgst_amount"] + intra["sgst_amount"] - inter["igst_amount"] in (D("0"), D("0.01"), D("-0.01"))


def test_rate_derived_from_pure_gold_and_old_gold_value():
    assert derived_rate("gold", "22K", {"24K": D("7000")}) == (D("7000") / D("0.999") * D("0.916")).quantize(D("0.01"))
    assert derived_rate("gold", "22K", {"22K": D("6400"), "24K": D("7000")}) == D("6400.00")
    assert derived_rate("silver", "925", {}) is None
    assert old_gold_value("8.5", "6000") == D("51000.00")


# --- weighing-scale labels ------------------------------------------------------------------

def _label(prefix, plu, value, digits=5):
    body = f"{prefix}{str(plu).zfill(digits)}{str(value).zfill(10 - digits)}"
    return body + str(ean13_check_digit(body))


def test_scale_label_weight_and_price():
    prefixes = {"21": "weight", "22": "price"}
    w = parse(_label("21", 42, 1250), prefixes)
    assert w.plu == "42" and w.kind == "weight" and w.value == D("1.25")
    pr = parse(_label("22", 7, 8950), prefixes)
    assert pr.kind == "price" and pr.value == D("89.50")
    bad = _label("21", 42, 1250)
    assert parse(bad[:-1] + str((int(bad[-1]) + 1) % 10), prefixes) is None  # check digit
    assert parse(_label("23", 42, 1250), prefixes) is None and parse("8901234567890", prefixes) is None
    six = parse(_label("21", 123456, 500, digits=6), prefixes, code_digits=6)
    assert six.plu == "123456" and six.value == D("0.5")


def test_scale_scan_endpoint(client, db, tenant, client_admin):
    _on(db, tenant, "weighing")
    p = _product(db, tenant, "Mysore Pak", "640", unit="KGS", plu_code="42")
    _login(client, client_admin.email, "ClientSecret123")
    r = client.get(f"/pos/scale-scan?code={_label('21', 42, 750)}").get_json()
    assert r["product_id"] == p.id and r["qty"] == 0.75
    r = client.get(f"/pos/scale-scan?code={_label('22', 42, 32000)}").get_json()
    assert r["qty"] == 0.5  # Rs. 320 of a Rs. 640/kg item
    assert client.get(f"/pos/scale-scan?code={_label('21', 99, 750)}").status_code == 404


# --- restaurant -----------------------------------------------------------------------------

@pytest.fixture()
def menu(db, tenant):
    return (_product(db, tenant, "Chicken Biriyani", "200", hsn="996331"),
            _product(db, tenant, "Lime Soda", "60", hsn="996331"))


def test_table_kot_deltas_and_billing(db, tenant, client_admin, menu):
    _on(db, tenant, "restaurant_mode")
    biriyani, soda = menu
    table = DiningTable(tenant_id=tenant.id, name="T1")
    db.session.add(table)
    db.session.commit()
    k1 = rest.send_kot(tenant, client_admin, table, CartInput(lines=[_line(biriyani, "2", note="less spicy"), _line(soda)]))
    db.session.commit()
    assert [(i["name"], i["qty"], i["note"]) for i in k1.items] == [("Chicken Biriyani", "2", "less spicy"), ("Lime Soda", "1", "")]
    assert rest.running_order(tenant.id, table.id).table_id == table.id
    # One soda less, one biriyani more: the next ticket carries only the change.
    k2 = rest.send_kot(tenant, client_admin, table, CartInput(lines=[_line(biriyani, "3", note="less spicy")]))
    db.session.commit()
    assert sorted((i["name"], i["qty"]) for i in k2.items) == [("Chicken Biriyani", "1"), ("Lime Soda", "-1")]
    assert k2.kot_no == k1.kot_no + 1
    assert rest.send_kot(tenant, client_admin, table, CartInput(lines=[_line(biriyani, "3", note="less spicy")])) is None
    bill = checkout(tenant, client_admin, CartInput(lines=[_line(biriyani, "3", note="less spicy")], order_type="dine_in",
                                                   table_id=table.id), PaymentMode.CASH)
    db.session.commit()
    assert bill.table_id == table.id and bill.lines[0].note == "less spicy"
    assert {k.status for k in KitchenTicket.query.filter_by(tenant_id=tenant.id)} == {KotStatus.BILLED}
    assert rest.running_order(tenant.id, table.id) is None


def test_move_merge_and_cancel(db, tenant, client_admin, menu):
    _on(db, tenant, "restaurant_mode")
    t1, t2, t3 = (DiningTable(tenant_id=tenant.id, name=n) for n in ("T1", "T2", "T3"))
    db.session.add_all([t1, t2, t3])
    db.session.commit()
    rest.send_kot(tenant, client_admin, t1, CartInput(lines=[_line(menu[0])]))
    rest.send_kot(tenant, client_admin, t2, CartInput(lines=[_line(menu[1], "2")]))
    db.session.commit()
    with pytest.raises(rest.RestaurantError):
        rest.move_table(tenant, t1, t2)
    rest.move_table(tenant, t1, t3)
    db.session.commit()
    assert rest.running_order(tenant.id, t1.id) is None and rest.running_order(tenant.id, t3.id) is not None
    rest.merge_tables(tenant, client_admin, t3, t2)
    db.session.commit()
    merged = rest.running_order(tenant.id, t2.id)
    assert sorted(l.description for l in merged.lines) == ["Chicken Biriyani", "Lime Soda"]
    assert len(rest.open_tickets(tenant.id, t2.id)) == 2
    rest.cancel_order(tenant, t2, "Guests left")
    db.session.commit()
    assert rest.running_order(tenant.id, t2.id) is None
    assert {k.status for k in KitchenTicket.query.filter_by(tenant_id=tenant.id)} == {KotStatus.CANCELLED}


def test_order_type_price_list_packing_and_service_charge(db, tenant, staff, menu):
    client_admin = staff  # a counter login: prices come from the item master / lists
    _on(db, tenant, "restaurant_mode")
    biriyani, soda = menu
    swiggy = PriceList(tenant_id=tenant.id, name="Swiggy")
    db.session.add(swiggy)
    db.session.flush()
    db.session.add(PriceListItem(tenant_id=tenant.id, price_list_id=swiggy.id, product_id=biriyani.id, price=D("240")))
    save_module_settings(tenant, "restaurant", {"price_lists": {"swiggy": swiggy.id}, "packing_charge": "10",
                                                "packing_per": "item", "service_charge_percent": "10"})
    db.session.commit()
    bill = checkout(tenant, client_admin, CartInput(lines=[_line(biriyani, "2"), _line(soda)], order_type="swiggy"),
                    PaymentMode.UPI)
    db.session.commit()
    rates = {l.description: l.rate for l in bill.lines}
    assert rates["Chicken Biriyani"] == D("240") and rates["Packing charge"] == D("10")
    packing = next(l for l in bill.lines if l.charge_kind == "packing")
    assert packing.qty == D("3") and packing.gst_rate == gst_config.RESTAURANT_SERVICE_GST_RATE
    assert packing.cgst_amount == packing.sgst_amount == D("0.75")
    # Dine-in: no packing; service charge only when ticked, 10% of the food.
    plain = checkout(tenant, client_admin, CartInput(lines=[_line(biriyani)], order_type="dine_in"), PaymentMode.CASH)
    assert not any(l.charge_kind for l in plain.lines)
    ticked = checkout(tenant, client_admin, CartInput(lines=[_line(biriyani)], order_type="dine_in", service_charge=True),
                      PaymentMode.CASH)
    service = next(l for l in ticked.lines if l.charge_kind == "service")
    assert service.taxable_value == D("20.00") and ticked.grand_total == D("231")


def test_sold_out_today_is_refused_and_returns_tomorrow(db, tenant, client_admin, menu):
    _on(db, tenant, "restaurant_mode")
    menu[0].sold_out_on = TODAY
    db.session.commit()
    with pytest.raises(POSValidationError, match="sold out"):
        checkout(tenant, client_admin, CartInput(lines=[_line(menu[0])]), PaymentMode.CASH)
    menu[0].sold_out_on = TODAY - timedelta(days=1)
    db.session.commit()
    assert checkout(tenant, client_admin, CartInput(lines=[_line(menu[0])]), PaymentMode.CASH).bill_number


def test_takeaway_checkout_prints_a_kot(client, db, tenant, client_admin, menu):
    _on(db, tenant, "restaurant_mode")
    _login(client, client_admin.email, "ClientSecret123")
    r = client.post("/pos/checkout", headers={"X-Requested-With": "XMLHttpRequest"}, data={
        "payment_mode": "cash", "order_type": "takeaway", "line_product_id[]": [menu[0].id],
        "line_description[]": [menu[0].name], "line_hsn[]": ["996331"], "line_qty[]": ["1"], "line_rate[]": ["200"],
        "line_gst_rate[]": ["5"], "line_unit[]": ["NOS"], "line_note[]": ["extra raita"]})
    data = r.get_json()
    assert r.status_code == 200 and data["kot_pdf"].endswith(".pdf")
    assert client.get(data["kot_pdf"]).data.startswith(b"%PDF")
    kot = KitchenTicket.query.filter_by(tenant_id=tenant.id).one()
    assert kot.status == KotStatus.BILLED and kot.items[0]["note"] == "extra raita"


def test_quote_endpoint_prices_like_checkout(client, db, tenant, client_admin, menu):
    _on(db, tenant, "restaurant_mode")
    save_module_settings(tenant, "restaurant", {"packing_charge": "15"})
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    r = client.post("/pos/quote", data={"order_type": "delivery", "line_product_id[]": [menu[0].id],
                                        "line_description[]": [menu[0].name], "line_hsn[]": ["996331"],
                                        "line_qty[]": ["1"], "line_rate[]": ["1"], "line_gst_rate[]": ["5"],
                                        "line_unit[]": ["NOS"]}).get_json()
    assert r["lines"][0]["rate"] == 200.0  # the item master's price, not the 1 posted
    assert r["charges"] == [{"description": "Packing charge", "qty": 1.0, "rate": 15.0, "gst_rate": 5.0}]


# --- pharmacy --------------------------------------------------------------------------------

def test_schedule_h1_needs_prescription_and_lands_in_register(db, tenant, client_admin, client):
    _on(db, tenant, "pharmacy")
    drug = _product(db, tenant, "Alprazolam 0.25", "30", rate="12", hsn="3004", drug_schedule="H1")
    with pytest.raises(POSValidationError, match="doctor"):
        checkout(tenant, client_admin, CartInput(lines=[_line(drug)]), PaymentMode.CASH)
    with pytest.raises(POSValidationError, match="register"):
        checkout(tenant, client_admin, CartInput(lines=[_line(drug)], prescription={"doctor": "Dr Rao", "patient": "Anu"}),
                 PaymentMode.CASH)
    bill = checkout(tenant, client_admin, CartInput(lines=[_line(drug)], prescription={
        "doctor": "Dr Rao", "doctor_reg_no": "TN-12345", "patient": "Anu", "patient_address": "4 Lake Rd"}), PaymentMode.CASH)
    db.session.commit()
    assert bill.prescription["doctor_reg_no"] == "TN-12345" and bill.prescription["schedules"] == ["H1"]
    _login(client, client_admin.email, "ClientSecret123")
    page = client.get("/pharmacy/register").data
    assert b"Alprazolam" in page and b"TN-12345" in page
    assert client.get("/pharmacy/register.xlsx").status_code == 200


def test_selling_by_the_strip_moves_tablets(db, tenant, client_admin, staff):
    _on(db, tenant, "pharmacy", "inventory")
    tab = _product(db, tenant, "Paracetamol 500", "2", rate="12", unit="TBS", hsn="3004", alt_unit="STR",
                   alt_unit_factor=D("10"))
    _stock(db, tenant, client_admin, tab, "100")
    bill = checkout(tenant, staff, CartInput(lines=[_line(tab, "2", alt=True)]), PaymentMode.CASH)
    db.session.commit()
    line = bill.lines[0]
    assert line.unit == "STR" and line.unit_factor == D("10") and line.rate == D("20")
    assert _on_hand(tenant, tab) == D("80")
    void_bill(bill, client_admin, "wrong")
    db.session.commit()
    assert _on_hand(tenant, tab) == D("100")


def test_substitutes_by_composition(client, db, tenant, client_admin):
    _on(db, tenant, "pharmacy")
    a = _product(db, tenant, "Crocin 500", "25", hsn="3004", composition="Paracetamol 500mg")
    b = _product(db, tenant, "Dolo 500", "20", hsn="3004", composition="paracetamol  500 MG")
    _product(db, tenant, "Brufen", "30", hsn="3004", composition="Ibuprofen 400mg")
    _login(client, client_admin.email, "ClientSecret123")
    items = client.get(f"/pharmacy/substitutes/{a.id}").get_json()["items"]
    assert [i["id"] for i in items] == [b.id]


def test_pharmacy_purchase_needs_expiry(db, tenant, client_admin):
    from app.models.purchase import Supplier
    from app.purchases import services as ps

    _on(db, tenant, "pharmacy", "purchases", "batch_expiry", "inventory")
    drug = _product(db, tenant, "Azithral 500", "100", rate="12", hsn="3004", track_batch=True)
    sup = Supplier(tenant_id=tenant.id, name="Medi Dist", state_code="27", state_name="Maharashtra")
    db.session.add(sup)
    db.session.commit()
    with pytest.raises(ps.PurchaseError, match="expiry"):
        ps.create_purchase_bill(tenant, client_admin, ps.PurchaseInput(
            supplier_id=sup.id, gstin_id=tenant.gstins.first().id, supplier_invoice_no="M1", supplier_invoice_date=TODAY,
            location_id=ledger.default_location(tenant.id).id,
            lines=[ps.PurchaseLineInput(product_id=drug.id, description=drug.name, hsn_or_sac_code="3004", qty=D("10"),
                                        unit="NOS", rate=D("60"), gst_rate=D("12"), batch_no="B1")]))


# --- electronics -------------------------------------------------------------------------------

def test_warranty_runs_from_the_sale(db, tenant, client_admin, client):
    _on(db, tenant, "electronics", "serial_imei", "inventory")
    phone = _product(db, tenant, "Redmi 13", "12000", rate="18", hsn="85171300", track_serial=True, warranty_months=12)
    db.session.add(ProductSerial(tenant_id=tenant.id, product_id=phone.id, serial_no="356789012345678",
                                 location_id=ledger.default_location(tenant.id).id, warranty_end=date(2020, 1, 1)))
    db.session.commit()
    with pytest.raises(POSValidationError, match="serial"):
        checkout(tenant, client_admin, CartInput(lines=[_line(phone)]), PaymentMode.CASH)
    checkout(tenant, client_admin, CartInput(lines=[_line(phone, serials=["356789012345678"])]), PaymentMode.CASH)
    db.session.commit()
    unit = ProductSerial.query.filter_by(serial_no="356789012345678").one()
    assert unit.status == SerialStatus.SOLD and unit.warranty_end > TODAY + timedelta(days=360)
    _login(client, client_admin.email, "ClientSecret123")
    assert b"In warranty" in client.get("/jobs/warranty?q=356789012345678").data


# --- jewellery at the counter --------------------------------------------------------------------

@pytest.fixture()
def ring(db, tenant):
    return _product(db, tenant, "Ring 22K", "0", rate="3", hsn="7113", metal="gold", purity="22K", gross_weight=D("5.2"),
                    net_weight=D("5"), making_charge_kind="per_gram", making_charge_value=D("400"), stone_value=D("500"))


def test_jewellery_sale_from_todays_rate(db, tenant, client_admin, ring):
    _on(db, tenant, "jewellery")
    with pytest.raises(POSValidationError, match="today's gold 22K rate"):
        checkout(tenant, client_admin, CartInput(lines=[_line(ring)]), PaymentMode.CASH)
    jewel.set_rate(tenant, client_admin, "gold", "24K", "7000", on=TODAY - timedelta(days=1))
    jewel.set_rate(tenant, client_admin, "gold", "22K", "6500")
    db.session.commit()
    bill = checkout(tenant, client_admin, CartInput(lines=[_line(ring)]), PaymentMode.CASH)
    line = bill.lines[0]
    assert line.rate == D("35000.00") and not line.price_includes_tax  # 5 x 6500 + 5 x 400 + 500
    assert line.cgst_amount == line.sgst_amount == D("525.00")  # 1.5% + 1.5%
    assert line.jewellery["metal_value"] == "32500.00"


def test_old_gold_pays_after_gst_and_void_releases(db, tenant, client_admin, ring):
    _on(db, tenant, "jewellery")
    jewel.set_rate(tenant, client_admin, "gold", "22K", "6500")
    db.session.commit()
    voucher = jewel.create_old_gold(tenant, client_admin, {"seller_name": "Lakshmi", "metal": "gold", "purity": "22K",
                                                           "gross_weight": "3", "net_weight": "2.8", "rate_per_gram": "6000"})
    db.session.commit()
    assert voucher.number.startswith("OG/") and voucher.value == D("16800.00")
    bill = checkout(tenant, client_admin, CartInput(lines=[_line(ring)]), [
        PaymentInput(PaymentMode.OLD_GOLD, D("16800"), voucher.number),
        PaymentInput(PaymentMode.CASH, D("36050") - D("16800"))])
    db.session.commit()
    # GST stays on the ring's full value - old gold is a payment.
    assert bill.total_taxable_value == D("35000.00") and bill.grand_total == D("36050")
    assert voucher.status == OldGoldStatus.USED and voucher.balance == 0
    with pytest.raises(POSValidationError, match="Only Rs. 0.00"):
        checkout(tenant, client_admin, CartInput(lines=[_line(ring)]), [
            PaymentInput(PaymentMode.OLD_GOLD, D("100"), voucher.number), PaymentInput(PaymentMode.CASH, D("35950"))])
    db.session.rollback()
    with pytest.raises(JewelleryError):
        jewel.void_old_gold(voucher, client_admin, "mistake")
    void_bill(bill, client_admin, "wrong ring")
    db.session.commit()
    assert voucher.status == OldGoldStatus.OPEN and voucher.balance == D("16800.00")
    jewel.void_old_gold(voucher, client_admin, "entered twice")
    db.session.commit()
    assert voucher.status == OldGoldStatus.VOID


def test_job_work_item_is_five_percent_service(client, db, tenant, client_admin):
    _on(db, tenant, "jewellery")
    _login(client, client_admin.email, "ClientSecret123")
    client.post("/jewellery/job-work-item")
    item = Product.query.filter_by(tenant_id=tenant.id, hsn_or_sac_code=gst_config.JEWELLERY_JOB_WORK_SAC).one()
    assert item.is_service and item.gst_rate == gst_config.JEWELLERY_JOB_WORK_GST_RATE == D("5")


# --- job cards --------------------------------------------------------------------------------------

def test_job_card_parts_labour_invoice_counts_stock_once(db, tenant, client_admin):
    _on(db, tenant, "job_cards", "inventory")
    screen = _product(db, tenant, "A35 display", "3000", rate="18", hsn="85177990")
    _stock(db, tenant, client_admin, screen, "5", cost="2000")
    job = jobs.create_job(tenant, client_admin, {"customer_name": "Kumar", "customer_phone": "9876500000",
                                                 "item": "Samsung A35", "complaint": "Cracked screen", "estimate": "3800"})
    db.session.commit()
    assert job.number.startswith("JOB/") and job.status == JobStatus.RECEIVED
    jobs.add_part(tenant, client_admin, job, screen.id, "1")
    jobs.add_labour(job, "Display fitting", "500", "18", "998719")
    db.session.commit()
    assert _on_hand(tenant, screen) == D("4")
    with pytest.raises(jobs.JobError):
        jobs.set_status(job, JobStatus.DELIVERED)
    invoice = jobs.make_invoice(tenant, client_admin, job)
    db.session.commit()
    assert _on_hand(tenant, screen) == D("4")  # the invoice took it, the job's own entry was undone
    assert invoice.total_taxable_value == D("3500.00") and job.status == JobStatus.READY
    assert "ready for pickup" in jobs.ready_message(tenant, job)
    jobs.set_status(job, JobStatus.DELIVERED)
    db.session.commit()
    with pytest.raises(jobs.JobError):
        jobs.cancel(job, client_admin, "x")


def test_cancelled_job_puts_parts_back(db, tenant, client_admin):
    _on(db, tenant, "job_cards", "inventory")
    part = _product(db, tenant, "Battery", "900", rate="18", hsn="85076000")
    _stock(db, tenant, client_admin, part, "3")
    job = jobs.create_job(tenant, client_admin, {"customer_name": "Asha", "item": "Phone", "complaint": "Battery"})
    line = jobs.add_part(tenant, client_admin, job, part.id, "2")
    db.session.commit()
    assert _on_hand(tenant, part) == D("1")
    jobs.remove_line(job, line, client_admin)
    jobs.add_part(tenant, client_admin, job, part.id, "1")
    db.session.commit()
    assert _on_hand(tenant, part) == D("2")
    jobs.cancel(job, client_admin, "Customer took it back")
    db.session.commit()
    assert _on_hand(tenant, part) == D("3") and job.status == JobStatus.CANCELLED


# --- wholesale -------------------------------------------------------------------------------------

def test_group_list_and_slabs(db, tenant, client_admin, staff):
    _on(db, tenant, "wholesale")
    soap = _product(db, tenant, "Soap", "40", rate="18", hsn="3401")
    dealers = PriceList(tenant_id=tenant.id, name="Dealers")
    db.session.add(dealers)
    db.session.flush()
    db.session.add(PriceListItem(tenant_id=tenant.id, price_list_id=dealers.id, product_id=soap.id, price=D("34")))
    group = CustomerGroup(tenant_id=tenant.id, name="Dealers", price_list_id=dealers.id)
    db.session.add(group)
    db.session.flush()
    dealer = Customer(tenant_id=tenant.id, name="Raj Agencies", customer_group_id=group.id)
    walkin = Customer(tenant_id=tenant.id, name="Walk-in")
    db.session.add_all([dealer, walkin, PriceSlab(tenant_id=tenant.id, product_id=soap.id, min_qty=D("12"), price=D("36")),
                        PriceSlab(tenant_id=tenant.id, product_id=soap.id, min_qty=D("48"), price=D("33"))])
    db.session.commit()
    assert unit_price(tenant.id, soap, D("1"), walkin) == D("40")
    assert unit_price(tenant.id, soap, D("12"), walkin) == D("36")
    assert unit_price(tenant.id, soap, D("12"), dealer) == D("34")  # a slab never raises the price
    assert unit_price(tenant.id, soap, D("50"), dealer) == D("33")
    sm = Salesman(tenant_id=tenant.id, name="Vijay")
    db.session.add(sm)
    db.session.commit()
    bill = checkout(tenant, staff, CartInput(lines=[_line(soap, "50")], customer_id=dealer.id, salesman_id=sm.id),
                    PaymentMode.CASH)
    db.session.commit()
    assert bill.lines[0].rate == D("33") and bill.salesman_id == sm.id
    from app.wholesale.report import salesman_report

    row = salesman_report(tenant, TODAY, TODAY)[0]
    assert row["salesman"].name == "Vijay" and row["bills"] == 1 and row["taxable"] == bill.total_taxable_value


def test_invoice_records_salesman_and_price_suggestion(client, db, tenant, client_admin):
    _on(db, tenant, "wholesale")
    soap = _product(db, tenant, "Soap", "40", rate="18", hsn="3401")
    db.session.add(PriceSlab(tenant_id=tenant.id, product_id=soap.id, min_qty=D("10"), price=D("35")))
    sm = Salesman(tenant_id=tenant.id, name="Vijay")
    c = Customer(tenant_id=tenant.id, name="Shop", state_code="27", state_name="Maharashtra")
    db.session.add_all([sm, c])
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    assert client.get(f"/wholesale/price?product_id={soap.id}&qty=10").get_json()["rate"] == 35.0
    client.post("/invoices/new", data={
        "gstin_id": tenant.gstins.first().id, "document_type": "tax_invoice", "bill_to": "customer", "customer_id": c.id,
        "place_of_supply_state_code": "27", "invoice_date": TODAY.isoformat(), "salesman_id": sm.id,
        "line_description[]": ["Soap"], "line_hsn[]": ["3401"], "line_qty[]": ["10"], "line_rate[]": ["35"],
        "line_discount[]": ["0"], "line_gst_rate[]": ["18"], "line_unit[]": ["NOS"], "line_product_id[]": [soap.id]})
    from app.models.invoice import Invoice

    assert Invoice.query.filter_by(tenant_id=tenant.id).one().salesman_id == sm.id


# --- loyalty ------------------------------------------------------------------------------------------

def test_points_earned_spent_and_reversed(db, tenant, client_admin):
    _on(db, tenant, "loyalty")
    save_module_settings(tenant, "loyalty", {"points_per_100": "2", "point_value": "0.5", "min_redeem": 10})
    item = _product(db, tenant, "Kurta", "1000", rate="5", hsn="6211", price_includes_tax=True)
    meena = Customer(tenant_id=tenant.id, name="Meena")
    db.session.add(meena)
    db.session.commit()
    checkout(tenant, client_admin, CartInput(lines=[_line(item)], customer_id=meena.id), PaymentMode.CASH)
    db.session.commit()
    assert points.balance(tenant.id, meena.id) == 20
    with pytest.raises(POSValidationError, match="needs 40"):
        checkout(tenant, client_admin, CartInput(lines=[_line(item)], customer_id=meena.id),
                 [PaymentInput(PaymentMode.LOYALTY, D("20")), PaymentInput(PaymentMode.CASH, D("980"))])
    db.session.rollback()
    bill = checkout(tenant, client_admin, CartInput(lines=[_line(item)], customer_id=meena.id),
                    [PaymentInput(PaymentMode.LOYALTY, D("10")), PaymentInput(PaymentMode.CASH, D("990"))])
    db.session.commit()
    assert bill.total_taxable_value == D("952.38")  # GST on the full price - points are a payment
    assert points.balance(tenant.id, meena.id) == 20 - 20 + 19  # spent 20, earned 2 per 100 on Rs. 990
    void_bill(bill, client_admin, "mistake")
    db.session.commit()
    assert points.balance(tenant.id, meena.id) == 20
    with pytest.raises(POSValidationError, match="customer"):
        checkout(tenant, client_admin, CartInput(lines=[_line(item)]),
                 [PaymentInput(PaymentMode.LOYALTY, D("10")), PaymentInput(PaymentMode.CASH, D("990"))])


def test_customer_history_page(client, db, tenant, client_admin):
    _on(db, tenant, "loyalty")
    item = _product(db, tenant, "Kurta", "1000", rate="5", hsn="6211")
    meena = Customer(tenant_id=tenant.id, name="Meena")
    db.session.add(meena)
    db.session.commit()
    checkout(tenant, client_admin, CartInput(lines=[_line(item)], customer_id=meena.id), PaymentMode.CASH)
    db.session.commit()
    _login(client, client_admin.email, "ClientSecret123")
    page = client.get(f"/loyalty/customers/{meena.id}").data
    assert b"Kurta" in page and b"Earned on" in page


# --- flags, roles, zero change ---------------------------------------------------------------------------

ADMIN_ROUTES = ["/restaurant/setup", "/wholesale/", "/wholesale/salesmen-report", "/pharmacy/register",
                "/loyalty/", "/settings/modules"]
USER_ROUTES = ["/restaurant/", "/restaurant/menu", "/jobs/", "/jobs/new", "/jobs/warranty", "/jewellery/rates",
               "/jewellery/old-gold", "/jewellery/old-gold/new"]


def test_flags_off_404_and_no_trace(client, db, tenant, client_admin, menu):
    _login(client, client_admin.email, "ClientSecret123")
    for url in ADMIN_ROUTES + USER_ROUTES + ["/pos/scale-scan?code=1", f"/pharmacy/substitutes/{menu[0].id}"]:
        assert client.get(url).status_code == 404, url
    page = client.get("/pos/").data
    assert b'const MODULES = {"quote": false};' in page  # no module reaches the counter
    nav = client.get("/invoices/").data
    for word in (b">Tables<", b">Jobs<", b">Rates<"):
        assert word not in nav


def test_existing_flags_unchanged_and_checkout_identical(db, tenant, client_admin, menu):
    """With every Phase 9 flag off, a bill is exactly what it was."""
    bill = checkout(tenant, client_admin, CartInput(lines=[_line(menu[0], "2", note="ignored? no - kept")],
                                                   order_type="swiggy", service_charge=True), PaymentMode.CASH)
    assert [l.charge_kind for l in bill.lines] == [None] and bill.grand_total == D("420")


def test_staff_forbidden_on_admin_pages(client, db, tenant, staff):
    for f in ("restaurant_mode", "wholesale", "pharmacy", "loyalty", "weighing", "job_cards", "jewellery", "electronics"):
        tenant.set_feature(f, True)
    db.session.commit()
    _login(client, staff.email, "CashierSecret123")
    for url in ADMIN_ROUTES:
        assert client.get(url).status_code == 403, url
    for url in USER_ROUTES:
        assert client.get(url).status_code == 200, url


# --- tenant isolation for every new table ------------------------------------------------------------------

@pytest.fixture()
def other(db, super_admin):
    t = Tenant(legal_name="Other Co", registration_type=RegistrationType.REGULAR, onboarded_by_id=super_admin.id,
               billing_cycle=BillingCycle.MONTHLY)
    db.session.add(t)
    db.session.flush()
    db.session.add(Gstin(tenant_id=t.id, gstin="29BBBBB1111B1Z5", state_code="29", state_name="Karnataka", is_primary=True))
    u = User(tenant_id=t.id, name="Other Admin", email="admin@other.example.com", role=UserRole.CLIENT_ADMIN,
             must_change_password=False)
    u.set_password("OtherSecret123")
    db.session.add(u)
    for f in ("restaurant_mode", "wholesale", "pharmacy", "loyalty", "job_cards", "jewellery", "electronics", "weighing"):
        t.set_feature(f, True)
    db.session.commit()
    return t, u


def test_tenant_isolation(client, db, tenant, client_admin, menu, ring, other):
    for f in ("restaurant_mode", "wholesale", "pharmacy", "loyalty", "job_cards", "jewellery", "electronics"):
        tenant.set_feature(f, True)
    table = DiningTable(tenant_id=tenant.id, name="T1")
    sm = Salesman(tenant_id=tenant.id, name="Vijay")
    group = CustomerGroup(tenant_id=tenant.id, name="Dealers")
    db.session.add_all([table, sm, group])
    db.session.commit()
    rest.send_kot(tenant, client_admin, table, CartInput(lines=[_line(menu[0])]))
    kot = KitchenTicket.query.filter_by(tenant_id=tenant.id).one()
    slab = PriceSlab(tenant_id=tenant.id, product_id=menu[0].id, min_qty=D("5"), price=D("150"))
    db.session.add(slab)
    jewel.set_rate(tenant, client_admin, "gold", "22K", "6500")
    voucher = jewel.create_old_gold(tenant, client_admin, {"seller_name": "L", "metal": "gold", "purity": "22K",
                                                           "gross_weight": "1", "net_weight": "1", "rate_per_gram": "6000"})
    job = jobs.create_job(tenant, client_admin, {"customer_name": "Kumar", "item": "Phone", "complaint": "x"})
    cust = Customer(tenant_id=tenant.id, name="Meena")
    db.session.add(cust)
    db.session.flush()
    db.session.add(LoyaltyEntry(tenant_id=tenant.id, customer_id=cust.id, points=50, kind="adjust"))
    db.session.commit()

    other_tenant, other_admin = other
    _login(client, other_admin.email, "OtherSecret123")
    for url in (f"/restaurant/kot/{kot.id}.pdf", f"/jobs/{job.id}", f"/jewellery/old-gold/{voucher.id}",
                f"/jewellery/old-gold/{voucher.id}.pdf", f"/wholesale/slabs/{menu[0].id}", f"/loyalty/customers/{cust.id}",
                f"/pharmacy/substitutes/{menu[0].id}", f"/wholesale/price?product_id={menu[0].id}",
                f"/pos/?table={table.id}"):
        assert client.get(url).status_code == 404, url
    assert client.get(f"/jewellery/old-gold/lookup?number={voucher.number}").status_code == 404
    for url, data in ((f"/restaurant/tables/{table.id}/kot", {}), (f"/restaurant/tables/{table.id}/cancel", {"reason": "x"}),
                      (f"/restaurant/tables/{table.id}/move", {"to_table_id": table.id}),
                      (f"/jobs/{job.id}", {"action": "cancel", "reason": "x"}),
                      (f"/jewellery/old-gold/{voucher.id}/void", {"reason": "x"}),
                      (f"/wholesale/slabs/{menu[0].id}", {"action": "remove", "slab_id": slab.id})):
        assert client.post(url, data=data).status_code == 404, url
    # Their counter can't use our salesman, table, voucher, or our customer's points / group.
    their_item = Product(tenant_id=other_tenant.id, name="Tea", hsn_or_sac_code="0902", gst_rate=D("5"), sale_price=D("20"))
    db.session.add(their_item)
    db.session.commit()
    with pytest.raises(POSValidationError):
        checkout(other_tenant, other_admin, CartInput(lines=[_line(their_item)], salesman_id=sm.id), PaymentMode.CASH)
    db.session.rollback()
    with pytest.raises(POSValidationError):
        checkout(other_tenant, other_admin, CartInput(lines=[_line(their_item)]),
                 [PaymentInput(PaymentMode.OLD_GOLD, D("21"), voucher.number)])
    db.session.rollback()
    with pytest.raises(POSValidationError):
        checkout(other_tenant, other_admin, CartInput(lines=[_line(their_item)], customer_id=cust.id),
                 [PaymentInput(PaymentMode.LOYALTY, D("21"))])
    db.session.rollback()
    r = client.post("/pos/checkout", headers={"X-Requested-With": "XMLHttpRequest"}, data={
        "payment_mode": "cash", "order_type": "dine_in", "table_id": table.id, "line_product_id[]": [their_item.id],
        "line_description[]": ["Tea"], "line_hsn[]": ["0902"], "line_qty[]": ["1"], "line_rate[]": ["20"],
        "line_gst_rate[]": ["5"], "line_unit[]": ["NOS"]})
    assert r.status_code == 400 and "table" in r.get_json()["error"]
    # Our metal rate doesn't price their jewellery.
    from app.utils.jewellery import todays_rate

    assert todays_rate(other_tenant.id, "gold", "22K") is None
    # Lists and pages show none of ours.
    for url, word in (("/restaurant/", b"T1"), ("/jobs/?status=all", job.number.encode()),
                      ("/jewellery/old-gold", voucher.number.encode()), ("/wholesale/", b"Vijay"), ("/loyalty/", b"Meena")):
        assert word not in client.get(url).data, url
    db.session.refresh(job)
    db.session.refresh(voucher)
    assert job.status == JobStatus.RECEIVED and voucher.status == OldGoldStatus.OPEN
    assert MetalRate.query.filter_by(tenant_id=other_tenant.id).count() == 0
    assert OldGoldPurchase.query.filter_by(tenant_id=tenant.id).count() == 1 and JobCard.query.count() == 1
