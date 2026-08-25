"""صفوف إغلاق وردية الفندق — كاش ومصرف فقط."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from modules.hotel.shift_activity import HotelShiftActivitySummary


@dataclass(frozen=True)
class HotelShiftCloseRow:
    key: str
    name_ar: str
    subtitle: str
    kind: str
    closable: bool
    require_count: bool
    is_count: bool
    expected: Decimal | int
    sales_total: Decimal = Decimal("0")
    amount: Decimal = Decimal("0")


def hotel_shift_expected_drawers(
    db,
    shift,
    activity: HotelShiftActivitySummary,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """متوقع الكاش/المصرف: افتتاحي + تحصيل الجلسة − مصروف.

    يعيد (expected_cash, expected_bank, cash_expenses, bank_expenses).
    """
    from modules.hotel.shift_expenses import (
        sum_hotel_shift_bank_expenses,
        sum_hotel_shift_cash_expenses,
    )

    opening = Decimal(str(getattr(shift, "opening_cash", None) or 0)).quantize(
        Decimal("0.001")
    )
    opening_bank = Decimal(str(getattr(shift, "opening_bank", None) or 0)).quantize(
        Decimal("0.001")
    )
    cash_exp = sum_hotel_shift_cash_expenses(db, shift.id)
    bank_exp = sum_hotel_shift_bank_expenses(db, shift.id)
    # تحصيلات الجلسة (قبض حجوزات + أي قبض نقدي مرتبط بالتسوية) − مصروفات
    cash_in = (
        activity.booking_payments_cash + activity.meals_settled_cash
    ).quantize(Decimal("0.001"))
    bank_in = (
        activity.booking_payments_bank + activity.meals_settled_bank
    ).quantize(Decimal("0.001"))
    exp_cash = (opening + cash_in - cash_exp).quantize(Decimal("0.001"))
    exp_bank = (opening_bank + bank_in - bank_exp).quantize(Decimal("0.001"))
    return exp_cash, exp_bank, cash_exp, bank_exp


def build_hotel_shift_close_rows(
    activity: HotelShiftActivitySummary,
    *,
    expected_cash: Decimal,
    expected_bank: Decimal,
    opening_cash: Decimal | None = None,
    opening_bank: Decimal | None = None,
    cash_expenses: Decimal | None = None,
    bank_expenses: Decimal | None = None,
) -> list[HotelShiftCloseRow]:
    """بنود العد عند الإقفال: رصيد الكاش + رصيد المصرف فقط."""
    oc = Decimal(str(opening_cash or 0)).quantize(Decimal("0.001"))
    ob = Decimal(str(opening_bank or 0)).quantize(Decimal("0.001"))
    cx = Decimal(str(cash_expenses or 0)).quantize(Decimal("0.001"))
    bx = Decimal(str(bank_expenses or 0)).quantize(Decimal("0.001"))
    cash_in = (
        activity.booking_payments_cash + activity.meals_settled_cash
    ).quantize(Decimal("0.001"))
    bank_in = (
        activity.booking_payments_bank + activity.meals_settled_bank
    ).quantize(Decimal("0.001"))
    if cash_in < 0:
        cash_sub = f"افتتاحي {oc} − صرف/استرداد الجلسة {abs(cash_in)} − مصروف كاش {cx}"
    else:
        cash_sub = f"افتتاحي {oc} + قبض الجلسة {cash_in} − مصروف كاش {cx}"
    if bank_in < 0:
        bank_sub = f"افتتاحي {ob} − صرف/استرداد الجلسة {abs(bank_in)} − مصروف مصرف {bx}"
    else:
        bank_sub = f"افتتاحي {ob} + قبض الجلسة {bank_in} − مصروف مصرف {bx}"
    return [
        HotelShiftCloseRow(
            key="cash",
            name_ar="رصيد الكاش",
            subtitle=cash_sub,
            kind="cash",
            closable=True,
            require_count=True,
            is_count=False,
            expected=expected_cash,
            sales_total=expected_cash,
        ),
        HotelShiftCloseRow(
            key="bank",
            name_ar="رصيد المصرف",
            subtitle=bank_sub,
            kind="bank",
            closable=True,
            require_count=True,
            is_count=False,
            expected=expected_bank,
            sales_total=expected_bank,
        ),
    ]
