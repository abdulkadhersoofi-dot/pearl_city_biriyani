"""Rule 32(5) margin-scheme arithmetic (app/utils/margin.py)."""

from decimal import Decimal

import pytest

from app.utils.margin import MarginChunk, compute_margin, compute_margin_line, split_sale_value

D = Decimal


def test_intra_state_margin_is_tax_inclusive_and_split_cgst_sgst():
    m = compute_margin(D("12000"), D("10000"), D("18"), True)
    # margin 2000 -> taxable 2000 x 100/118 = 1694.92, GST 305.08
    assert (m["margin"], m["taxable_value"], m["tax"]) == (D("2000.00"), D("1694.92"), D("305.08"))
    assert (m["cgst_amount"], m["sgst_amount"], m["igst_amount"]) == (D("152.54"), D("152.54"), D("0.00"))
    assert m["taxable_value"] + m["tax"] == m["margin"]


def test_inter_state_margin_is_igst():
    m = compute_margin(D("12000"), D("10000"), D("18"), False)
    assert (m["cgst_amount"], m["sgst_amount"], m["igst_amount"]) == (D("0.00"), D("0.00"), D("305.08"))


def test_odd_paise_split_keeps_the_total_exact():
    m = compute_margin(D("1000.01"), D("1000"), D("18"), True)
    # margin 0.01 -> taxable 0.01 (0.008 half-up), tax 0.00
    assert m["taxable_value"] + m["tax"] == D("0.01")
    m = compute_margin(D("1001"), D("1000"), D("5"), True)
    # margin 1.00 -> taxable 0.95, tax 0.05 -> CGST 0.03 (half-up), SGST 0.02
    assert (m["taxable_value"], m["cgst_amount"], m["sgst_amount"]) == (D("0.95"), D("0.03"), D("0.02"))
    assert m["cgst_amount"] + m["sgst_amount"] == m["tax"]


@pytest.mark.parametrize("sale,purchase", [("9000", "10000"), ("10000", "10000")])
def test_zero_or_negative_margin_means_no_tax_never_negative(sale, purchase):
    m = compute_margin(D(sale), D(purchase), D("18"), True)
    assert m["margin"] == 0 and m["taxable_value"] == 0 and m["tax"] == 0
    assert m["cgst_amount"] == m["sgst_amount"] == m["igst_amount"] == 0
    assert m["raw_margin"] == D(sale) - D(purchase)


def test_zero_rate_goods_margin_is_all_taxable_with_no_tax():
    m = compute_margin(D("1500"), D("1000"), D("0"), True)
    assert (m["taxable_value"], m["tax"]) == (D("500.00"), D("0.00"))


def test_line_over_several_lots_never_lets_a_loss_cancel_tax():
    chunks = [MarginChunk(D("1"), D("5000"), D("4000")), MarginChunk(D("1"), D("5000"), D("6000"))]
    line = compute_margin_line(chunks, D("18"), True)
    # lot 1 margin 1000 is taxed; lot 2's loss of 1000 is ignored, not netted
    assert line["margin"] == D("1000.00") and line["taxable_value"] == D("847.46") and line["tax"] == D("152.54")
    assert line["line_total"] == D("10000.00") and line["remainder"] == D("9000.00")
    assert line["purchase_value"] == D("10000.00")


def test_split_sale_value_adds_back_exactly():
    parts = split_sale_value(D("100"), [D("1"), D("1"), D("1")])
    assert sum(parts) == D("100.00") and sorted(parts) == [D("33.33"), D("33.33"), D("33.34")]
