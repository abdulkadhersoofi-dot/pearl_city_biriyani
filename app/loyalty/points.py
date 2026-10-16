"""Loyalty points (module "loyalty") - the one place points are worked out.

  earn:   floor(amount paid / 100 x points_per_100) on a counter bill with a
          customer on it - the amount paid excludes what was paid with
          points, exchange credit or old gold.
  spend:  points are a way to pay (PaymentMode.LOYALTY): Rs. amount needs
          ceil(amount / point_value) points; at least min_redeem points
          must be held to spend any. Like any payment it comes off the
          amount payable - GST is on the bill's full value.
  undo:   a voided bill gives back the points spent and takes back the
          points earned; a sales return takes back the points earned on
          the value returned.
The balance is the sum of a customer's LoyaltyEntry rows."""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from sqlalchemy import func

from app.extensions import db
from app.models.business import LoyaltyEntry
from app.models.pos_bill import PaymentMode
from app.utils.module_settings import dec, module_settings

NOT_EARNING = (PaymentMode.LOYALTY, PaymentMode.EXCHANGE, PaymentMode.OLD_GOLD)


class LoyaltyError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def settings(tenant) -> dict:
    s = module_settings(tenant, "loyalty")
    return {"points_per_100": dec(s["points_per_100"], "1"), "point_value": dec(s["point_value"], "1"),
            "min_redeem": int(s.get("min_redeem") or 0)}


def balance(tenant_id: int, customer_id: int) -> int:
    total = (db.session.query(func.coalesce(func.sum(LoyaltyEntry.points), 0))
             .filter(LoyaltyEntry.tenant_id == tenant_id, LoyaltyEntry.customer_id == customer_id).scalar())
    return int(total or 0)


def points_earned(tenant, amount_paid) -> int:
    s = settings(tenant)
    if amount_paid <= 0 or s["points_per_100"] <= 0:
        return 0
    return int((Decimal(amount_paid) / 100 * s["points_per_100"]).to_integral_value(rounding=ROUND_FLOOR))


def points_needed(tenant, amount) -> int:
    s = settings(tenant)
    if s["point_value"] <= 0:
        raise LoyaltyError("Points can't be spent - set what a point is worth in Loyalty settings.")
    return int((Decimal(amount) / s["point_value"]).to_integral_value(rounding=ROUND_CEILING))


def check_spend(tenant, customer, amount) -> int:
    if customer is None:
        raise LoyaltyError("Add the customer to the bill to use their points.")
    need = points_needed(tenant, amount)
    have = balance(tenant.id, customer.id)
    s = settings(tenant)
    if have < s["min_redeem"]:
        raise LoyaltyError(f"{customer.name} has {have} points - at least {s['min_redeem']} are needed to spend any.")
    if need > have:
        raise LoyaltyError(f"{customer.name} has {have} points - Rs. {Decimal(amount):.2f} needs {need}.")
    return need


def record_bill(tenant, bill, user_id) -> None:
    """Points spent and earned on a just-completed counter bill."""
    if not bill.customer_id:
        return
    spent = sum((p.amount for p in bill.payments if p.mode == PaymentMode.LOYALTY), Decimal("0"))
    if spent:
        db.session.add(LoyaltyEntry(tenant_id=tenant.id, customer_id=bill.customer_id, points=-points_needed(tenant, spent),
                                    kind="redeem", source_type="pos_bill", source_id=bill.id, created_by_id=user_id,
                                    note=f"Spent on {bill.bill_number}"))
    paid = sum((p.amount for p in bill.payments if p.mode not in NOT_EARNING), Decimal("0"))
    earned = points_earned(tenant, paid)
    if earned:
        db.session.add(LoyaltyEntry(tenant_id=tenant.id, customer_id=bill.customer_id, points=earned, kind="earn",
                                    source_type="pos_bill", source_id=bill.id, created_by_id=user_id,
                                    note=f"Earned on {bill.bill_number}"))


def reverse_bill(tenant_id: int, bill, user_id, note: str) -> None:
    """Undo every point movement of a voided / refunded bill."""
    rows = LoyaltyEntry.query.filter_by(tenant_id=tenant_id, source_type="pos_bill", source_id=bill.id).all()
    net = sum(r.points for r in rows)
    if net:
        db.session.add(LoyaltyEntry(tenant_id=tenant_id, customer_id=bill.customer_id, points=-net, kind="reverse",
                                    source_type="pos_bill", source_id=bill.id, created_by_id=user_id, note=note))


def reverse_return(tenant, ret, user_id) -> None:
    """A sales return takes back the points earned on the value returned."""
    bill = ret.bill
    if not bill.customer_id or not bill.grand_total:
        return
    earned = sum(r.points for r in LoyaltyEntry.query.filter_by(tenant_id=tenant.id, source_type="pos_bill",
                                                                 source_id=bill.id, kind="earn"))
    if earned <= 0:
        return
    back = int((Decimal(earned) * Decimal(ret.grand_total) / Decimal(bill.grand_total))
               .to_integral_value(rounding=ROUND_FLOOR))
    if back:
        db.session.add(LoyaltyEntry(tenant_id=tenant.id, customer_id=bill.customer_id, points=-back, kind="reverse",
                                    source_type="pos_return", source_id=ret.id, created_by_id=user_id,
                                    note=f"Return {ret.return_number}"))
