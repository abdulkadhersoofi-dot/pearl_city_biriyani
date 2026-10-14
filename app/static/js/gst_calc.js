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

  // discountFlat: rupees off on the line's own price basis, after the %
  // (app.utils.gst.compute_line's discount_flat).
  function line(qty, rate, discountPct, gstRate, inclusive, chargeGst, intraState, discountFlat) {
    qty = Number(qty) || 0;
    rate = Number(rate) || 0;
    discountPct = Number(discountPct) || 0;
    discountFlat = Number(discountFlat) || 0;
    gstRate = chargeGst ? (Number(gstRate) || 0) : 0;
    if (intraState === undefined) intraState = true;

    var gross = qty * rate;
    var taxable, tax, discount;
    if (inclusive) {
      var grossIncl = round2(gross);
      var netIncl = round2(grossIncl - round2(grossIncl * discountPct / 100) - discountFlat);
      taxable = round2(netIncl * 100 / (100 + gstRate));
      discount = round2(round2(grossIncl * 100 / (100 + gstRate)) - taxable);
      tax = round2(netIncl - taxable);
    } else {
      discount = round2(round2(gross * discountPct / 100) + round2(discountFlat));
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

  // app.utils.gst.price_basis_amount
  function priceBasis(payable, gstRate, inclusive, chargeGst) {
    payable = Number(payable) || 0;
    if (inclusive || !chargeGst) return round2(payable);
    return round2(payable * 100 / (100 + (Number(gstRate) || 0)));
  }

  // app.utils.gst.allocate_amount - remainder on the heaviest line.
  function allocate(weights, amount) {
    amount = round2(Number(amount) || 0);
    var total = weights.reduce(function (s, w) { return s + (Number(w) || 0); }, 0);
    if (!weights.length || total <= 0 || amount === 0) return weights.map(function () { return 0; });
    var parts = weights.map(function (w) { return round2(amount * (Number(w) || 0) / total); });
    var heaviest = 0;
    weights.forEach(function (w, i) { if ((Number(w) || 0) > (Number(weights[heaviest]) || 0)) heaviest = i; });
    var sum = parts.reduce(function (s, p) { return s + p; }, 0);
    parts[heaviest] = round2(parts[heaviest] + amount - sum);
    return parts;
  }

  window.GstCalc = { round2: round2, line: line, priceBasis: priceBasis, allocate: allocate };
})();
