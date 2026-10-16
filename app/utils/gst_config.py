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

# Units an item is sold loose in - weighed or measured - so the POS takes
# a decimal quantity for them (0.750 KGS) on a keypad instead of +1 taps.
# Not a GST rule; kept beside the UQC list it is a subset of.
LOOSE_QTY_UQC = frozenset({"KGS", "GMS", "QTL", "TON", "LTR", "MLT", "KLR", "MTR", "CMS", "KME", "YDS", "SQF", "SQM", "CBM", "CCM"})

# --- GSTR-1: B2C Large invoices -------------------------------------------
# An inter-state supply to an unregistered person is reported invoice by
# invoice (table 5, B2CL) when the invoice value is above this; below it,
# it goes into the B2CS summary (table 7). Source: Notification No.
# 12/2024-Central Tax dated 10.07.2024, amending Rule 59(4)(a)(ii) -
# reduced from Rs. 2,50,000 to Rs. 1,00,000, effective 01.08.2024.
B2CL_INVOICE_VALUE_THRESHOLD = Decimal("100000")

# --- Return due dates (monthly filers) -------------------------------------
# GSTR-1 by the 11th of the following month (Rule 59(1) / s.37(1)); GSTR-3B
# by the 20th of the following month (Rule 61(5)). Quarterly (QRMP) filers
# have different dates - not tracked here. Source: CGST Rules as amended
# by Notification No. 82/2020-Central Tax (effective 01.01.2021).
GSTR1_DUE_DAY = 11
GSTR3B_DUE_DAY = 20

# --- e-Invoice (IRN) ----------------------------------------------------------
# Mandatory for B2B supplies, exports and their credit / debit notes when
# the aggregate annual turnover in any year from 2017-18 exceeds this.
# Source: Notification No. 10/2023-Central Tax dated 10.05.2023, effective
# 01.08.2023 (amending Notification 13/2020-CT).
EINVOICE_AATO_THRESHOLD = Decimal("50000000")  # Rs. 5 crore
# Taxpayers with AATO at or above this can't report an invoice to the IRP
# more than EINVOICE_REPORTING_DAYS after its date. Source: GSTN advisory
# of 05.11.2024, effective 01.04.2025 (earlier, from 01.11.2023, only
# AATO >= Rs. 100 crore).
EINVOICE_REPORTING_LIMIT_AATO = Decimal("100000000")  # Rs. 10 crore
EINVOICE_REPORTING_DAYS = 30
EINVOICE_WARN_DAYS_BEFORE_LIMIT = 5
# An IRN can be cancelled on the IRP only within 24 hours of generation
# (IRP rule; after that a credit note is issued instead).
IRN_CANCEL_HOURS = 24

# --- e-Way Bill -----------------------------------------------------------------
# Needed to move goods worth more than this (consignment value, including
# tax). Source: Rule 138(1), CGST Rules 2017 (Notification 12/2018-CT).
# Some states set a different limit for movement within the state.
EWAY_BILL_VALUE_THRESHOLD = Decimal("50000")
# Validity, Rule 138(10): one day per this many km (or part); an
# over-dimensional cargo (ODC) vehicle gets one day per 20 km.
EWAY_BILL_KM_PER_DAY = 200
EWAY_BILL_KM_PER_DAY_ODC = 20
# Rule 138(9): an e-way bill can be cancelled within 24 hours.
EWAY_BILL_CANCEL_HOURS = 24
# The e-way bill system refuses a distance above this (EWB system
# validation, NIC e-way bill API v1.03 - max 4000 km).
EWAY_BILL_MAX_DISTANCE_KM = 4000
