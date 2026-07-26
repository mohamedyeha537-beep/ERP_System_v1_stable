"""صفوف إغلاق وردية الفندق — متوقع مقابل معدود."""
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
    """متوقع الكاش/المصرف مثل المطعم: افتتاحي + تحصيل − مصروف.

    يعيد (expected_cash, expected_bank, cash_expenses, bank_expenses).
    """
    from modules.hotel.shift_expenses import (
        sum_hotel_shift_bank_expenses,
        sum_hotel_shift_cash_expenses,
    )

    opening = Decimal(str(getattr(shift, "opening_cash", None) or 0)).quantize(
        Decimal("0.001")
    )
    cash_exp = sum_hotel_shift_cash_expenses(db, shift.id)
    bank_exp = sum_hotel_shift_bank_expenses(db, shift.id)
    exp_cash = (
        opening
        + activity.booking_payments_cash
        + activity.meals_settled_cash
        - cash_exp
    ).quantize(Decimal("0.001"))
    exp_bank = (
        activity.booking_payments_bank + activity.meals_settled_bank - bank_exp
    ).quantize(Decimal("0.001"))
    return exp_cash, exp_bank, cash_exp, bank_exp


def build_hotel_shift_close_rows(
    activity: HotelShiftActivitySummary,
    *,
    expected_cash: Decimal,
    expected_bank: Decimal,
    opening_cash: Decimal | None = None,
    cash_expenses: Decimal | None = None,
    bank_expenses: Decimal | None = None,
) -> list[HotelShiftCloseRow]:
    oc = Decimal(str(opening_cash or 0)).quantize(Decimal("0.001"))
    cx = Decimal(str(cash_expenses or 0)).quantize(Decimal("0.001"))
    bx = Decimal(str(bank_expenses or 0)).quantize(Decimal("0.001"))
    cash_sub = (
        f"افتتاحي {oc} + تحصيل كاش "
        f"{(activity.booking_payments_cash + activity.meals_settled_cash).quantize(Decimal('0.001'))}"
        f" − مصروف كاش {cx}"
    )
    bank_sub = (
        f"تحصيل مصرف "
        f"{(activity.booking_payments_bank + activity.meals_settled_bank).quantize(Decimal('0.001'))}"
        f" − مصروف مصرف {bx}"
    )
    rows: list[HotelShiftCloseRow] = [
        HotelShiftCloseRow(
            key="cash",
            name_ar="كاش الاستقبال",
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
            name_ar="مصرف / تحويل",
            subtitle=bank_sub,
            kind="bank",
            closable=True,
            require_count=True,
            is_count=False,
            expected=expected_bank,
            sales_total=expected_bank,
        ),
        HotelShiftCloseRow(
            key="bookings",
            name_ar="عمليات الحجز",
            subtitle=(
                f"حجوزات جديدة {activity.bookings_created} · "
                f"وصول {activity.checkins} · مغادرة {activity.checkouts}"
            ),
            kind="bookings",
            closable=True,
            require_count=True,
            is_count=True,
            expected=activity.expected_booking_ops,
        ),
        HotelShiftCloseRow(
            key="meals",
            name_ar="وجبات الغرف (مُسوّاة)",
            subtitle=f"إجمالي {activity.meals_settled_total} د.ل",
            kind="meals",
            closable=True,
            require_count=True,
            is_count=True,
            expected=activity.meals_settled_count,
        ),
        HotelShiftCloseRow(
            key="laundry",
            name_ar="طلبات المغسلة",
            subtitle=f"إجمالي {activity.laundry_total} د.ل",
            kind="laundry",
            closable=True,
            require_count=True,
            is_count=True,
            expected=activity.laundry_count,
        ),
        HotelShiftCloseRow(
            key="services",
            name_ar="خدمات إضافية",
            subtitle=f"إجمالي {activity.services_total} د.ل",
            kind="services",
            closable=True,
            require_count=True,
            is_count=True,
            expected=activity.services_count,
        ),
        HotelShiftCloseRow(
            key="booking_payments",
            name_ar="قبض حجوزات",
            subtitle=f"{activity.booking_payment_count} حركة",
            kind="info",
            closable=False,
            require_count=False,
            is_count=False,
            expected=0,
            amount=(
                activity.booking_payments_cash + activity.booking_payments_bank
            ).quantize(Decimal("0.001")),
        ),
    ]
    return rows
