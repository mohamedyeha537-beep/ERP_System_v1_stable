"""حساب صافي الربح بعد المصاريف التشغيلية."""
from __future__ import annotations

from decimal import Decimal


def calc_net_profit(
    gross_profit: Decimal,
    *,
    expenses_total: Decimal,
    rec_period_share: Decimal,
    operating_assets_expense: Decimal,
    loyalty_dinar_cost: Decimal = Decimal("0"),
) -> Decimal:
    """صافي الربح = إجمالي − مصاريف − ثابتة − أصول − تكلفة نقاط ولاء."""
    return (
        gross_profit
        - expenses_total
        - rec_period_share
        - operating_assets_expense
        - loyalty_dinar_cost
    ).quantize(Decimal("0.001"))
