"""Rupee discounts, bill-discount allocation and sales-return amounts in
the one GST service (app.utils.gst), plus the JS preview's parity."""

import json
import shutil
import subprocess
from decimal import Decimal as D
from pathlib import Path

import pytest

from app.utils.gst import (
    allocate_amount,
    compute_invoice_totals,
    compute_line,
    line_gross,
    price_basis_amount,
    return_line_amounts,
)


def _line(qty, rate, pct="0", gst="18", incl=False, intra=True, charge=True, flat="0"):
    return compute_line(
        D(qty), D(rate), D(pct), D(gst), is_intra_state=intra, charge_gst=charge,
        price_includes_tax=incl, discount_flat=D(flat),
    )


# --- rupee (flat) discounts -------------------------------------------------

def test_flat_discount_on_exclusive_price_comes_off_the_pre_tax_value():
    r = _line("2", "100", flat="20")
    assert r["discount_amount"] == D("20.00")
    assert r["taxable_value"] == D("180.00")
    assert (r["cgst_amount"], r["sgst_amount"]) == (D("16.20"), D("16.20"))
    assert r["line_total"] == D("212.40")


def test_flat_discount_on_inclusive_price_comes_off_what_the_customer_pays():
    r = _line("1", "118", flat="18", incl=True)
    assert r["line_total"] == D("100.00")
    assert r["taxable_value"] == D("84.75")
    assert r["cgst_amount"] + r["sgst_amount"] == D("15.25")
    # reported pre-tax: 100.00 (taxable of 118) - 84.75
    assert r["discount_amount"] == D("15.25")


def test_percent_then_flat():
    r = _line("1", "200", pct="10", flat="30", gst="5")
    assert r["discount_amount"] == D("50.00")
    assert r["taxable_value"] == D("150.00")
    assert r["line_total"] == D("157.50")


def test_flat_discount_inter_state_is_igst():
    r = _line("1", "100", flat="10", intra=False)
    assert (r["cgst_amount"], r["sgst_amount"], r["igst_amount"]) == (D("0.00"), D("0.00"), D("16.20"))


def test_flat_discount_at_zero_rate_and_for_a_composition_client():
    assert _line("1", "100", gst="0", flat="10")["line_total"] == D("90.00")
    r = _line("1", "100", gst="18", flat="10", charge=False)
    assert r["line_total"] == D("90.00") and r["cgst_amount"] == D("0.00")


def test_no_flat_discount_leaves_the_old_results_untouched():
    assert _line("3", "33.33", pct="5") == compute_line(D("3"), D("33.33"), D("5"), D("18"), is_intra_state=True)


def test_price_basis_turns_rupees_off_the_total_into_rupees_off_the_price():
    assert price_basis_amount(D("118"), D("18"), price_includes_tax=False) == D("100.00")
    assert price_basis_amount(D("50"), D("18"), price_includes_tax=True) == D("50.00")
    assert price_basis_amount(D("50"), D("18"), price_includes_tax=False, charge_gst=False) == D("50.00")
    assert price_basis_amount(D("50"), D("0"), price_includes_tax=False) == D("50.00")
    # so the customer's total drops by (about) the rupees asked for
    flat = price_basis_amount(D("50"), D("18"), price_includes_tax=False)
    before = _line("1", "500")["line_total"]
    after = _line("1", "500", flat=str(flat))["line_total"]
    # within a paisa: the tax is re-rounded on the lower value
    assert abs((before - after) - D("50.00")) <= D("0.01")


def test_line_gross():
    assert line_gross(D("0.750"), D("80")) == D("60.00")
    assert line_gross(D("3"), D("33.335")) == D("100.01")


# --- bill discount allocation -----------------------------------------------

@pytest.mark.parametrize(
    "weights,amount",
    [
        (["100", "200", "300"], "60"),
        (["33.33", "33.33", "33.34"], "10"),
        (["1", "1", "1"], "0.10"),
        (["999.99"], "12.34"),
        (["10", "0", "5"], "3"),
    ],
)
def test_allocation_always_adds_back_exactly(weights, amount):
    parts = allocate_amount([D(w) for w in weights], D(amount))
    assert sum(parts, D("0")) == D(amount)
    assert all(p >= 0 for p in parts)


def test_allocation_is_proportional_and_puts_the_remainder_on_the_biggest_line():
    assert allocate_amount([D("100"), D("200"), D("300")], D("60")) == [D("10.00"), D("20.00"), D("30.00")]
    assert allocate_amount([D("1"), D("1"), D("2")], D("0.10")) == [D("0.03"), D("0.03"), D("0.04")]
    assert allocate_amount([D("1"), D("1"), D("1")], D("0.10")) == [D("0.04"), D("0.03"), D("0.03")]


def test_allocation_of_nothing_or_over_nothing():
    assert allocate_amount([D("10"), D("20")], D("0")) == [D("0.00"), D("0.00")]
    assert allocate_amount([D("0"), D("0")], D("5")) == [D("0.00"), D("0.00")]
    assert allocate_amount([], D("5")) == []


# --- sales returns (credit-note equivalent) ---------------------------------

def _sold(qty, rate, pct="0", flat="0", gst="18", incl=False):
    amounts = _line(qty, rate, pct=pct, gst=gst, incl=incl, flat=flat)
    return {
        "rate": D(rate), "discount_percent": D(pct), "discount_flat": D(flat), "gst_rate": D(gst),
        "price_includes_tax": incl, "is_intra_state": True, **amounts,
    }


def _sum(*dicts):
    keys = ("discount_amount", "taxable_value", "cgst_amount", "sgst_amount", "igst_amount", "line_total")
    return {k: sum((d[k] for d in dicts), D("0")) for k in keys}


def test_full_return_gives_back_exactly_the_line():
    sold = _sold("2", "118", incl=True)
    back = return_line_amounts(sold, D("2"), {}, D("0"), D("2"))
    assert back["line_total"] == sold["line_total"] == D("236.00")
    assert back["cgst_amount"] == sold["cgst_amount"]


@pytest.mark.parametrize(
    "qty,rate,pct,flat,gst,incl,pieces",
    [
        ("3", "33.33", "0", "0", "18", False, ["1", "1", "1"]),
        ("3", "10", "33.33", "0", "18", False, ["1", "2"]),
        ("7", "49.99", "0", "13", "5", True, ["2", "2", "3"]),
        ("5", "99", "10", "7.77", "28", True, ["1", "1", "1", "1", "1"]),
        ("2.5", "80", "0", "0", "0", False, ["0.75", "1.75"]),
        ("4", "250", "0", "100", "12", False, ["3", "1"]),
    ],
)
def test_returns_in_pieces_add_up_to_the_original_to_the_paisa(qty, rate, pct, flat, gst, incl, pieces):
    sold = _sold(qty, rate, pct=pct, flat=flat, gst=gst, incl=incl)
    done, done_qty = [], D("0")
    for piece in pieces:
        part = return_line_amounts(sold, D(qty), _sum(*done) if done else {}, done_qty, D(piece))
        done.append(part)
        done_qty += D(piece)
    totals = _sum(*done)
    for key in totals:
        assert totals[key] == sold[key], key


def test_part_return_prorates_the_rupee_discount():
    sold = _sold("4", "250", flat="100")  # 1000 - 100 = 900 taxable
    part = return_line_amounts(sold, D("4"), {}, D("0"), D("1"))
    assert part["taxable_value"] == D("225.00")  # 250 - 25
    assert part["line_total"] == D("265.50")


def test_cannot_return_more_than_was_sold_or_nothing():
    sold = _sold("2", "100")
    with pytest.raises(ValueError):
        return_line_amounts(sold, D("2"), {}, D("0"), D("3"))
    with pytest.raises(ValueError):
        return_line_amounts(sold, D("2"), {}, D("1.5"), D("1"))
    with pytest.raises(ValueError):
        return_line_amounts(sold, D("2"), {}, D("0"), D("0"))


def test_return_of_a_composition_bill_line_carries_no_tax():
    sold = _sold("2", "100", gst="0")
    part = return_line_amounts(sold, D("2"), {}, D("0"), D("1"))
    assert part["line_total"] == D("100.00") and part["cgst_amount"] == D("0.00")


def test_return_totals_round_once_like_any_document():
    sold = [_sold("1", "99.99", gst="18"), _sold("1", "0.50", gst="5")]
    parts = [return_line_amounts(s, D("1"), {}, D("0"), D("1")) for s in sold]
    totals = compute_invoice_totals(parts)
    assert totals["grand_total"] == D("119.00")
    assert totals["round_off"] == D("0.47")  # 117.99 + 0.54


# --- JS preview parity ------------------------------------------------------

FLAT_CASES = [
    ("2", "100", "0", "18", False, True, True, "20"),
    ("1", "118", "0", "18", True, True, True, "18"),
    ("1", "200", "10", "5", False, True, True, "30"),
    ("1", "100", "0", "18", False, True, False, "10"),
    ("1", "100", "0", "18", False, False, True, "10"),
    ("3", "33.33", "5", "12", True, True, True, "0.07"),
    ("0.750", "80", "0", "5", True, True, True, "1.5"),
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_flat_discount_allocation_and_price_basis_match_python():
    js = Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "gst_calc.js"
    alloc_cases = [(["100", "200", "300"], "60"), (["1", "1", "1"], "0.10"), (["33.33", "33.33", "33.34"], "10")]
    basis_cases = [("118", "18", False, True), ("50", "18", True, True), ("50", "18", False, False), ("99.99", "28", False, True)]
    script = (
        "globalThis.window = globalThis;"
        f"require({json.dumps(str(js))});"
        f"const lines = {json.dumps(FLAT_CASES)};"
        f"const allocs = {json.dumps(alloc_cases)};"
        f"const bases = {json.dumps(basis_cases)};"
        "console.log(JSON.stringify({"
        "lines: lines.map(c => window.GstCalc.line(Number(c[0]), Number(c[1]), Number(c[2]), Number(c[3]), c[4], c[5], c[6], Number(c[7]))),"
        "allocs: allocs.map(a => window.GstCalc.allocate(a[0].map(Number), Number(a[1]))),"
        "bases: bases.map(b => window.GstCalc.priceBasis(Number(b[0]), Number(b[1]), b[2], b[3]))"
        "}));"
    )
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    q = lambda x: D(str(x)).quantize(D("0.01"))
    for case, js_result in zip(FLAT_CASES, out["lines"]):
        qty, rate, pct, gst, incl, charge, intra, flat = case
        py = _line(qty, rate, pct=pct, gst=gst, incl=incl, intra=intra, charge=charge, flat=flat)
        assert q(js_result["taxable"]) == py["taxable_value"], case
        assert q(js_result["cgst"]) == py["cgst_amount"], case
        assert q(js_result["sgst"]) == py["sgst_amount"], case
        assert q(js_result["igst"]) == py["igst_amount"], case
        assert q(js_result["total"]) == py["line_total"], case
        assert q(js_result["discount"]) == py["discount_amount"], case
    for (weights, amount), js_parts in zip(alloc_cases, out["allocs"]):
        assert [q(p) for p in js_parts] == allocate_amount([D(w) for w in weights], D(amount))
    for (payable, gst, incl, charge), js_value in zip(basis_cases, out["bases"]):
        assert q(js_value) == price_basis_amount(D(payable), D(gst), incl, charge)
