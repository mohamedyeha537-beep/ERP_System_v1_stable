"""فتح وإقفال ورديات الفندق."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.datetime_local import ensure_utc, format_local_dt, now_local
from modules.hotel.revenue_stats import hotel_cash_collected, hotel_collections_by_payment_method
from modules.hotel.shift_expenses import list_shift_expenses, sum_shift_expenses
from modules.hotel.shift_models import HotelShift, HotelShiftError, HotelShiftStatus
from modules.hotel.shift_schedules import active_shift_slot, load_shift_schedules


@dataclass
class HotelShiftFinancialSummary:
    total_revenue: Decimal
    total_expenses: Decimal
    net_total: Decimal
    cash_collected: Decimal
    bank_collected: Decimal
    payment_count: int
    expense_count: int
    opened_at: datetime
    closed_at: datetime | None


def get_shift(db: Session, shift_id: int) -> HotelShift | None:
    return db.get(HotelShift, shift_id)


def get_open_shift(db: Session, *, property_id: int = 1) -> HotelShift | None:
    return db.scalar(
        select(HotelShift)
        .where(
            HotelShift.property_id == property_id,
            HotelShift.status == HotelShiftStatus.OPEN,
        )
        .options(selectinload(HotelShift.user), selectinload(HotelShift.employee))
        .order_by(HotelShift.id.desc())
        .limit(1)
    )


@dataclass
class HotelShiftIndexRow:
    shift_id: int
    shift_number: int
    shift_name_ar: str
    status: str
    opened_at: datetime | None
    closed_at: datetime | None
    operator_label: str
    user_label: str
    total_revenue: Decimal
    total_expenses: Decimal
    net_total: Decimal
    payment_count: int
    expense_count: int
    counted_cash: Decimal | None = None
    counted_bank: Decimal | None = None
    shortage_total: Decimal = Decimal("0")
    surplus_total: Decimal = Decimal("0")


def _hotel_shift_operator_label(shift: HotelShift) -> str:
    if getattr(shift, "employee", None) is not None and shift.employee.full_name_ar:
        return shift.employee.full_name_ar
    if getattr(shift, "user", None) is not None and shift.user.username:
        return shift.user.username
    return f"وردية #{shift.id}"


def list_hotel_shifts_index(
    db: Session,
    *,
    status_filter: str = "all",
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    employee_id: int | None = None,
    user_id: int | None = None,
    property_id: int = 1,
    limit: int = 40,
    offset: int = 0,
) -> tuple[list[HotelShiftIndexRow], int]:
    base = select(HotelShift).where(HotelShift.property_id == property_id)
    count_q = select(func.count()).select_from(HotelShift).where(
        HotelShift.property_id == property_id
    )
    if status_filter == "open":
        base = base.where(HotelShift.status == HotelShiftStatus.OPEN)
        count_q = count_q.where(HotelShift.status == HotelShiftStatus.OPEN)
    elif status_filter == "closed":
        base = base.where(HotelShift.status == HotelShiftStatus.CLOSED)
        count_q = count_q.where(HotelShift.status == HotelShiftStatus.CLOSED)
    if date_from is not None:
        base = base.where(HotelShift.opened_at >= date_from)
        count_q = count_q.where(HotelShift.opened_at >= date_from)
    if date_to is not None:
        base = base.where(HotelShift.opened_at < date_to)
        count_q = count_q.where(HotelShift.opened_at < date_to)
    if employee_id is not None:
        base = base.where(HotelShift.employee_id == employee_id)
        count_q = count_q.where(HotelShift.employee_id == employee_id)
    if user_id is not None:
        base = base.where(HotelShift.user_id == user_id)
        count_q = count_q.where(HotelShift.user_id == user_id)

    total = int(db.execute(count_q).scalar_one() or 0)
    shifts = list(
        db.scalars(
            base.options(selectinload(HotelShift.user), selectinload(HotelShift.employee))
            .order_by(HotelShift.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
    )
    rows: list[HotelShiftIndexRow] = []
    for sh in shifts:
        rev = Decimal(str(sh.total_revenue or 0)).quantize(Decimal("0.001"))
        exp = Decimal(str(sh.total_expenses or 0)).quantize(Decimal("0.001"))
        if sh.status == HotelShiftStatus.OPEN:
            live = compute_shift_summary(db, sh)
            rev = live.total_revenue
            exp = live.total_expenses
        net = (rev - exp).quantize(Decimal("0.001"))
        user_label = (sh.user.username if sh.user else "") or "—"
        cnt_cash = (
            Decimal(str(sh.counted_cash)).quantize(Decimal("0.001"))
            if sh.counted_cash is not None
            else None
        )
        cnt_bank = (
            Decimal(str(sh.counted_bank)).quantize(Decimal("0.001"))
            if sh.counted_bank is not None
            else None
        )
        cash_diff = Decimal(str(sh.cash_difference or 0))
        bank_diff = Decimal(str(sh.bank_difference or 0))
        shortage = Decimal("0")
        surplus = Decimal("0")
        for d in (cash_diff, bank_diff):
            if d < 0:
                shortage += -d
            elif d > 0:
                surplus += d
        pay_cnt = int(sh.payment_count or 0)
        exp_cnt = int(sh.expense_count or 0)
        if sh.status == HotelShiftStatus.OPEN:
            pay_cnt = live.payment_count
            exp_cnt = live.expense_count
        rows.append(
            HotelShiftIndexRow(
                shift_id=sh.id,
                shift_number=int(sh.shift_number or 0),
                shift_name_ar=sh.shift_name_ar,
                status=sh.status.value,
                opened_at=sh.opened_at,
                closed_at=sh.closed_at,
                operator_label=_hotel_shift_operator_label(sh),
                user_label=user_label,
                total_revenue=rev,
                total_expenses=exp,
                net_total=net,
                payment_count=pay_cnt,
                expense_count=exp_cnt,
                counted_cash=cnt_cash,
                counted_bank=cnt_bank,
                shortage_total=shortage.quantize(Decimal("0.001")),
                surplus_total=surplus.quantize(Decimal("0.001")),
            )
        )
    return rows, total


def list_stale_open_hotel_shifts(
    db: Session, *, stale_hours: float = 24.0, property_id: int = 1
) -> list[HotelShift]:
    """جلسات فندق مفتوحة منذ أكثر من N ساعة (مثل جلسات الكاشير)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=stale_hours)
    return list(
        db.scalars(
            select(HotelShift)
            .where(
                HotelShift.property_id == property_id,
                HotelShift.status == HotelShiftStatus.OPEN,
                HotelShift.opened_at <= cutoff,
            )
            .options(selectinload(HotelShift.user), selectinload(HotelShift.employee))
            .order_by(HotelShift.opened_at.asc())
        ).all()
    )


def admin_close_hotel_shift(
    db: Session,
    shift_id: int,
    *,
    user_id: int,
    note: str | None = None,
) -> HotelShift:
    """إغلاق إداري لجلسة عالقة — يُعدّ بالمتوقع دون إدخال يدوي."""
    from modules.hotel.shift_activity import compute_hotel_shift_activity
    from modules.hotel.shift_close_rows import hotel_shift_expected_drawers

    shift = db.get(HotelShift, shift_id)
    if shift is None:
        raise HotelShiftError("الوردية غير موجودة.")
    if shift.status != HotelShiftStatus.OPEN:
        raise HotelShiftError("الوردية مغلقة بالفعل.")
    now = datetime.now(timezone.utc)
    activity = compute_hotel_shift_activity(db, ensure_utc(shift.opened_at), now)
    exp_cash, exp_bank, _, _ = hotel_shift_expected_drawers(db, shift, activity)
    return close_shift(
        db,
        shift_id,
        user_id=user_id,
        closing_note=(note or "").strip() or "إغلاق إداري — جلسة عالقة",
        counted_cash=exp_cash,
        counted_bank=exp_bank,
        counted_bookings=activity.expected_booking_ops,
        counted_meals=activity.meals_settled_count,
        counted_laundry=activity.laundry_count,
        counted_services=activity.services_count,
        require_counts=False,
    )


def list_recent_shifts(db: Session, *, limit: int = 30, property_id: int = 1) -> list[HotelShift]:
    return list(
        db.scalars(
            select(HotelShift)
            .where(HotelShift.property_id == property_id)
            .options(
                selectinload(HotelShift.user),
                selectinload(HotelShift.closed_by),
                selectinload(HotelShift.employee),
            )
            .order_by(HotelShift.id.desc())
            .limit(limit)
        ).all()
    )


def compute_shift_summary(
    db: Session, shift: HotelShift, *, end_at: datetime | None = None
) -> HotelShiftFinancialSummary:
    start = ensure_utc(shift.opened_at)
    end = ensure_utc(end_at or shift.closed_at or datetime.now(timezone.utc))
    revenue = hotel_cash_collected(db, start, end)
    expenses = sum_shift_expenses(db, shift.id)
    expense_rows = list_shift_expenses(db, shift.id, limit=500)

    cash = Decimal("0")
    bank = Decimal("0")
    pay_count = 0
    for name, cnt, _pay, _ref_cnt, _ref, net in hotel_collections_by_payment_method(db, start, end):
        pay_count += int(cnt or 0)
        net_amt = Decimal(str(net or 0))
        low = (name or "").lower()
        if "كاش" in name or "cash" in low:
            cash += net_amt
        elif "مصرف" in name or "bank" in low:
            bank += net_amt
        else:
            cash += net_amt

    return HotelShiftFinancialSummary(
        total_revenue=revenue,
        total_expenses=expenses,
        net_total=(revenue - expenses).quantize(Decimal("0.001")),
        cash_collected=cash.quantize(Decimal("0.001")),
        bank_collected=bank.quantize(Decimal("0.001")),
        payment_count=pay_count,
        expense_count=len(expense_rows),
        opened_at=start,
        closed_at=end if shift.closed_at else None,
    )


def open_shift(
    db: Session,
    *,
    user_id: int,
    employee_id: int | None = None,
    opening_note: str | None = None,
    property_id: int = 1,
    shift_number: int | None = None,
    opening_cash: Decimal | str | None = None,
) -> HotelShift:
    if get_open_shift(db, property_id=property_id) is not None:
        raise HotelShiftError("يوجد جلسة مفتوحة — أقفلها أولاً قبل افتتاح جلسة جديدة.")

    slot = active_shift_slot(db)
    num = int(shift_number or slot.number)
    name = slot.name_ar
    for sch in load_shift_schedules(db):
        if int(sch["number"]) == num:
            name = sch["name_ar"]
            break

    start_utc = slot.scheduled_start.astimezone(timezone.utc)
    end_utc = slot.scheduled_end.astimezone(timezone.utc)

    shift = HotelShift(
        property_id=property_id,
        shift_number=num,
        shift_name_ar=name,
        status=HotelShiftStatus.OPEN,
        user_id=user_id,
        employee_id=employee_id,
        scheduled_start=start_utc,
        scheduled_end=end_utc,
        opening_note=(opening_note or "").strip() or None,
        opening_cash=Decimal(str(opening_cash or "0")).quantize(Decimal("0.001")),
    )
    db.add(shift)
    db.flush()
    from modules.authz.models import User

    user = db.get(User, user_id)
    operator = (user.username or "").strip() if user else ""
    try:
        from modules.notifications.hotel_shift_hooks import emit_hotel_shift_opened

        emit_hotel_shift_opened(db, shift, operator_name=operator)
    except Exception:  # noqa: BLE001
        pass
    return shift


def close_shift(
    db: Session,
    shift_id: int,
    *,
    user_id: int,
    closing_note: str | None = None,
    counted_cash: Decimal | str | None = None,
    counted_bank: Decimal | str | None = None,
    counted_bookings: int | str | None = None,
    counted_meals: int | str | None = None,
    counted_laundry: int | str | None = None,
    counted_services: int | str | None = None,
    require_counts: bool = True,
) -> HotelShift:
    import json

    from modules.hotel.shift_activity import compute_hotel_shift_activity
    from modules.hotel.shift_close_rows import hotel_shift_expected_drawers

    shift = db.get(HotelShift, shift_id)
    if shift is None:
        raise HotelShiftError("الوردية غير موجودة.")
    if shift.status != HotelShiftStatus.OPEN:
        raise HotelShiftError("الوردية مغلقة بالفعل.")

    if require_counts:
        if counted_cash is None or str(counted_cash).strip() == "":
            raise HotelShiftError(
                "أدخل المبلغ المعدود للكاش بعد عدّ الدرج — مثل إقفال جلسة المطعم."
            )
        if counted_bank is None or str(counted_bank).strip() == "":
            raise HotelShiftError(
                "أدخل المبلغ المعدود للمصرف/التحويل بعد مراجعة الإيصالات."
            )
        for label, raw in (
            ("عمليات الحجز", counted_bookings),
            ("الوجبات", counted_meals),
            ("المغسلة", counted_laundry),
            ("الخدمات", counted_services),
        ):
            if raw is None or str(raw).strip() == "":
                raise HotelShiftError(f"أدخل العدد المعدود لبند: {label}.")

    now = datetime.now(timezone.utc)
    summary = compute_shift_summary(db, shift, end_at=now)
    activity = compute_hotel_shift_activity(db, ensure_utc(shift.opened_at), now)
    exp_cash, exp_bank, _, _ = hotel_shift_expected_drawers(db, shift, activity)

    try:
        cnt_cash = Decimal(str(counted_cash)).quantize(Decimal("0.001"))
        cnt_bank = Decimal(str(counted_bank)).quantize(Decimal("0.001"))
    except Exception as exc:
        raise HotelShiftError("مبالغ المعدود غير صالحة.") from exc
    if cnt_cash < 0 or cnt_bank < 0:
        raise HotelShiftError("المعدود لا يمكن أن يكون سالباً.")

    def _int_val(raw, default: int = 0) -> int:
        try:
            return max(0, int(str(raw if raw is not None else default)))
        except (TypeError, ValueError):
            return default

    cnt_book = _int_val(counted_bookings, activity.expected_booking_ops)
    cnt_meals = _int_val(counted_meals, activity.meals_settled_count)
    cnt_laundry = _int_val(counted_laundry, activity.laundry_count)
    cnt_services = _int_val(counted_services, activity.services_count)

    shift.status = HotelShiftStatus.CLOSED
    shift.closed_at = now
    shift.closed_by_id = user_id
    shift.closing_note = (closing_note or "").strip() or None
    shift.total_revenue = summary.total_revenue
    shift.total_expenses = summary.total_expenses
    shift.cash_collected = summary.cash_collected
    shift.bank_collected = summary.bank_collected
    shift.payment_count = summary.payment_count
    shift.expense_count = summary.expense_count
    shift.expected_cash = exp_cash
    shift.expected_bank = exp_bank
    shift.counted_cash = cnt_cash
    shift.counted_bank = cnt_bank
    shift.cash_difference = (cnt_cash - exp_cash).quantize(Decimal("0.001"))
    shift.bank_difference = (cnt_bank - exp_bank).quantize(Decimal("0.001"))
    shift.expected_bookings_count = activity.expected_booking_ops
    shift.counted_bookings_count = cnt_book
    shift.expected_meals_count = activity.meals_settled_count
    shift.counted_meals_count = cnt_meals
    shift.expected_laundry_count = activity.laundry_count
    shift.counted_laundry_count = cnt_laundry
    shift.expected_services_count = activity.services_count
    shift.counted_services_count = cnt_services
    snap = activity.to_dict()
    snap["expected_cash"] = str(exp_cash)
    snap["expected_bank"] = str(exp_bank)
    snap["opening_cash"] = str(
        Decimal(str(shift.opening_cash or 0)).quantize(Decimal("0.001"))
    )
    shift.close_snapshot_json = json.dumps(snap, ensure_ascii=False)
    db.flush()

    operator = ""
    from modules.authz.models import User

    closer = db.get(User, user_id)
    if closer:
        operator = (closer.username or "").strip()
    try:
        from modules.notifications.hotel_shift_hooks import emit_hotel_shift_closed

        emit_hotel_shift_closed(
            db,
            shift,
            operator_name=operator,
            revenue=summary.total_revenue,
            expenses=summary.total_expenses,
        )
    except Exception:  # noqa: BLE001
        pass
    return shift


def shift_is_overdue(shift: HotelShift, *, grace_minutes: int = 30) -> bool:
    if shift.status != HotelShiftStatus.OPEN:
        return False
    end = ensure_utc(shift.scheduled_end)
    deadline = end + timedelta(minutes=max(5, grace_minutes))
    return datetime.now(timezone.utc) > deadline


def shift_overdue_minutes(shift: HotelShift) -> int:
    end = ensure_utc(shift.scheduled_end)
    delta = datetime.now(timezone.utc) - end
    return max(0, int(delta.total_seconds() // 60))


def format_shift_window(shift: HotelShift) -> str:
    return (
        f"{format_local_dt(shift.scheduled_start, '%H:%M')} — "
        f"{format_local_dt(shift.scheduled_end, '%H:%M')}"
    )
