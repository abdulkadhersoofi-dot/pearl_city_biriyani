from decimal import Decimal

from app.utils.gst import compute_invoice_totals, compute_line


def test_intra_state_split_cgst_sgst_evenly():
    result = compute_line(
        qty=Decimal("2"), rate=Decimal("100"), discount_percent=Decimal("0"),
        gst_rate=Decimal("18"), is_intra_state=True,
    )
    assert result["taxable_value"] == Decimal("200.00")
    assert result["cgst_amount"] == Decimal("18.00")
    assert result["sgst_amount"] == Decimal("18.00")
    assert result["igst_amount"] == Decimal("0.00")
    assert result["line_total"] == Decimal("236.00")


def test_inter_state_charges_igst_only():
    result = compute_line(
        qty=Decimal("1"), rate=Decimal("1000"), discount_percent=Decimal("0"),
        gst_rate=Decimal("12"), is_intra_state=False,
    )
    assert result["cgst_amount"] == Decimal("0.00")
    assert result["sgst_amount"] == Decimal("0.00")
    assert result["igst_amount"] == Decimal("120.00")
    assert result["line_total"] == Decimal("1120.00")


def test_discount_percent_reduces_taxable_value():
    result = compute_line(
        qty=Decimal("1"), rate=Decimal("1000"), discount_percent=Decimal("10"),
        gst_rate=Decimal("18"), is_intra_state=True,
    )
    assert result["discount_amount"] == Decimal("100.00")
    assert result["taxable_value"] == Decimal("900.00")
    assert result["cgst_amount"] == Decimal("81.00")
    assert result["sgst_amount"] == Decimal("81.00")


def test_bill_of_supply_never_charges_gst_even_if_rate_present():
    result = compute_line(
        qty=Decimal("1"), rate=Decimal("500"), discount_percent=Decimal("0"),
        gst_rate=Decimal("18"), is_intra_state=True, charge_gst=False,
    )
    assert result["cgst_amount"] == Decimal("0.00")
    assert result["sgst_amount"] == Decimal("0.00")
    assert result["igst_amount"] == Decimal("0.00")
    assert result["line_total"] == Decimal("500.00")


def test_invoice_totals_round_off_to_nearest_rupee():
    lines = [
        compute_line(
            qty=Decimal("1"), rate=Decimal("333.33"), discount_percent=Decimal("0"),
            gst_rate=Decimal("18"), is_intra_state=True,
        )
    ]
    totals = compute_invoice_totals(lines)
    exact = totals["total_taxable_value"] + totals["total_cgst"] + totals["total_sgst"]
    assert totals["grand_total"] == exact.quantize(Decimal("1"))
    assert totals["round_off"] == totals["grand_total"] - exact


def test_invoice_totals_sum_multiple_lines():
    lines = [
        compute_line(Decimal("1"), Decimal("100"), Decimal("0"), Decimal("18"), True),
        compute_line(Decimal("2"), Decimal("50"), Decimal("0"), Decimal("5"), True),
    ]
    totals = compute_invoice_totals(lines)
    assert totals["total_taxable_value"] == Decimal("200.00")
    assert totals["total_cgst"] == Decimal("11.50")  # 9 + 2.5
    assert totals["total_sgst"] == Decimal("11.50")
