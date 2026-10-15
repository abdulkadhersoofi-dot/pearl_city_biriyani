"""Advances in the GST returns (feature "receivables" only):

  GSTR-1 11A - advances received in the month on which tax is payable
               (advance receipt vouchers for services), by place of supply
               and rate.
  GSTR-1 11B - advances on which tax was paid in an earlier entry and
               which were set against an invoice (adjusted) or given back
               on a refund voucher in the month.
  GSTR-3B 3.1(a) - the net (11A - 11B) is added to outward taxable supplies.

The tax in each adjustment / refund comes from app.accounts.services.
advance_usage (cumulative, so it never exceeds what was paid). An
advance with no GST on it (goods, or a business not registered as
regular) appears nowhere here."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.models.accounts import CustomerReceipt, VoucherStatus

ZERO = Decimal("0.00")


@dataclass
class AdvanceBucket:
    place_of_supply: str
    gst_rate: Decimal
    taxable_value: Decimal = ZERO
    cgst: Decimal = ZERO
    sgst: Decimal = ZERO
    igst: Decimal = ZERO
    count: int = 0

    @property
    def tax(self):
        return self.cgst + self.sgst + self.igst


def _add(buckets: dict, receipt, part: dict):
    key = (receipt.place_of_supply_state_code or "", Decimal(receipt.gst_rate))
    b = buckets.setdefault(key, AdvanceBucket(place_of_supply=key[0], gst_rate=key[1]))
    b.taxable_value += part["taxable_value"]
    b.cgst += part["cgst_amount"]
    b.sgst += part["sgst_amount"]
    b.igst += part["igst_amount"]
    b.count += 1


def _totals(buckets: list[AdvanceBucket]) -> dict:
    return {k: sum((getattr(b, k) for b in buckets), ZERO) for k in ("taxable_value", "cgst", "sgst", "igst")}


def advances_data(tenant, gstin, start: date, end: date) -> dict:
    from app.accounts.services import advance_usage

    receipts = CustomerReceipt.query.filter(
        CustomerReceipt.tenant_id == tenant.id, CustomerReceipt.gstin_id == gstin.id,
        CustomerReceipt.is_advance.is_(True), CustomerReceipt.status == VoucherStatus.POSTED,
        CustomerReceipt.voucher_date <= end,
    ).all()
    received: dict = {}
    adjusted: dict = {}
    for r in receipts:
        if not r.total_tax:
            continue
        if start <= r.voucher_date <= end:
            _add(received, r, {"taxable_value": Decimal(r.taxable_value), "cgst_amount": Decimal(r.cgst_amount),
                               "sgst_amount": Decimal(r.sgst_amount), "igst_amount": Decimal(r.igst_amount)})
        for use in advance_usage(r):
            if start <= use["date"] <= end:
                _add(adjusted, r, use)
    received_rows = sorted(received.values(), key=lambda b: (b.place_of_supply, b.gst_rate))
    adjusted_rows = sorted(adjusted.values(), key=lambda b: (b.place_of_supply, b.gst_rate))
    rt, at = _totals(received_rows), _totals(adjusted_rows)
    return {"received": received_rows, "adjusted": adjusted_rows, "received_totals": rt, "adjusted_totals": at,
            "net": {k: rt[k] - at[k] for k in rt}}
