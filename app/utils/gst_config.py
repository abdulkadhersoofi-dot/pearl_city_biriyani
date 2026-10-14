"""Every GST rate and legal threshold this app relies on, in one place.

Logic elsewhere (app.utils.gst, forms, reports) reads these values - it
never hardcodes a rate or a rupee threshold itself - so a rate change or
a new CBIC notification is a one-line edit here. Each entry names its
source and the date it took effect; update both together.
"""

from decimal import Decimal

# --- GST rate slabs --------------------------------------------------------
# Rates a product can be put on (Item master dropdown). Source: CGST/IGST
# rate notifications (1/2017-CT(R) as amended). The 40% special rate and
# the move to a mainly 5%/18% structure came from the 56th GST Council
# meeting (rates effective 22 Sep 2025); 12%/28% stay selectable because
# existing items, older documents and transitional goods still use them.
# 0.1/0.25/1.5/3/7.5 are the special low rates (merchant exports, rough
# diamonds, precious metals, specified hotel/job-work services).
GST_RATE_SLABS = [
    Decimal("0"),
    Decimal("0.1"),
    Decimal("0.25"),
    Decimal("1"),
    Decimal("1.5"),
    Decimal("3"),
    Decimal("5"),
    Decimal("6"),
    Decimal("7.5"),
    Decimal("12"),
    Decimal("18"),
    Decimal("28"),
    Decimal("40"),
]

# --- HSN / SAC code digits on invoices ------------------------------------
# Source: Notification No. 78/2020-Central Tax dated 15.10.2020, effective
# 01.04.2021 - a taxpayer whose Aggregate Annual Turnover (AATO) in the
# preceding financial year was up to Rs. 5 crore must show at least 4
# digits of HSN/SAC; above Rs. 5 crore, at least 6 digits.
HSN_AATO_THRESHOLD = Decimal("50000000")  # Rs. 5 crore
HSN_MIN_DIGITS_UP_TO_THRESHOLD = 4
HSN_MIN_DIGITS_ABOVE_THRESHOLD = 6
# HSN/SAC codes are published at the 4-digit heading, 6-digit sub-heading
# and 8-digit tariff-item level - an odd length is never a real code (it's
# almost always a lost leading zero, e.g. "401" typed for "0401").
HSN_VALID_LENGTHS = (4, 6, 8)
# Services Accounting Codes all sit in chapter 99.
SAC_PREFIX = "99"

# --- Unit Quantity Codes ---------------------------------------------------
# Source: GST portal UQC master (the list GSTR-1's HSN summary accepts),
# as published with the GSTR-1 offline tool. Code -> description.
UQC_CODES = {
    "BAG": "Bags",
    "BAL": "Bale",
    "BDL": "Bundles",
    "BKL": "Buckles",
    "BOU": "Billion of units",
    "BOX": "Box",
    "BTL": "Bottles",
    "BUN": "Bunches",
    "CAN": "Cans",
    "CBM": "Cubic metres",
    "CCM": "Cubic centimetres",
    "CMS": "Centimetres",
    "CTN": "Cartons",
    "DOZ": "Dozens",
    "DRM": "Drums",
    "GGK": "Great gross",
    "GMS": "Grammes",
    "GRS": "Gross",
    "GYD": "Gross yards",
    "KGS": "Kilograms",
    "KLR": "Kilolitre",
    "KME": "Kilometre",
    "LTR": "Litres",
    "MLT": "Millilitre",
    "MTR": "Metres",
    "MTS": "Metric ton",
    "NOS": "Numbers",
    "OTH": "Others",
    "PAC": "Packs",
    "PCS": "Pieces",
    "PRS": "Pairs",
    "QTL": "Quintal",
    "ROL": "Rolls",
    "SET": "Sets",
    "SQF": "Square feet",
    "SQM": "Square metres",
    "SQY": "Square yards",
    "TBS": "Tablets",
    "TGM": "Ten gross",
    "THD": "Thousands",
    "TON": "Tonnes",
    "TUB": "Tubes",
    "UGS": "US gallons",
    "UNT": "Units",
    "YDS": "Yards",
}
DEFAULT_UQC = "PCS"
