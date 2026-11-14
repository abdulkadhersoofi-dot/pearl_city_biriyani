# Ported unchanged from the ARFA Invoice Parser (validators.py) - the
# deterministic checks that prove what the AI read. Keep in step with it.
"""
validators.py — ARFA Deterministic Invoice Validation Engine
=============================================================
Gemini extracts. This module PROVES.

Every extracted invoice passes through a battery of mathematical and
structural checks that do not depend on AI:

  HARD checks (failure = invoice needs human review before save):
    gstin_supplier   — 15-char format + checksum digit verification
    gstin_receiver   — same
    totals_match     — taxable + IGST + CGST + SGST + CESS − TDS − discount
                       + round_off ≈ invoice_value (±₹1)
    li_taxable_sum   — Σ line-item taxable == root taxable (±₹1)
    li_tax_sum       — Σ line-item IGST/CGST/SGST/CESS == root values (±₹1)
    cgst_eq_sgst     — CGST must equal SGST when both present (±₹0.02)
    igst_xor_local   — IGST and CGST/SGST must not BOTH be charged

  SOFT checks (warning — shown to user, never blocks):
    interstate_logic — supplier/receiver state codes vs IGST-vs-CGST usage
    line_rate_math   — per-line tax ≈ gst_rate × taxable (±₹1)
    rate_slab_tax    — Σ(rate × taxable per slab) == consolidated tax (HARD)
    slab_taxable_sum — Σ line taxable == invoice taxable
    date_sane        — valid date, not in the future, not absurdly old
    hsn_format       — HSN/SAC is 4, 6 or 8 digits
    inv_number       — invoice number present
    pos_state        — place of supply consistent with receiver GSTIN state

  AUTO-REPAIR (deterministic fixes applied BEFORE checks):
    - strip spaces / punctuation inside GSTINs, uppercase
    - derive supplier_pan from a checksum-valid supplier GSTIN
    - if line items are internally consistent but root totals are zero or
      wrong → recompute root totals from line items
    - if root totals are present but a single line item is zero → backfill
    - recompute round_off when |computed − invoice_value| ≤ ₹0.99

Output contract (attached to the record as rec["validation"]):
  {
    "status":  "verified" | "review" | "failed",
    "score":   0–100,
    "checks":  [{"id","label","ok","level","detail"}],
    "repairs": ["human-readable description of each auto-fix"],
    "field_flags": ["supplier_gstin", "invoice_value", ...]   # for UI highlight
  }
"""

from __future__ import annotations
import re
from datetime import date, datetime, timedelta

TOL_RUPEE = 1.00     # tolerance for totals reconciliation
TOL_TAX   = 2.00     # forgiving tolerance for tax verification (a rupee or two)
TOL_PAISA = 0.02     # tolerance for CGST == SGST

_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")

STATE_CODES = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana", "07": "Delhi",
    "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam",
    "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra & Nagar Haveli and Daman & Diu", "27": "Maharashtra",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
    "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman & Nicobar",
    "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory", "99": "Centre Jurisdiction",
}

# Informal abbreviations seen on invoices that don't appear in STATE_CODES'
# own names verbatim — feeds _pos_to_state_code's lookup, never used elsewhere.
_STATE_ABBR = {
    "TN": "33", "MH": "27", "KA": "29", "KL": "32", "AP": "37", "TG": "36",
    "TS": "36", "WB": "19", "UP": "09", "MP": "23", "GJ": "24", "RJ": "08",
    "PB": "03", "HR": "06", "DL": "07", "BR": "10", "JH": "20", "CG": "22",
    "OD": "21", "OR": "21", "AS": "18", "GA": "30", "PY": "34",
}

def _norm_state_name(s: str) -> str:
    """Lowercase, alphanumeric-only — for tolerant state-name matching."""
    return re.sub(r"[^a-z0-9]", "", s.lower())

_STATE_NAME_TO_CODE = {_norm_state_name(name): code for code, name in STATE_CODES.items()}

def _pos_to_state_code(pos: str) -> str | None:
    """Best-effort: free-text place_of_supply (OCR'd) -> 2-digit state code.
    Handles: bare code ('33'), full name ('Tamil Nadu'), name+code
    ('Tamil Nadu, Code: 33' / 'Tamil Nadu (33)'), and common informal
    abbreviations ('TN'). Returns None when unrecognisable — caller must
    treat that as "skip the check", never as a mismatch."""
    s = (pos or "").strip()
    if not s:
        return None
    # Bare or leading 2-digit code
    m = re.match(r"^(\d{2})\b", s)
    if m and m.group(1) in STATE_CODES:
        return m.group(1)
    # "...Code: NN" or "...(NN)" anywhere in the string
    m = re.search(r"(?:code\s*[:\-]?\s*|\()(\d{2})\)?", s, re.I)
    if m and m.group(1) in STATE_CODES:
        return m.group(1)
    # Full state name (ignoring punctuation/case), trimmed of a trailing code
    name_part = re.split(r",|\(", s)[0]
    key = _norm_state_name(name_part)
    if key in _STATE_NAME_TO_CODE:
        return _STATE_NAME_TO_CODE[key]
    # Common informal abbreviation (e.g. "TN")
    abbr = name_part.strip().upper()
    if abbr in _STATE_ABBR:
        return _STATE_ABBR[abbr]
    return None


# ── GSTIN checksum ────────────────────────────────────────────────────

def clean_gstin(g) -> str:
    """Remove every non-alphanumeric char, uppercase."""
    return re.sub(r"[^A-Z0-9]", "", str(g or "").upper())


def gstin_checksum_ok(gstin: str) -> bool:
    """
    Verify the 15th character of a GSTIN against the official
    mod-36 weighted checksum. This is deterministic — a GSTIN that
    passes format AND checksum is almost certainly transcribed correctly.
    """
    g = clean_gstin(gstin)
    if len(g) != 15 or not _GSTIN_RE.match(g):
        return False
    total = 0
    for i, ch in enumerate(g[:14]):
        v = _CHARSET.index(ch)
        w = v * (1 if i % 2 == 0 else 2)
        total += w // 36 + w % 36
    expected = _CHARSET[(36 - total % 36) % 36]
    return g[14] == expected


# PAN structure inside a GSTIN (chars 3-12): 3 random letters + entity-type
# letter + name letter + 4 digits + check letter. The entity-type letter
# (4th of PAN = 6th of GSTIN) has a CLOSED set — a deterministic misread net.
# NOTE (by design): the 5th PAN letter is the first letter of the holder's
# SURNAME for individuals — it must NEVER be validated against the company/
# trade name. A proprietor trading as "XYZ Traders" legitimately carries a
# PAN starting with their own surname letter.
PAN_ENTITY_TYPES = {
    "P": "Individual", "C": "Company", "H": "HUF", "F": "Firm/LLP",
    "A": "AOP", "T": "Trust", "B": "BOI", "L": "Local Authority",
    "J": "Artificial Juridical Person", "G": "Government",
}

def pan_entity_of_gstin(gstin: str) -> tuple[str, str | None]:
    """(entity letter, human label or None if invalid) from a 15-char GSTIN."""
    g = clean_gstin(gstin)
    if len(g) != 15:
        return "", None
    ch = g[5]
    return ch, PAN_ENTITY_TYPES.get(ch)


def gstin_check_digit(first14: str) -> str | None:
    """Compute the correct 15th character from the first 14 (Luhn mod-36,
    official GSTN algorithm: values 0-9,A-Z→0-35; alternating ×1/×2 weights
    right-to-left with ×2 on the 14th char; doubled values digit-split as
    w//36 + w%36; check = (36 − sum mod 36) mod 36)."""
    g = clean_gstin(first14)[:14]
    if len(g) != 14 or any(c not in _CHARSET for c in g):
        return None
    total = 0
    for i, ch in enumerate(g):
        v = _CHARSET.index(ch)
        w = v * (1 if i % 2 == 0 else 2)
        total += w // 36 + w % 36
    return _CHARSET[(36 - total % 36) % 36]


_GSTIN14_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z$")

def gstin_repair(gstin: str) -> tuple[str, list[str]]:
    """CONSERVATIVE GSTIN repair. The ONLY change we make is fixing the 14th
    character to 'Z' — that position is ALWAYS 'Z' by the GSTIN spec, so a
    different letter there is unambiguously a scanner misread and the fix is
    deterministic and safe.

    We DELIBERATELY DO NOT recompute the check digit. A wrong check digit
    means the GSTIN was misread — recomputing it would forge a valid-looking
    but WRONG number and destroy the only signal that the read was bad. A bad
    checksum must stay bad so it gets flagged for human review. (The only
    place a misread GSTIN is corrected is the company-anchor snap in
    validate(), where we KNOW the true value.)"""
    g = clean_gstin(gstin)
    notes = []
    if len(g) != 15:
        return gstin, notes
    # Safe, certain fix only: 14th char must be 'Z'
    if g[13] != "Z" and re.match(r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]$", g[:13]):
        cand = g[:13] + "Z" + g[14]
        if re.match(r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z$", cand[:14]):
            notes.append(f"GSTIN 14th char fixed to 'Z' (always Z by spec): '{g[13]}' → 'Z'")
            g = cand
    return g, notes


def gstin_state(gstin: str) -> str | None:
    g = clean_gstin(gstin)
    return STATE_CODES.get(g[:2]) if len(g) >= 2 else None


def gstin_pan(gstin: str) -> str | None:
    g = clean_gstin(gstin)
    return g[2:12] if len(g) >= 12 else None


# ── Helpers ───────────────────────────────────────────────────────────

def _line_rate(li):
    """Best-effort GST rate for a line item — the STANDARD, COMBINED rate
    (0/5/12/18/28), not a per-component figure. By the time this runs,
    auto_repair's normalization step has already converted any printed
    per-component rate (e.g. "CGST @9%, SGST @9%" → gst_rate=9) into the
    combined total (18), so every caller here can just use the value
    directly with no further interpretation needed:
      1. explicit gst_rate field, else
      2. derive from the line's OWN tax: (igst+cgst+sgst)/taxable × 100,
         snapped to the nearest standard slab (0,3,5,12,18,28) when close."""
    rate = _n(li.get("gst_rate"))
    if rate > 0:
        return rate
    tv  = _n(li.get("taxable_value"))
    tax = _n(li.get("igst")) + _n(li.get("cgst")) + _n(li.get("sgst"))
    if tv > 0 and tax > 0:
        r = tax / tv * 100
        # snap to nearest standard GST slab if within 0.6 points
        for std in (0, 3, 5, 12, 18, 28):
            if abs(r - std) <= 0.6:
                return float(std)
        return round(r, 2)
    return 0.0


def _rate_slab_tax(items):
    """Group line items by GST rate and compute expected total tax per the
    GST method: Σ over rate-slabs of (slab_taxable × rate). Rates are
    derived per _line_rate (already the combined rate, pre-normalized —
    see auto_repair) so this works whether the line shows a rate, shows
    its own tax, or neither.
    Returns (expected_total_tax, taxable_total, slabs_dict, usable)."""
    slabs = {}   # rate -> taxable sum
    taxable_total = 0.0
    usable = False
    for li in items:
        tv = _n(li.get("taxable_value"))
        if tv <= 0:
            continue
        taxable_total += tv
        rate = _line_rate(li)
        if rate > 0:
            usable = True
        slabs[rate] = slabs.get(rate, 0.0) + tv
    expected_tax = round(sum(tv * rate / 100 for rate, tv in slabs.items()), 2)
    return expected_tax, round(taxable_total, 2), slabs, usable


def _n(v) -> float:
    """Coerce to float, treating None/'' / garbage as 0."""
    if v is None:
        return 0.0
    try:
        return float(str(v).replace(",", "").replace("\u20b9", "").strip() or 0)
    except Exception:
        return 0.0


def _normalise_line_gst_rates(items: list) -> list[str]:
    """Normalise each line's gst_rate to the TRUE COMBINED rate, derived
    from its own real tax figures, whenever that derived rate lands
    cleanly on a real GST slab (0/3/5/12/18/28). Indian invoices commonly
    print CGST and SGST as separate rate columns for an intrastate line
    (e.g. "CGST @9%, SGST @9%"), and Gemini extracts gst_rate as that
    single printed component figure (9) — correct as read, but ambiguous
    downstream: every consumer (validation, the edit form, Excel export)
    would need to separately know "is this line intrastate?" to interpret
    9 as really meaning 18. That's fragile — miss the conversion in ONE
    place and a perfectly correct invoice displays a nonsense rate (9%
    isn't a real GST slab on its own).

    Deliberately idempotent — safe to call more than once on the same
    items list (a line already normalised produces the same snapped rate
    again, so the "only overwrite if different" guard is a no-op the
    second time). This matters because a line's tax may exist from the
    start (Gemini extracted it directly) or only appear later, once
    auto_repair's own fill-missing-tax step runs — this needs calling at
    both points to catch either case.

    Only overwrites when the derived rate is PLAUSIBLE (lands on a real
    slab). If it doesn't (e.g. a genuinely wrong tax amount derives
    something like 199.8%), the original stored rate is left alone, so
    downstream checks still have an independent value to catch the error
    against, instead of gst_rate just echoing whatever (even wrong) tax
    was entered.
    """
    repairs = []
    for li in items:
        tv = _n(li.get("taxable_value"))
        li_tax = _n(li.get("igst")) + _n(li.get("cgst")) + _n(li.get("sgst"))
        if tv > 0 and li_tax > 0:
            true_rate = li_tax / tv * 100
            snapped = None
            for std in (0, 3, 5, 12, 18, 28):
                if abs(true_rate - std) <= 0.6:
                    snapped = float(std)
                    break
            if snapped is not None:
                old_rate = _n(li.get("gst_rate"))
                if abs(old_rate - snapped) > 0.5:
                    li["gst_rate"] = snapped
                    repairs.append(
                        f"Normalised line GST rate to combined figure: "
                        f"{old_rate:g}% → {snapped:g}%")
    return repairs


def _fill_line_item_taxes(rec, items):
    """Populate each line item's per-line tax from rate x taxable, per the GST
    Act. The invoice's TOTAL already tells us whether it's interstate (IGST) or
    intrastate (CGST+SGST); we split each line's tax the same way:
        IGST  line tax = taxable x rate%          (rate is the full combined
        CGST  line tax = taxable x (rate/2)%       rate — auto_repair already
        SGST  line tax = taxable x (rate/2)%       normalized it from any
                                                     printed per-component
                                                     figure to the standard
                                                     combined value before
                                                     this function ever runs).
    Returns list of field-names that were filled (for repair notes). Only fills
    a line's tax when it's missing/zero — never overwrites an extracted value."""
    # Decide IGST vs CGST/SGST from the invoice's own total tax fields.
    root_igst = _n(rec.get("igst"))
    root_cgst = _n(rec.get("cgst"))
    root_sgst = _n(rec.get("sgst"))
    interstate = root_igst > 0 and (root_cgst + root_sgst) < TOL_RUPEE
    intrastate = (root_cgst + root_sgst) > 0 and root_igst < TOL_RUPEE
    # If the totals are blank, fall back to GSTIN state codes.
    if not interstate and not intrastate:
        sg = clean_gstin(rec.get("supplier_gstin") or "")
        rg = clean_gstin(rec.get("receiver_gstin") or "")
        if len(sg) >= 2 and len(rg) >= 2:
            interstate = sg[:2] != rg[:2]
            intrastate = not interstate

    filled = []
    for li in items:
        rate = _line_rate(li)
        tv   = _n(li.get("taxable_value"))
        if rate <= 0 or tv <= 0:
            continue
        line_has_tax = (_n(li.get("igst")) + _n(li.get("cgst")) + _n(li.get("sgst"))) > 0.01
        if line_has_tax:
            continue   # already has per-line tax — don't overwrite
        if interstate:
            li["igst"] = round(tv * rate / 100, 2)
            li["cgst"] = 0.0
            li["sgst"] = 0.0
            filled.append("igst")
        elif intrastate:
            # rate is the COMBINED figure (pre-normalized) — halve it for
            # the CGST/SGST split, since each component carries half the
            # combined rate.
            each = round(tv * (rate / 2) / 100, 2)
            li["cgst"] = each
            li["sgst"] = each
            li["igst"] = 0.0
            filled.append("cgst/sgst")
    return filled


def _li_sums(items: list) -> dict:
    out = {"taxable_value": 0.0, "igst": 0.0, "cgst": 0.0,
           "sgst": 0.0, "cess": 0.0, "amount": 0.0}
    for li in items or []:
        for k in out:
            out[k] += _n(li.get(k))
    return {k: round(v, 2) for k, v in out.items()}


def _computed_total(rec: dict) -> float:
    return round(
        _n(rec.get("taxable_value")) + _n(rec.get("igst")) + _n(rec.get("cgst"))
        + _n(rec.get("sgst")) + _n(rec.get("cess"))
        - _n(rec.get("tds")) - _n(rec.get("discount"))
        + _n(rec.get("round_off")), 2)


# ── Auto-repair (deterministic only — never guesses) ─────────────────

def _near_miss(a: str, b: str, max_diff: int = 3) -> bool:
    """Same-length strings differing in ≤ max_diff positions — the classic
    OCR profile (I↔1, O↔0, K↔A on rough scans)."""
    if len(a) != len(b) or not a:
        return False
    return sum(1 for x, y in zip(a, b) if x != y) <= max_diff


def auto_repair(rec: dict, company_gstin: str = "") -> list[str]:
    """Mutates rec in place. Returns list of human-readable repairs made."""
    repairs = []

    # 1. Normalise GSTINs (kill the classic "33AAICS8948 L1ZN" space bug)
    for fld in ("supplier_gstin", "receiver_gstin"):
        raw = rec.get(fld)
        if raw:
            cleaned = clean_gstin(raw)
            if cleaned != str(raw):
                rec[fld] = cleaned
                repairs.append(f"Normalised {fld}: '{raw}' → '{cleaned}'")

    cg_clean = clean_gstin(company_gstin)

    # 1a-0. SWAP DETECTION (deterministic, ground-truth based):
    #   The uploader's company is one party on its own invoice. For a Sales
    #   invoice the company is the SUPPLIER; for a Purchase it's the RECEIVER.
    #   If the company's exact GSTIN was read into the OPPOSITE field, Gemini
    #   swapped supplier/receiver — switch them back. Uses exact match only,
    #   so it never fires on an uncertain read.
    if len(cg_clean) == 15 and gstin_checksum_ok(cg_clean):
        sup_c = clean_gstin(rec.get("supplier_gstin"))
        rec_c = clean_gstin(rec.get("receiver_gstin"))
        cat = (rec.get("category") or "").strip().lower()
        swapped = False
        # startswith, not ==, so this also covers "sales - credit note",
        # "purchase - debit note" etc — a credit/debit note issued by this
        # company keeps the same supplier/receiver role as the base
        # category (a sales credit note still has US as supplier), so the
        # same swap logic applies. The exact-match version silently missed
        # every credit/debit note category string _classify_category
        # actually produces.
        if cat.startswith("sales") and rec_c == cg_clean and sup_c != cg_clean:
            swapped = True
        elif cat.startswith("purchase") and sup_c == cg_clean and rec_c != cg_clean:
            swapped = True
        if swapped:
            rec["supplier_gstin"], rec["receiver_gstin"] = \
                rec.get("receiver_gstin"), rec.get("supplier_gstin")
            rec["supplier_name"], rec["receiver_name"] = \
                rec.get("receiver_name"), rec.get("supplier_name")
            repairs.append(f"Supplier/receiver were swapped (company GSTIN was on the "
                           f"wrong side for a {cat} invoice) — corrected.")

    # 1a. ANCHOR REPAIR FIRST (ground truth beats any computed guess):
    #     the uploading company's own GSTIN is known-correct. If a side is a
    #     ≤3-char near-miss of it, the scanner misread OUR OWN number — snap
    #     it. Runs BEFORE check-digit repair so a misread that happens to stay
    #     structurally valid (e.g. J→U) is corrected to the TRUE value, not to
    #     a wrong-but-self-consistent one.
    _anchored = set()
    if len(cg_clean) == 15 and gstin_checksum_ok(cg_clean):
        for fld in ("supplier_gstin", "receiver_gstin"):
            v = clean_gstin(rec.get(fld))
            if v and v != cg_clean and _near_miss(v, cg_clean):
                rec[fld] = cg_clean
                _anchored.add(fld)
                repairs.append(f"Snapped {fld} to your company GSTIN: '{v}' → '{cg_clean}'")

    # 1b. SPEC REPAIR for any field the anchor did NOT fix: the 14th char is
    #     always 'Z' and the 15th is computable from the first 14 — fix
    #     last-character misreads (I↔1 etc.) deterministically, no guessing.
    for fld in ("supplier_gstin", "receiver_gstin"):
        if fld in _anchored:
            continue
        v = rec.get(fld)
        if v:
            fixed, notes = gstin_repair(v)
            if notes:
                rec[fld] = fixed
                repairs.extend(f"{fld}: {n}" for n in notes)

    # 2. Derive supplier PAN from a checksum-valid GSTIN
    sg = rec.get("supplier_gstin") or ""
    if gstin_checksum_ok(sg):
        pan = gstin_pan(sg)
        if pan and clean_gstin(rec.get("supplier_pan")) != pan:
            old = rec.get("supplier_pan")
            rec["supplier_pan"] = pan
            if old:
                repairs.append(f"Corrected supplier PAN from GSTIN: '{old}' → '{pan}'")
            else:
                repairs.append(f"Filled supplier PAN from GSTIN: '{pan}'")

    # 3. Root totals vs line-item sums.
    #    If line items exist and sum to something, and the root totals are
    #    zero/missing or off by more than tolerance — trust the line items
    #    ONLY when the line items are internally rate-consistent.
    items = rec.get("line_items") or []
    if items:
        # Normalise gst_rate for any line that ALREADY has its own real tax
        # (extracted directly by Gemini, before this function ever touched
        # it) — must happen before internally_ok's check just below, which
        # assumes such lines are already normalised. A SECOND call happens
        # later, after step 3c fills in any still-missing per-line tax, to
        # catch lines that only gained tax figures during this same repair
        # pass — see _normalise_line_gst_rates's docstring for why calling
        # it twice is safe (idempotent on lines already normalised).
        repairs.extend(_normalise_line_gst_rates(items))

        sums = _li_sums(items)
        if sums["taxable_value"] > 0:
            # A line with its own tax was ALREADY normalised just above by
            # _normalise_line_gst_rates — gst_rate is now the combined
            # figure, safe to use directly with no further interpretation.
            internally_ok = True
            for li in items:
                rate = _n(li.get("gst_rate"))
                tv   = _n(li.get("taxable_value"))
                tax  = _n(li.get("igst")) + _n(li.get("cgst")) + _n(li.get("sgst"))
                if rate > 0 and tv > 0 and abs(tax - tv * rate / 100) > max(TOL_RUPEE, tv * 0.005):
                    internally_ok = False
                    break
            # TAX fields (igst/cgst/sgst) carry NO signal from line items that
            # have no gst_rate filled at all — e.g. a single-rate invoice where
            # the rate is stated once for the whole document rather than
            # repeated per line (a completely normal, common layout). Before
            # this guard, "internally_ok" was trivially True for an all-zero-
            # rate invoice (the inconsistency check never triggers when rate=0
            # everywhere), so this step would silently OVERWRITE a correctly
            # extracted root tax figure with a fabricated 0.0 — the sum of
            # line items that were never carrying tax info in the first place.
            # Require every taxable line to actually have a KNOWN rate before
            # trusting the line-item tax sum enough to override root.
            #
            # "Known" is not simply "gst_rate > 0" — a genuinely NIL-RATED
            # line (0%, correct and common: e.g. a store selling both taxable
            # and nil-rated goods on the same bill) legitimately has
            # gst_rate == 0, and treating that the same as "rate never
            # filled in" meant such an invoice's tax could never be trusted
            # from its own line items at all. A 0-rate line counts as
            # confirmed-nil (not missing) when its own tax is ALSO zero AND
            # at least one other line on this invoice has real tax — that
            # combination is evidence Gemini was actively filling per-line
            # figures for this invoice, so a 0/0 line here is a genuine
            # nil-rated item, not a gap. An invoice with no rate or tax
            # anywhere still correctly fails this, preserving the original
            # safety behaviour.
            taxable_lines = [li for li in items if _n(li.get("taxable_value")) > 0]
            _any_line_has_real_tax = any(
                (_n(li.get("igst")) + _n(li.get("cgst")) + _n(li.get("sgst"))) > 0.01
                for li in taxable_lines)
            def _rate_known(li):
                if _n(li.get("gst_rate")) > 0:
                    return True
                line_tax = _n(li.get("igst")) + _n(li.get("cgst")) + _n(li.get("sgst"))
                return line_tax <= 0.01 and _any_line_has_real_tax
            _lines_rate_complete = bool(taxable_lines) and \
                all(_rate_known(li) for li in taxable_lines)
            if internally_ok:
                for k in ("taxable_value", "igst", "cgst", "sgst", "cess"):
                    if k in ("igst", "cgst", "sgst") and not _lines_rate_complete:
                        continue   # no rate info on the lines — leave root as extracted
                    root = round(_n(rec.get(k)), 2)
                    if abs(root - sums[k]) > TOL_RUPEE:
                        rec[k] = sums[k]
                        repairs.append(
                            f"Recomputed {k} from line items: {root} → {sums[k]}")

    # 3b. Rate-slab tax fill: multiple rates but consolidated tax missing.
    #     Compute total tax from Σ(slab_taxable × rate); split per the tax type
    #     already indicated on the record (IGST present → interstate; else CGST/SGST).
    #
    #     Skipped entirely when this is a confirmed zero-tax-legitimate
    #     invoice (RCM / export-SEZ under LUT-Bond / composition scheme /
    #     nil-rated) — the supplier genuinely charges no tax in those
    #     cases, even if a line item still shows an informational gst_rate
    #     (e.g. the applicable rate for the buyer's own self-assessment
    #     under RCM). Filling tax in from that rate would corrupt an
    #     invoice that was already correct as extracted.
    _zero_reason_3b = _zero_tax_reason(rec)
    if items and not _zero_reason_3b:
        # decide intrastate vs interstate from GSTIN state codes if available
        sg = clean_gstin(rec.get("supplier_gstin") or "")
        rg = clean_gstin(rec.get("receiver_gstin") or "")
        interstate = (len(sg) >= 2 and len(rg) >= 2 and sg[:2] != rg[:2])
        root_tax_now = _n(rec.get("igst")) + _n(rec.get("cgst")) + _n(rec.get("sgst"))
        # These lines have NO tax of their own yet (that's why we're in this
        # branch at all) — _normalise_line_gst_rates above had nothing to
        # derive a rate from, so gst_rate is still the raw per-component
        # figure. Double it in place for intrastate lines, once, here —
        # this is the "second normalisation point" for the case where a
        # rate needs converting before tax gets filled FROM it, rather
        # than after tax already exists to derive it from.
        if not interstate and root_tax_now < TOL_RUPEE:
            for li in items:
                explicit = _n(li.get("gst_rate"))
                if explicit > 0:
                    li["gst_rate"] = round(explicit * 2, 2)
        expected_tax, li_taxable, slabs, usable = _rate_slab_tax(items)
        if usable and expected_tax > 0 and root_tax_now < TOL_RUPEE:
            if interstate:
                rec["igst"] = expected_tax
                repairs.append(f"Filled IGST from rate slabs: ₹{expected_tax:,.2f}")
            else:
                half = round(expected_tax / 2, 2)
                rec["cgst"] = half
                rec["sgst"] = round(expected_tax - half, 2)  # keep exact sum
                repairs.append(f"Filled CGST+SGST from rate slabs: ₹{expected_tax:,.2f} total")
            if _n(rec.get("taxable_value")) < TOL_RUPEE and li_taxable > 0:
                rec["taxable_value"] = li_taxable
                repairs.append(f"Filled taxable_value from line items: ₹{li_taxable:,.2f}")

    # 3c. Per-line tax calculator — fill each line item's IGST or CGST/SGST from
    #     rate x taxable (CGST/SGST split in half, per the GST Act). This makes
    #     the per-line tax visible in the UI; the rate_slab_tax check then
    #     verifies the sum against the invoice's extracted total tax.
    if items:
        filled = _fill_line_item_taxes(rec, items)
        if filled:
            kinds = "IGST" if "igst" in filled else "CGST/SGST"
            repairs.append(f"Calculated per-line {kinds} from rate × taxable "
                           f"for {len(filled)} line item(s)")

    # 3d. Normalise gst_rate to the COMBINED rate, at the source — runs
    #     AFTER 3c so a line whose tax was just filled in above (rather
    #     than extracted directly by Gemini) is treated identically to one
    #     that already had its own tax figures from the start.
    #
    #     Indian invoices commonly print CGST and SGST as separate rate
    #     columns for an intrastate line (e.g. "CGST @9%, SGST @9%"), and
    #     Gemini extracts gst_rate as that single printed component figure
    #     (9) — correct as read, but ambiguous downstream: every consumer
    #     (validation, the edit form, Excel export) would need to
    #     separately know "is this line intrastate?" to interpret 9 as
    #     really meaning 18. That's fragile — miss the conversion in ONE
    #     place and a perfectly correct invoice displays a nonsense rate
    #     (9% isn't a real GST slab on its own).
    #
    #     Fixed at the source instead: whenever a line has its own real
    #     CGST+SGST (or IGST) figures, the TRUE combined rate is
    #     unambiguous — just divide the actual tax by the actual taxable
    #     value. Overwrite gst_rate with that derived, verified figure,
    #     but ONLY when it lands cleanly on a real GST slab (0/3/5/12/18/28)
    #     — if it doesn't (e.g. a genuinely wrong tax amount derives
    #     something like 199.8%), leave the original stored rate alone, so
    #     downstream checks still have an independent value to catch the
    #     error against, instead of gst_rate just echoing whatever (even
    #     wrong) tax was entered.
    for li in items:
        tv = _n(li.get("taxable_value"))
        li_tax = _n(li.get("igst")) + _n(li.get("cgst")) + _n(li.get("sgst"))
        if tv > 0 and li_tax > 0:
            true_rate = li_tax / tv * 100
            snapped = None
            for std in (0, 3, 5, 12, 18, 28):
                if abs(true_rate - std) <= 0.6:
                    snapped = float(std)
                    break
            if snapped is not None:
                old_rate = _n(li.get("gst_rate"))
                if abs(old_rate - snapped) > 0.5:
                    li["gst_rate"] = snapped
                    repairs.append(
                        f"Normalised line GST rate to combined figure: "
                        f"{old_rate:g}% → {snapped:g}%")

    # 4. Round-off reconciliation: if computed total is within 99 paise of
    #    the stated invoice value, set round_off to close the gap exactly.
    inv_v = _n(rec.get("invoice_value"))
    if inv_v > 0:
        base = _computed_total(rec) - _n(rec.get("round_off"))
        gap  = round(inv_v - base, 2)
        if 0 < abs(gap) <= 0.99 and abs(gap - _n(rec.get("round_off"))) > 0.001:
            old = rec.get("round_off")
            rec["round_off"] = gap
            repairs.append(f"Reconciled round_off: {old} → {gap}")

    # 5. If invoice_value missing but everything else reconciles — fill it.
    if inv_v == 0:
        comp = _computed_total(rec)
        if comp > 0:
            rec["invoice_value"] = comp
            repairs.append(f"Filled invoice_value from components: {comp}")

    return repairs


# ── Zero-tax legitimacy detection ─────────────────────────────────────

_ZERO_TAX_SIGNALS = {
    "lut": "Export/SEZ supply under LUT (zero-rated)",
    "letter of undertaking": "Export/SEZ supply under LUT (zero-rated)",
    "bond": "Export/SEZ supply under Bond (zero-rated)",
    "sez": "SEZ supply (zero-rated)",
    "export": "Export supply (zero-rated)",
    "composition": "Composition-scheme supplier — not permitted to charge GST separately",
    "nil rated": "Nil-rated supply",
    "nil-rated": "Nil-rated supply",
    "exempt": "Exempt supply",
}

def _zero_tax_reason(rec: dict) -> str:
    """Best-effort detection that a taxable-value-with-zero-tax invoice is
    LEGITIMATE, not a totals page that never got read. Checks the two
    fields Gemini actually extracts free text into (reverse_charge, notes)
    for the standard signals: RCM (buyer self-assesses, supplier charges
    nothing), export/SEZ under LUT or Bond, composition-scheme suppliers
    (barred by law from charging GST on their invoice), and nil-rated/
    exempt supply. Returns a human-readable reason, or "" if no signal
    was found — callers must treat "" as "unconfirmed", not "definitely
    wrong": the warning still fires, just without alarming language, when
    no explicit signal exists either way."""
    rc = str(rec.get("reverse_charge") or "").strip().lower()
    if rc in ("yes", "y", "true", "1"):
        return "Reverse Charge Mechanism — buyer self-assesses tax, supplier charges none"
    notes = str(rec.get("notes") or "").lower()
    category = str(rec.get("category") or "").lower()
    combined = f"{notes} {category}"
    for kw, reason in _ZERO_TAX_SIGNALS.items():
        if kw in combined:
            return reason
    return ""


# ── Validation checks ────────────────────────────────────────────────

def validate(rec: dict, company_gstin: str = "") -> dict:
    checks, field_flags = [], set()

    def chk(cid, label, ok, level, detail=""):
        checks.append({"id": cid, "label": label, "ok": bool(ok),
                       "level": level, "detail": detail})
        return bool(ok)

    sg, rg = rec.get("supplier_gstin") or "", rec.get("receiver_gstin") or ""

    # HARD — GSTIN checksums
    if sg:
        ok = gstin_checksum_ok(sg)
        chk("gstin_supplier", "Supplier GSTIN checksum", ok, "hard",
            "" if ok else f"'{sg}' fails format/checksum — re-read from image")
        if not ok: field_flags.add("supplier_gstin")
    else:
        chk("gstin_supplier", "Supplier GSTIN checksum", False, "soft",
            "Supplier GSTIN missing (unregistered supplier?)")
        field_flags.add("supplier_gstin")

    if rg:
        ok = gstin_checksum_ok(rg)
        chk("gstin_receiver", "Receiver GSTIN checksum", ok, "hard",
            "" if ok else f"'{rg}' fails format/checksum — re-read from image")
        if not ok: field_flags.add("receiver_gstin")
    else:
        chk("gstin_receiver", "Receiver GSTIN checksum", False, "soft",
            "Receiver GSTIN missing (B2C invoice?)")
        field_flags.add("receiver_gstin")

    # HARD — totals reconciliation. Tolerance scales with invoice size —
    # a flat ₹1 is tight for a large, many-line invoice where independent
    # per-line paisa rounding can genuinely accumulate past ₹1 even when
    # every figure on the bill is correct. 0.05% keeps this proportionally
    # tight (still just ₹1 for anything under ₹2,000) while giving real
    # headroom exactly where it's needed — large invoices with many lines.
    def _rupee_tol(base): return max(TOL_RUPEE, abs(base) * 0.0005)
    inv_v = _n(rec.get("invoice_value"))
    comp  = _computed_total(rec)
    ok = abs(comp - inv_v) <= _rupee_tol(inv_v) and inv_v > 0
    chk("totals_match", "Invoice total = taxable + taxes ± adjustments", ok, "hard",
        "" if ok else f"Components sum to ₹{comp:,.2f} but invoice value is ₹{inv_v:,.2f} (Δ ₹{abs(comp-inv_v):,.2f})")
    if not ok:
        field_flags.update({"invoice_value", "taxable_value"})

    # HARD — line items vs root
    items = rec.get("line_items") or []
    # Does the invoice itemize tax PER LINE? (some carry per-line CGST/SGST/IGST;
    # many show only ONE consolidated tax total). Only run per-line tax checks
    # when the lines actually carry their own tax — otherwise the rate_slab_tax
    # check (further down) is the correct verifier.
    _line_tax_present = any(
        (_n(li.get("igst")) + _n(li.get("cgst")) + _n(li.get("sgst")) + _n(li.get("cess"))) > TOL_RUPEE
        for li in items) if items else False

    if items:
        sums = _li_sums(items)
        _root_taxable = _n(rec.get("taxable_value"))
        ok = abs(sums["taxable_value"] - _root_taxable) <= _rupee_tol(_root_taxable)
        chk("li_taxable_sum", "Σ line-item taxable = root taxable", ok, "hard",
            "" if ok else f"Line items sum to ₹{sums['taxable_value']:,.2f}, root says ₹{_n(rec.get('taxable_value')):,.2f}")
        if not ok: field_flags.add("taxable_value")

        # Per-line tax sum check — ONLY for invoices that itemize tax per line.
        if _line_tax_present:
            tax_ok, tax_msgs = True, []
            for k in ("igst", "cgst", "sgst", "cess"):
                if abs(sums[k] - _n(rec.get(k))) > TOL_TAX:
                    tax_ok = False
                    tax_msgs.append(f"{k.upper()}: items ₹{sums[k]:,.2f} vs root ₹{_n(rec.get(k)):,.2f}")
                    field_flags.add(k)
            chk("li_tax_sum", "Σ line-item taxes = root taxes", tax_ok, "hard",
                "; ".join(tax_msgs))

    # HARD — CGST == SGST
    cg, sgst = _n(rec.get("cgst")), _n(rec.get("sgst"))
    if cg > 0 or sgst > 0:
        ok = abs(cg - sgst) <= TOL_PAISA
        chk("cgst_eq_sgst", "CGST equals SGST", ok, "hard",
            "" if ok else f"CGST ₹{cg:,.2f} ≠ SGST ₹{sgst:,.2f} — one was misread")
        if not ok: field_flags.update({"cgst", "sgst"})

    # HARD — IGST xor local taxes
    ig = _n(rec.get("igst"))
    if ig > 0 and (cg > 0 or sgst > 0):
        chk("igst_xor_local", "IGST and CGST/SGST not both charged", False, "hard",
            f"Invoice has both IGST ₹{ig:,.2f} and CGST/SGST — impossible on one supply")
        field_flags.update({"igst", "cgst", "sgst"})
    else:
        chk("igst_xor_local", "IGST and CGST/SGST not both charged", True, "hard")

    # SOFT — interstate logic from GSTIN state codes
    ss, rs = clean_gstin(sg)[:2], clean_gstin(rg)[:2]
    if len(ss) == 2 and len(rs) == 2 and ss in STATE_CODES and rs in STATE_CODES \
            and (ig > 0 or cg > 0 or sgst > 0):
        interstate = ss != rs
        if interstate and ig == 0 and (cg > 0 or sgst > 0):
            chk("interstate_logic", "Tax type matches supply route", False, "soft",
                f"Supplier {STATE_CODES[ss]} → receiver {STATE_CODES[rs]} is interstate but CGST/SGST charged (check POS / bill-to-ship-to)")
        elif not interstate and ig > 0:
            chk("interstate_logic", "Tax type matches supply route", False, "soft",
                f"Both parties in {STATE_CODES[ss]} but IGST charged (check POS / bill-to-ship-to)")
        else:
            chk("interstate_logic", "Tax type matches supply route", True, "soft")

    # SOFT — place of supply vs receiver GSTIN state (documented but never
    # implemented before this fix). place_of_supply is free-text OCR, not a
    # code, so it's normalised via a name→code lookup before comparing.
    # Skipped entirely when place_of_supply is blank or unrecognisable —
    # never guesses, never flags on absence of data.
    pos_raw = (rec.get("place_of_supply") or "").strip()
    if pos_raw and len(rs) == 2 and rs in STATE_CODES:
        pos_code = _pos_to_state_code(pos_raw)
        if pos_code:
            ok = pos_code == rs
            pos_label = STATE_CODES.get(pos_code, pos_code)
            pos_desc = pos_raw if _norm_state_name(pos_raw) == _norm_state_name(pos_label) \
                else f"'{pos_raw}' ({pos_label})"
            chk("pos_state", "Place of supply matches receiver state", ok, "soft",
                "" if ok else
                f"Place of supply reads {pos_desc} but receiver GSTIN is "
                f"registered in {STATE_CODES[rs]} — verify which is correct")
            if not ok:
                field_flags.add("place_of_supply")

    # SOFT — per-line rate math (ONLY when the invoice itemizes tax per line)
    if items and _line_tax_present:
        bad = []
        for n_i, li in enumerate(items):
            stored_rate, tv = _n(li.get("gst_rate")), _n(li.get("taxable_value"))
            tax = _n(li.get("igst")) + _n(li.get("cgst")) + _n(li.get("sgst"))
            if stored_rate > 0 and tv > 0:
                # gst_rate was already normalised to the true combined
                # figure by auto_repair (steps 1a/3d) before validate()
                # ever runs — safe to use directly, no reinterpretation.
                expect = tv * stored_rate / 100
                if abs(tax - expect) > max(TOL_TAX, tv * 0.01):
                    bad.append(f"item {n_i+1}: {stored_rate:g}% of ₹{tv:,.2f} should be ₹{expect:,.2f}, got ₹{tax:,.2f}")
        chk("line_rate_math", "Per-line tax = rate × taxable", not bad, "soft",
            "; ".join(bad[:3]))

        # HARD — tax verification. Two kinds of invoice:
        #   (a) SEPARATE-tax: each line carries its own CGST/SGST/IGST. Those
        #       are the invoice's authoritative figures — verify the root tax
        #       against the SUM of the line taxes (take them as-is, don't force
        #       a theoretical recompute that rounding might disagree with).
        #   (b) CONSOLIDATED-tax: lines carry only rate+taxable, one tax total
        #       on the invoice. Compute Σ(slab_taxable × rate) and verify that.
        expected_tax, li_taxable, slabs, usable = _rate_slab_tax(items)
        line_tax_sum = round(sum(_n(li.get("igst")) + _n(li.get("cgst")) + _n(li.get("sgst"))
                                 for li in items), 2)
        root_tax = round(_n(rec.get("igst")) + _n(rec.get("cgst")) + _n(rec.get("sgst")), 2)
        # Forgiving tolerance: ₹2, or 1% of the tax for larger invoices.
        def _tax_tol(base): return max(TOL_TAX, base * 0.01)

        if line_tax_sum > TOL_RUPEE:
            # (a) separate-tax invoice — compare root to the invoice's own line taxes
            tol = _tax_tol(line_tax_sum)
            ok = abs(root_tax - line_tax_sum) <= tol
            chk("rate_slab_tax", "Invoice tax total = sum of line taxes", ok, "hard",
                "" if ok else (f"Line-wise taxes sum to ₹{line_tax_sum:,.2f} but the "
                               f"invoice tax total reads ₹{root_tax:,.2f} (±₹2 allowed). "
                               f"A line tax or the total was misread."))
            if not ok:
                field_flags.update({"cgst", "sgst", "igst"})
        elif usable and li_taxable > 0:
            # (b) consolidated-tax invoice — verify the theoretical slab tax
            tol = _tax_tol(expected_tax)
            ok = abs(root_tax - expected_tax) <= tol
            slab_desc = ", ".join(f"{r:g}%×₹{t:,.0f}" for r, t in sorted(slabs.items()))
            chk("rate_slab_tax", "Total tax = Σ(rate × taxable per slab)", ok, "hard",
                "" if ok else (f"Computed tax from rate slabs ({slab_desc}) = "
                               f"₹{expected_tax:,.2f}, but invoice tax totals = "
                               f"₹{root_tax:,.2f} (±₹2 allowed). A taxable value or the "
                               f"tax was misread."))
            if not ok:
                field_flags.update({"taxable_value", "cgst", "sgst", "igst"})

            # Also verify the line-item taxable sum equals the root taxable —
            # catches a missing/extra line item even with one consolidated tax.
            root_taxable = _n(rec.get("taxable_value"))
            if root_taxable > 0:
                ok2 = abs(li_taxable - root_taxable) <= max(TOL_RUPEE, root_taxable*0.005)
                chk("slab_taxable_sum", "Σ line taxable = invoice taxable", ok2, "soft",
                    "" if ok2 else (f"Line items' taxable sums to ₹{li_taxable:,.2f} "
                                    f"but invoice taxable shows ₹{root_taxable:,.2f}"))
                if not ok2:
                    field_flags.add("taxable_value")

    # SOFT — date sanity
    d = rec.get("invoice_date") or ""
    try:
        dt = datetime.strptime(d, "%Y-%m-%d").date()
        if dt > date.today():
            chk("date_sane", "Invoice date plausible", False, "soft",
                f"Date {d} is in the future")
            field_flags.add("invoice_date")
        elif dt < date.today() - timedelta(days=365 * 3):
            chk("date_sane", "Invoice date plausible", False, "soft",
                f"Date {d} is over 3 years old — ITC time-barred under s.16(4)?")
            field_flags.add("invoice_date")
        else:
            chk("date_sane", "Invoice date plausible", True, "soft")
    except Exception:
        chk("date_sane", "Invoice date plausible", False, "soft",
            f"Unparseable date '{d}'")
        field_flags.add("invoice_date")

    # SOFT — HSN format
    hsns = {str(li.get("hsn_sac") or "").strip() for li in items}
    hsns.add(str(rec.get("hsn_sac") or "").strip())
    bad_hsn = [h for h in hsns if h and not re.fullmatch(r"\d{4}|\d{6}|\d{8}", h)]
    chk("hsn_format", "HSN/SAC is 4/6/8 digits", not bad_hsn, "soft",
        ("Invalid: " + ", ".join(bad_hsn[:4])) if bad_hsn else "")

    # SOFT — PAN entity-type letter (4th char of the embedded PAN) must be
    # from the closed official set; an out-of-set letter is a misread at
    # GSTIN position 6. Informational label shown when valid.
    for _side, _g in (("supplier", sg), ("receiver", rg)):
        if len(clean_gstin(_g)) == 15:
            _ch, _lbl = pan_entity_of_gstin(_g)
            chk(f"pan_type_{_side}", f"PAN entity letter ({_side})", bool(_lbl), "soft",
                f"{_ch} → {_lbl}" if _lbl else
                f"'{_ch}' is not a valid PAN category (P/C/H/F/A/T/B/L/J/G) — "
                f"character 6 of the GSTIN may be misread")
            if not _lbl:
                field_flags.add(f"{_side}_gstin")

    # SOFT — taxes present: taxable value without ANY GST is the signature
    # of a totals page that never got read — UNLESS this is one of several
    # invoice types where zero tax is legally correct: Reverse Charge
    # (buyer self-assesses), export/SEZ under LUT or Bond, a composition-
    # scheme supplier (barred from charging GST at all), or a nil-rated/
    # exempt supply. Previously every one of these correct, common invoice
    # types was flagged as if something had gone wrong.
    _tv  = _n(rec.get("taxable_value"))
    _tax = _n(rec.get("igst")) + _n(rec.get("cgst")) + _n(rec.get("sgst"))
    if _tv > 0:
        _zero_reason = "" if _tax > 0 else _zero_tax_reason(rec)
        ok = _tax > 0 or bool(_zero_reason)
        chk("taxes_present", "GST amounts present", ok, "soft",
            "" if ok else
            "Taxable value found but ZERO GST extracted — totals page may not "
            "have been read (or this is an exempt supply). Check CGST/SGST/IGST.")
        if not ok:
            field_flags.update({"cgst", "sgst", "igst"})

    # SOFT — tax figure present but UNVERIFIED: this fires specifically for
    # single-rate invoices where the GST rate is printed once for the whole
    # document instead of per line item — a common, legitimate layout. In
    # that case no line item carries a gst_rate, so rate_slab_tax/li_tax_sum
    # never run (there's nothing to compute a rate slab from) and this
    # number is trusted from raw OCR alone, with zero mathematical cross-
    # check. Not a failure — an honest signal so a reviewer knows to glance
    # at this one instead of assuming "verified" covered it.
    if items and _tv > 0 and _tax > 0 and not _line_tax_present:
        _, _, _, _slab_usable = _rate_slab_tax(items)
        if not _slab_usable:
            chk("tax_unverified", "Tax figure verified against line items", False, "soft",
                "No line item has a GST rate filled in (common when the rate is "
                "printed once for the whole invoice rather than per line) — the "
                "extracted tax total could not be mathematically checked. "
                "Please verify against the bill.")
            field_flags.update({"igst", "cgst", "sgst"})

    # SOFT — invoice number present
    inv_n = (rec.get("invoice_number") or "").strip()
    chk("inv_number", "Invoice number present", bool(inv_n), "soft",
        "" if inv_n else "No invoice number extracted")
    if not inv_n:
        field_flags.add("invoice_number")

    # SOFT — ground truth: company's own GSTIN must appear on one side
    cgc = clean_gstin(company_gstin)
    if len(cgc) == 15 and gstin_checksum_ok(cgc):
        found = clean_gstin(sg) == cgc or clean_gstin(rg) == cgc
        chk("company_gstin", "Your company GSTIN on invoice", found, "soft",
            "" if found else
            f"{cgc} not found on either side — supplier/receiver may be SWAPPED or misread")
        if not found:
            field_flags.update({"supplier_gstin", "receiver_gstin"})

    # ── Score & status ────────────────────────────────────────────────
    hard = [c for c in checks if c["level"] == "hard"]
    soft = [c for c in checks if c["level"] == "soft"]
    hard_fail = sum(1 for c in hard if not c["ok"])
    soft_fail = sum(1 for c in soft if not c["ok"])
    score = max(0, 100 - hard_fail * 25 - soft_fail * 7)
    status = "failed" if hard_fail else ("review" if soft_fail else "verified")

    return {"status": status, "score": score, "checks": checks,
            "field_flags": sorted(field_flags)}


def validate_and_repair(rec: dict, repair: bool = True,
                        company_gstin: str = "") -> dict:
    """Main entry point: optionally auto-repair, then validate.
    company_gstin: the uploading company's own GSTIN — a deterministic
    anchor for near-miss repair and side-presence checking."""
    repairs = auto_repair(rec, company_gstin) if repair else []
    result  = validate(rec, company_gstin)
    result["repairs"] = repairs
    return result
