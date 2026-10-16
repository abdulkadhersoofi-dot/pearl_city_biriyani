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

# --- Business-type modules (Phase 9) ---------------------------------------------
# Restaurants: a packing / parcel charge or a service charge billed with the
# food is part of the same restaurant service and taxed at its rate - 5%
# (no ITC) for a restaurant not in a specified premises. Source:
# Notification 11/2017-CT(Rate) item 7(ii), as amended by Notification
# 46/2017-CT(Rate), effective 15.11.2017. A client whose rate differs
# changes it in Restaurant settings; this is only the default.
RESTAURANT_SERVICE_GST_RATE = Decimal("5")
RESTAURANT_SERVICE_SAC = "996331"
# A service charge can't be added to the bill by default or compulsorily -
# only if the customer agrees. Source: CCPA guidelines dated 04.07.2022
# (upheld by the Delhi High Court, 28.03.2025). So it is off on every bill
# until the cashier ticks it.
SERVICE_CHARGE_DEFAULT_ON = False

# Jewellery: articles of gold / silver jewellery (HSN 7113), the metal,
# making charges and stones on a jewellery sale all at 3%. Source:
# Notification 1/2017-CT(Rate), Schedule V, effective 01.07.2017.
JEWELLERY_GST_RATE = Decimal("3")
# Making charges billed alone, on the customer's own gold (job work in
# relation to jewellery), are a service at 5%. Source: Notification
# 11/2017-CT(Rate) item 26 (job work in relation to diamonds, jewellery),
# as amended by Notification 20/2019-CT(Rate), effective 01.10.2019.
JEWELLERY_JOB_WORK_GST_RATE = Decimal("5")
JEWELLERY_JOB_WORK_SAC = "9988"
# Metal purity: what fraction of pure metal each grade holds (24K = 999,
# 22K = 916 per BIS hallmarking, IS 1417 / IS 2112).
METAL_PURITIES = {
    "gold": {"24K": Decimal("0.999"), "22K": Decimal("0.916"), "18K": Decimal("0.750"), "14K": Decimal("0.585")},
    "silver": {"999": Decimal("0.999"), "925": Decimal("0.925")},
    "platinum": {"950": Decimal("0.950")},
}

# Pharmacy: drugs that may only be sold on a prescription and whose sale
# is recorded with the prescriber and patient. Source: Drugs and Cosmetics
# Rules 1945, Rule 65(9) and Schedule H / H1 (the H1 register from
# 01.03.2014, Notification G.S.R. 588(E) of 30.08.2013) and Schedule X.
DRUG_SCHEDULES = {"H": "Schedule H", "H1": "Schedule H1", "X": "Schedule X"}
# Which schedules need the patient's address on record too (the H1 / X
# register).
DRUG_SCHEDULES_REGISTER = {"H1", "X"}

# The firm's own subscription invoices to its clients and Auditors
# (Platform billing, Phase 10): the right to use the ARFA software,
# SAC 997331 (licensing services for the right to use computer software
# and databases), 18%. Source: Notification 11/2017-CT(Rate), item 17(iii)
# heading 9973, effective 01.07.2017. Editable on the platform's billing
# profile; these are only the starting values.
PLATFORM_SERVICE_SAC = "997331"
PLATFORM_SERVICE_GST_RATE = Decimal("18")
