import pytest

from app.invoicing.services import InvoiceValidationError, parse_line_arrays


def test_parse_line_arrays_accepts_valid_input():
    lines = parse_line_arrays(
        ["Chicken Biriyani"], ["996331"], ["10"], ["220"], ["0"], ["5"], [""], ["plate"]
    )
    assert len(lines) == 1
    assert lines[0].hsn_or_sac_code == "996331"


def test_parse_line_arrays_rejects_hsn_longer_than_column_limit():
    # A too-long HSN must fail as a clean validation error here, not as an
    # unhandled database error once it reaches the VARCHAR(8) column.
    with pytest.raises(InvoiceValidationError, match="HSN/SAC code"):
        parse_line_arrays(
            ["Chicken Biriyani"], ["996331996331"], ["10"], ["220"], ["0"], ["5"], [""], ["plate"]
        )


def test_parse_line_arrays_rejects_description_longer_than_column_limit():
    with pytest.raises(InvoiceValidationError, match="Item description"):
        parse_line_arrays(
            ["x" * 300], ["996331"], ["1"], ["100"], ["0"], ["5"], [""], ["pcs"]
        )


def test_parse_line_arrays_skips_blank_rows():
    lines = parse_line_arrays(
        ["", "Chicken Biriyani"], ["", "996331"], ["", "1"], ["", "220"], ["", "0"], ["", "5"], ["", ""], ["", "plate"]
    )
    assert len(lines) == 1
    assert lines[0].description == "Chicken Biriyani"
