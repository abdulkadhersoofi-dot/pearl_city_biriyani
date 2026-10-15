"""ITC set-off for GSTR-3B (s.49(5), Rule 88A) - app.utils.gst_setoff."""

from decimal import Decimal

from app.utils.gst_setoff import set_off

D = Decimal


def h(igst="0", cgst="0", sgst="0"):
    return {"igst": D(igst), "cgst": D(cgst), "sgst": D(sgst)}


def test_intra_state_only_each_head_against_itself():
    r = set_off(h(cgst="900", sgst="900"), h(cgst="500", sgst="700"))
    assert r["cash"] == h(cgst="400", sgst="200")
    assert r["carry_forward"] == h()


def test_igst_credit_used_first_and_spread_to_minimise_cash():
    # IGST credit 1000 against IGST 300; 700 left goes where CGST/SGST credit can't reach.
    r = set_off(h(igst="300", cgst="500", sgst="500"), h(igst="1000", cgst="500", sgst="100"))
    assert r["utilised"]["igst"]["igst"] == D("300")
    assert r["utilised"]["igst"]["sgst"] == D("400")  # SGST's uncovered part first
    assert r["cash"] == h()
    # Rule 88A: the IGST credit is exhausted before any CGST credit is touched.
    assert r["utilised"]["igst"]["cgst"] == D("300") and r["carry_forward"] == h(cgst="300")


def test_cgst_never_pays_sgst():
    r = set_off(h(sgst="1000"), h(cgst="5000"))
    assert r["cash"]["sgst"] == D("1000") and r["carry_forward"]["cgst"] == D("5000")


def test_cgst_and_sgst_credit_can_pay_igst():
    r = set_off(h(igst="1000"), h(cgst="600", sgst="600"))
    assert r["cash"]["igst"] == 0
    assert r["utilised"]["cgst"]["igst"] + r["utilised"]["sgst"]["igst"] == D("1000")
    assert sum(r["carry_forward"].values()) == D("200")


def test_rcm_is_always_cash_even_with_spare_credit():
    r = set_off(h(cgst="100", sgst="100"), h(cgst="1000", sgst="1000"), rcm=h(cgst="90", sgst="90"))
    assert r["output_in_cash"] == h() and r["cash"] == h(cgst="90", sgst="90") and r["cash_total"] == D("180")
    assert r["carry_forward"] == h(cgst="900", sgst="900")


def test_zero_and_negative_inputs():
    assert set_off(h(), h())["cash_total"] == 0
    r = set_off(h(igst="-50"), h(igst="-10"))  # a month of net returns
    assert r["cash_total"] == 0 and r["carry_forward"] == h()


def test_paise_are_kept_exact():
    r = set_off(h(igst="100.05", cgst="10.01", sgst="10.01"), h(igst="120.03"))
    assert r["paid_by_credit"]["igst"] == D("100.05")
    assert r["cash"]["cgst"] + r["cash"]["sgst"] == D("20.02") - D("19.98")
