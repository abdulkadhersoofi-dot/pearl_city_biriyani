"""Tax-inclusive pricing, HSN validation and MRP checks in app.utils.gst -
plus a parity check that the browser-side preview (static/js/gst_calc.js)
computes exactly what the server stores."""

import json
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from app.utils.gst import compute_invoice_totals, compute_line, exceeds_mrp, hsn_error, hsn_min_digits, inclusive_price

D = Decimal


def line(qty, rate, gst, *, disc="0", intra=True, incl=True, charge=True):
    return compute_line(D(qty), D(rate), D(disc), D(gst), is_intra_state=intra, charge_gst=charge, price_includes_tax=incl)


# --- inclusive back-calculation --------------------------------------------

def test_inclusive_5_percent_backs_out_exact_tax():
    r = line("1", "105", "5")
    assert r["taxable_value"] == D("100.00")
    assert r["cgst_amount"] == D("2.50") and r["sgst_amount"] == D("2.50")
    assert r["line_total"] == D("105.00")


def test_inclusive_total_is_always_the_shelf_price_even_when_halves_differ():
    # 100 incl. 18%: taxable 84.75, tax 15.25 - an odd paisa, so CGST
    # takes the rounded half and SGST the exact remainder.
    r = line("1", "100", "18")
    assert r["taxable_value"] == D("84.75")
    assert r["cgst_amount"] + r["sgst_amount"] == D("15.25")
    assert {r["cgst_amount"], r["sgst_amount"]} == {D("7.63"), D("7.62")}
    assert r["line_total"] == D("100.00")


def test_inclusive_inter_state_is_all_igst():
    r = line("1", "100", "18", intra=False)
    assert r["igst_amount"] == D("15.25")
    assert r["cgst_amount"] == r["sgst_amount"] == D("0.00")
    assert r["line_total"] == D("100.00")


def test_inclusive_zero_rate_is_all_taxable():
    r = line("2", "50", "0")
    assert r["taxable_value"] == D("100.00")
    assert r["line_total"] == D("100.00")
    assert r["cgst_amount"] == r["sgst_amount"] == r["igst_amount"] == D("0.00")


def test_inclusive_without_gst_charged_keeps_the_full_price_taxable():
    # Composition / unregistered / Bill of Supply - no GST on the bill.
    r = line("1", "118", "18", charge=False)
    assert r["taxable_value"] == D("118.00")
    assert r["line_total"] == D("118.00")


def test_inclusive_discount_is_reported_pre_tax():
    r = line("1", "118", "18", disc="10")
    assert r["line_total"] == D("106.20")  # 118 less 10%
    assert r["taxable_value"] == D("90.00")
    assert r["discount_amount"] == D("10.00")  # pre-tax: 100.00 - 90.00
    assert r["taxable_value"] + r["discount_amount"] == D("100.00")


def test_inclusive_fractional_quantity_and_awkward_price():
    assert line("0.5", "240", "5")["line_total"] == D("120.00")
    r = line("3", "33.33", "5")
    assert r["line_total"] == D("99.99")
    assert r["taxable_value"] == D("95.23")


def test_exclusive_lines_are_unchanged():
    r = line("2", "50", "18", incl=False)
    assert r["taxable_value"] == D("100.00")
    assert r["cgst_amount"] == r["sgst_amount"] == D("9.00")
    assert r["line_total"] == D("118.00")
    # The pre-existing exclusive split - both halves rounded the same way.
    r = line("1", "10.10", "5", incl=False)
    assert r["cgst_amount"] == r["sgst_amount"]


def test_invoice_totals_over_inclusive_lines_round_once():
    lines = [line("1", "100", "18"), line("1", "49.50", "5")]
    totals = compute_invoice_totals(lines)
    exact = sum(l["line_total"] for l in lines)
    assert exact == D("149.50")
    assert totals["grand_total"] == D("150.00")
    assert totals["round_off"] == D("0.50")
    assert totals["total_taxable_value"] == lines[0]["taxable_value"] + lines[1]["taxable_value"]


# --- HSN / SAC ----------------------------------------------------------------

@pytest.mark.parametrize("code", ["0401", "2106", "210690", "22021010"])
def test_hsn_accepted_for_small_business(code):
    assert hsn_error(code, aato=None) is None


@pytest.mark.parametrize("code,fragment", [("401", "4, 6 or 8"), ("21A6", "digits only"), ("21069", "4, 6 or 8"), ("", "digits only")])
def test_hsn_rejected_shapes(code, fragment):
    assert fragment in hsn_error(code, aato=None)


def test_hsn_needs_six_digits_above_five_crore():
    assert hsn_min_digits(D("50000000")) == 4  # exactly 5 crore is not "above"
    assert hsn_min_digits(D("50000001")) == 6
    assert "at least 6" in hsn_error("2106", aato=D("60000000"))
    assert hsn_error("210690", aato=D("60000000")) is None


def test_sac_must_be_chapter_99():
    assert hsn_error("998719", aato=None, is_service=True) is None
    assert "starts with 99" in hsn_error("2106", aato=None, is_service=True)


# --- MRP ------------------------------------------------------------------------

def test_mrp_check_compares_the_tax_inclusive_price():
    assert inclusive_price(D("100"), D("18"), False) == D("118.00")
    assert exceeds_mrp(D("100"), D("18"), False, D("110")) is True
    assert exceeds_mrp(D("100"), D("18"), False, D("118")) is False
    assert exceeds_mrp(D("100"), D("18"), True, D("100")) is False
    assert exceeds_mrp(D("101"), D("18"), True, D("100")) is True
    assert exceeds_mrp(D("999"), D("18"), False, None) is False


# --- browser preview == server --------------------------------------------------

PARITY_CASES = [
    # qty, rate, discount %, gst %, inclusive, charge gst, intra-state
    ("1", "105", "0", "5", True, True, True),
    ("1", "100", "0", "18", True, True, True),
    ("1", "100", "0", "18", True, True, False),
    ("3", "33.33", "0", "5", True, True, True),
    ("1", "118", "10", "18", True, True, True),
    ("0.5", "240", "0", "5", True, True, True),
    ("1", "1.005", "0", "0", False, True, True),
    ("7", "12.35", "2.5", "12", False, True, True),
    ("2", "50", "0", "18", False, True, False),
    ("1", "118", "0", "18", True, False, True),
    ("1.25", "99.99", "7", "28", True, True, True),
    ("4", "0.10", "0", "40", True, True, True),
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_preview_matches_python_to_the_paisa():
    js = Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "gst_calc.js"
    script = (
        "globalThis.window = globalThis;"
        f"require({json.dumps(str(js))});"
        f"const cases = {json.dumps(PARITY_CASES)};"
        "console.log(JSON.stringify(cases.map(c => window.GstCalc.line("
        "Number(c[0]), Number(c[1]), Number(c[2]), Number(c[3]), c[4], c[5], c[6]))));"
    )
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    for case, js_result in zip(PARITY_CASES, out):
        qty, rate, disc, gst, incl, charge, intra = case
        py = compute_line(D(qty), D(rate), D(disc), D(gst), is_intra_state=intra, charge_gst=charge, price_includes_tax=incl)
        assert D(str(js_result["taxable"])).quantize(D("0.01")) == py["taxable_value"], case
        assert D(str(js_result["cgst"])).quantize(D("0.01")) == py["cgst_amount"], case
        assert D(str(js_result["sgst"])).quantize(D("0.01")) == py["sgst_amount"], case
        assert D(str(js_result["igst"])).quantize(D("0.01")) == py["igst_amount"], case
        assert D(str(js_result["total"])).quantize(D("0.01")) == py["line_total"], case
