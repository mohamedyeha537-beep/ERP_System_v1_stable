"""صفوف إغلاق الجلسة — كاش/مصرف (عدّ) + شقق/توصيل/ولاء (عرض فقط)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from modules.pos_shifts.service import compute_shift_financial_summary


@dataclass
class ShiftCloseRow:
    key: str
    name_ar: str
    subtitle: str
    closable: bool
    #: يُعرض في خانة «المتوقع» — حصيلة مبيعات الفواتير
    sales_total: Decimal | None = None
    #: يُقارَن بالمعدود لحساب العجز (صافي الدرج/المحفظة)
    close_expected: Decimal | None = None
    amount: Decimal | None = None
    kind: str | None = None
    require_count: bool = False


def _m(d: Decimal) -> str:
    return f"{d.quantize(Decimal('0.001')):.3f}"


def build_shift_close_rows(db: Session, shift_id: int) -> list[ShiftCloseRow]:
    fin = compute_shift_financial_summary(db, shift_id)

    cash_sub = f"صافي الدرج {_m(fin.expected_cash_drawer)} د.ل"
    if fin.opening_cash and fin.opening_cash > 0:
        cash_sub += f" (فكة افتتاح {_m(fin.opening_cash)} + مبيعات كاش {_m(fin.cash_sales)} − خصومات)"
    else:
        cash_sub += " بعد خصم التوصيل والمصروفات والمرتجعات"
    bank_sub = f"صافي المحفظة {_m(fin.expected_bank)} د.ل"
    if fin.bank_refunds > 0:
        bank_sub += f" (بعد مرتجعات −{_m(fin.bank_refunds)})"

    loyalty_sub = (
        f"{fin.loyalty_redeem_count} عملية خصم — {_m(fin.loyalty_points_redeemed)} نقطة"
        if fin.loyalty_redeem_count
        else "لا توجد عمليات خصم نقاط في هذه الجلسة"
    )

    return [
        ShiftCloseRow(
            key="cash",
            name_ar="خزينة الكاش",
            subtitle=cash_sub,
            closable=True,
            sales_total=fin.cash_sales,
            close_expected=fin.expected_cash_drawer,
            kind="CASH",
        ),
        ShiftCloseRow(
            key="bank",
            name_ar="خزينة المصرف",
            subtitle=bank_sub + " — أدخل المجموع بعد مراجعة فواتير المصرف",
            closable=True,
            sales_total=fin.bank_sales,
            close_expected=fin.expected_bank,
            kind="BANK",
            require_count=True,
        ),
        ShiftCloseRow(
            key="room",
            name_ar="حساب مبيعات الشقق",
            subtitle="راجع فواتير «قيد على الشقة» وأدخل إجماليها — إلزامي",
            closable=True,
            sales_total=fin.room_account_sales,
            close_expected=fin.room_account_sales,
            kind="ROOM",
            require_count=True,
        ),
        ShiftCloseRow(
            key="delivery",
            name_ar="حساب التوصيل",
            subtitle="مصروفات دُفعت للسائق من الكاش خلال الجلسة",
            closable=False,
            amount=fin.delivery_expenses,
        ),
        ShiftCloseRow(
            key="shift_expense",
            name_ar="مصروفات الجلسة",
            subtitle="صرف مسجّل من الكاشير أثناء الجلسة (كاش + مصرف)",
            closable=False,
            amount=(fin.shift_cash_expenses + fin.shift_bank_expenses).quantize(
                Decimal("0.001")
            ),
        ),
        ShiftCloseRow(
            key="loyalty",
            name_ar="نقاط الولاء",
            subtitle=loyalty_sub,
            closable=False,
            amount=fin.loyalty_dinar_cost,
        ),
    ]
