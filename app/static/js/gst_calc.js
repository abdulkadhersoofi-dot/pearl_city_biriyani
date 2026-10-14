/*
 * Client-side mirror of app/utils/gst.py compute_line - the POS terminal,
 * invoice form and note form all preview totals through this one function
 * so what a user sees before saving is what the server will store, to the
 * paisa. The server recomputes everything regardless; this is preview only.
 */
(function () {
  // Half-up to 2 places without binary-float drift (1.005 -> 1.01, the
  // same as Python's Decimal ROUND_HALF_UP).
  function round2(x) {
    if (!isFinite(x) || Math.abs(x) < 1e-9) return 0;
    var sign = x < 0 ? -1 : 1;
    return sign * Number(Math.round(Number(Math.abs(x) + "e2")) + "e-2");
  }

  function line(qty, rate, discountPct, gstRate, inclusive, chargeGst, intraState) {
    qty = Number(qty) || 0;
    rate = Number(rate) || 0;
    discountPct = Number(discountPct) || 0;
    gstRate = chargeGst ? (Number(gstRate) || 0) : 0;
    if (intraState === undefined) intraState = true;

    var gross = qty * rate;
    var taxable, tax, discount;
    if (inclusive) {
      var grossIncl = round2(gross);
      var netIncl = round2(grossIncl - round2(grossIncl * discountPct / 100));
      taxable = round2(netIncl * 100 / (100 + gstRate));
      discount = round2(round2(grossIncl * 100 / (100 + gstRate)) - taxable);
      tax = round2(netIncl - taxable);
    } else {
      discount = round2(gross * discountPct / 100);
      taxable = round2(gross - discount);
      tax = round2(taxable * gstRate / 100);
    }

    var cgst = 0, sgst = 0, igst = 0;
    if (tax !== 0) {
      if (intraState) {
        if (inclusive) {
          cgst = round2(tax / 2);
          sgst = round2(tax - cgst);
        } else {
          cgst = sgst = round2(tax / 2);
        }
      } else {
        igst = tax;
      }
    }
    return {
      discount: discount,
      taxable: taxable,
      cgst: cgst,
      sgst: sgst,
      igst: igst,
      total: round2(taxable + cgst + sgst + igst),
    };
  }

  window.GstCalc = { round2: round2, line: line };
})();
