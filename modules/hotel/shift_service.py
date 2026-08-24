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


def _hotel_carry_label(sh: HotelShift) -> str:
    if getattr(sh, "employee", None) is not None and (sh.employee.full_name_ar or "").strip():
        return sh.employee.full_name_ar.strip()
    if getattr(sh, "user", None) is not None and (sh.user.username or "").strip():
        return sh.user.username.strip()
    return f"جلسة فندق #{sh.id}"


def get_pending_hotel_carry(db: Session) -> HotelShift | None:
    from modules.payments.shift_carry import CLOSE_DEST_NEXT_SHIFT

    return db.execute(
        select(HotelShift)
        .options(selectinload(HotelShift.employee), selectinload(HotelShift.user))
        .where(
            HotelShift.status == HotelShiftStatus.CLOSED,
            HotelShift.close_destination == CLOSE_DEST_NEXT_SHIFT,
            HotelShift.carried_to_shift_id.is_(None),
        )
        .order_by(HotelShift.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def list_active_reception_employees(
    db: Session, *, exclude_employee_id: int | None = None
) -> list[dict]:
    """موظفو استقبال نشطون وحساباتهم مفعّلة — لاختيار مستلم الترحيل."""
    from modules.authz.models import User
    from modules.hr.models import Employee, EmployeeStatus, HrDepartment
    from modules.platform.business_domain import BusinessDomain

    rows = list(
        db.scalars(
            select(Employee)
            .options(selectinload(Employee.department))
            .join(User, User.id == Employee.user_id)
            .where(
                Employee.status == EmployeeStatus.ACTIVE,
                Employee.user_id.is_not(None),
                User.is_active.is_(True),
                Employee.business_domain.in_(
                    (BusinessDomain.HOTEL.value, BusinessDomain.SHARED.value)
                ),
            )
            .order_by(Employee.full_name_ar)
        ).all()
    )
    reception: list[Employee] = []
    for emp in rows:
        dept_code = str(getattr(getattr(emp, "department", None), "code", "") or "").upper()
        if emp.is_hotel_front or dept_code == "RECEP":
            reception.append(emp)
    chosen = reception or rows
    out: list[dict] = []
    skip = int(exclude_employee_id) if exclude_employee_id else None
    for emp in chosen:
        if skip is not None and int(emp.id) == skip:
            continue
        out.append(
            {
                "id": int(emp.id),
                "name": (emp.full_name_ar or "").strip() or f"موظف #{emp.id}",
            }
        )
    if not out:
        for emp in chosen:
            out.append(
                {
                    "id": int(emp.id),
                    "name": (emp.full_name_ar or "").strip() or f"موظف #{emp.id}",
                }
            )
    return out


def peek_pending_hotel_carry_offer(db: Session):
    from modules.payments.shift_carry import ShiftCarryOffer, money3
    from modules.payments.shift_handovers import (
        confirmed_received_totals,
        handed_claimed_totals,
    )

    src = get_pending_hotel_carry(db)
    if src is None:
        return None
    recipient = ""
    if getattr(src, "carried_to_employee_id", None):
        from modules.hr.models import Employee

        emp = db.get(Employee, int(src.carried_to_employee_id))
        if emp is not None:
            recipient = (emp.full_name_ar or "").strip()
    handed_c, handed_b = handed_claimed_totals(db, hotel_shift_id=src.id)
    conf_c, conf_b = confirmed_received_totals(db, hotel_shift_id=src.id)
    rem_c = max(money3(src.counted_cash) - handed_c, money3(0))
    rem_b = max(money3(src.counted_bank) - handed_b, money3(0))
    needs = rem_c > money3("0.001") or rem_b > money3("0.001")
    return ShiftCarryOffer(
        shift_id=src.id,
        cash=rem_c if needs else money3(src.counted_cash),
        bank=rem_b if needs else money3(src.counted_bank),
        cashier_label=_hotel_carry_label(src),
        closed_at=src.closed_at,
        recipient_label=recipient,
        already_confirmed_cash=conf_c,
        already_confirmed_bank=conf_b,
        remainder_needs_count=needs or (handed_c == 0 and handed_b == 0),
    )


def consume_pending_hotel_carry(
    db: Session,
    shift: HotelShift,
    *,
    received_cash=None,
    received_bank=None,
) -> HotelShift | None:
    from modules.payments.shift_carry import money3
    from modules.payments.shift_variance_models import ShiftVarianceSource
    from modules.payments.shift_variances import record_pair_variances

    src = get_pending_hotel_carry(db)
    if src is None:
        return None
    from modules.payments.shift_handovers import (
        confirmed_received_totals,
        handed_claimed_totals,
        list_unconfirmed_cash_handovers,
    )

    pending_sent = list_unconfirmed_cash_handovers(db, hotel_shift_id=src.id)
    if pending_sent:
        raise HotelShiftError(
            "أكد استلام التسليمات المعلقة أولاً من صفحة «استلام العهدة» قبل افتتاح الجلسة."
        )
    handed_c, handed_b = handed_claimed_totals(db, hotel_shift_id=src.id)
    conf_c, conf_b = confirmed_received_totals(db, hotel_shift_id=src.id)
    rem_claimed_c = max(money3(src.counted_cash) - handed_c, money3(0))
    rem_claimed_b = max(money3(src.counted_bank) - handed_b, money3(0))
    needs_remainder = rem_claimed_c > money3("0.001") or rem_claimed_b > money3("0.001")
    if needs_remainder or (handed_c == 0 and handed_b == 0):
        if received_cash is None or received_bank is None:
            raise HotelShiftError(
                "يوجد رصيد مرحّل من الجلسة السابقة. أدخل المبلغ الذي استلمته وعددته فعلياً."
            )
        rec_cash = money3(received_cash)
        rec_bank = money3(received_bank)
        if rec_cash < 0 or rec_bank < 0:
            raise HotelShiftError("مبلغ الاستلام غير صالح.")
        claimed_cash = rem_claimed_c if handed_c or handed_b else money3(src.counted_cash)
        claimed_bank = rem_claimed_b if handed_c or handed_b else money3(src.counted_bank)
        record_pair_variances(
            db,
            source_type=ShiftVarianceSource.HOTEL_CARRY,
            claimed_cash=claimed_cash,
            received_cash=rec_cash,
            claimed_bank=claimed_bank,
            received_bank=rec_bank,
            hotel_shift_id=src.id,
            from_employee_id=src.employee_id,
            to_employee_id=shift.employee_id,
            note=f"تسليم عهدة من جلسة #{src.id} إلى جلسة #{shift.id}",
        )
    else:
        rec_cash = money3(0)
        rec_bank = money3(0)
        claimed_cash = money3(src.counted_cash)
        claimed_bank = money3(src.counted_bank)
    # أحمد يرث ما عده فقط — لا نعدّل مبلغ محمد
    shift.opening_cash = money3(conf_c + rec_cash)
    shift.opening_bank = money3(conf_b + rec_bank)
    shift.received_from_shift_id = src.id
    src.carried_to_shift_id = shift.id
    auto = (
        f"استلام من الجلسة #{src.id} "
        f"(المسلِّم: كاش {claimed_cash} / مصرف {claimed_bank} — "
        f"المستلم: كاش {shift.opening_cash} / مصرف {shift.opening_bank})"
    )
    prev = (shift.opening_note or "").strip()
    shift.opening_note = f"{prev} — {auto}".strip(" —") if prev else auto
    db.flush()
    return src


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
        close_destination="TREASURY",
        enforce_close_policy=False,
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
    received_cash: Decimal | str | None = None,
    received_bank: Decimal | str | None = None,
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

    if employee_id is None:
        from modules.hr.service import get_employee_by_user_id

        linked = get_employee_by_user_id(db, user_id)
        if linked is not None:
            employee_id = linked.id

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
        opening_bank=Decimal("0"),
    )
    db.add(shift)
    db.flush()
    carried = consume_pending_hotel_carry(
        db, shift, received_cash=received_cash, received_bank=received_bank
    )
    # رصيد مرحّل من الجلسة السابقة يبقى في الدرج — لا يُخصم من الخزينة مرة ثانية
    if carried is None:
        oc = Decimal(str(shift.opening_cash or 0)).quantize(Decimal("0.001"))
        if oc > 0:
            from modules.hotel.shift_handoff import (
                HotelShiftHandoffError,
                issue_hotel_opening_float_transfer,
            )

            try:
                issue_hotel_opening_float_transfer(
                    db, shift_id=shift.id, amount=oc, user_id=user_id
                )
            except HotelShiftHandoffError as exc:
                raise HotelShiftError(str(exc)) from exc
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
    close_destination: str | None = None,
    enforce_close_policy: bool = True,
    carried_to_employee_id: int | None = None,
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
                "أدخل المبلغ المعدود للكاش بعد عدّ الدرج."
            )
        if counted_bank is None or str(counted_bank).strip() == "":
            raise HotelShiftError(
                "أدخل المبلغ المعدود للمصرف/التحويل بعد مراجعة الإيصالات."
            )

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
        if exp_cash < 0 or exp_bank < 0:
            raise HotelShiftError(
                "المعدود لا يمكن أن يكون سالباً. "
                "المتوقع سالب لأن الجلسة فيها استرداد/صرف أكثر من القبض — "
                "أدخل 0 إذا الدرج فارغ (لا تُدخل سالباً لمطابقة المتوقع)."
            )
        raise HotelShiftError("المعدود لا يمكن أن يكون سالباً.")

    def _int_val(raw, default: int = 0) -> int:
        try:
            return max(0, int(str(raw if raw is not None else default)))
        except (TypeError, ValueError):
            return default

    # بنود العدد لم تعد تُطلب عند الإقفال — نُسجّل المتوقع كمرجع للتقرير فقط
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
    snap["opening_bank"] = str(
        Decimal(str(shift.opening_bank or 0)).quantize(Decimal("0.001"))
    )
    shift.close_snapshot_json = json.dumps(snap, ensure_ascii=False)
    from modules.payments.shift_carry import (
        CLOSE_DEST_NEXT_SHIFT,
        CLOSE_DEST_TREASURY,
        ShiftCarryError,
        money3,
        resolve_close_destination,
    )

    from modules.payments.shift_handovers import (
        hotel_handover_progress,
        last_cash_handover_recipient,
        propose_cash_carry,
        ShiftHandoverError,
    )

    progress = hotel_handover_progress(
        db, shift, expected_cash=exp_cash, expected_bank=exp_bank
    )
    has_handovers = bool(progress.rows)
    if has_handovers:
        # المعدود هنا = المتبقي بعد التسليمات السابقة
        shift.counted_cash = money3(progress.handed_cash + cnt_cash)
        shift.counted_bank = money3(progress.handed_bank + cnt_bank)
        shift.cash_difference = money3(cnt_cash - progress.remainder_cash)
        shift.bank_difference = money3(cnt_bank - progress.remainder_bank)
    try:
        dest = resolve_close_destination(
            db, close_destination, enforce_policy=enforce_close_policy
        )
    except ShiftCarryError as exc:
        raise HotelShiftError(str(exc)) from exc
    if dest == CLOSE_DEST_NEXT_SHIFT:
        pending = get_pending_hotel_carry(db)
        if pending is not None:
            raise HotelShiftError(
                f"يوجد رصيد مرحّل من الجلسة #{pending.id} لم يُستلم بعد. "
                "افتح الجلسة التالية أولاً، أو رحّل هذه الجلسة إلى الخزينة."
            )
        last_to = last_cash_handover_recipient(db, hotel_shift_id=shift.id)
        emp_id = int(carried_to_employee_id) if carried_to_employee_id else (last_to or 0)
        allowed = {
            int(e["id"])
            for e in list_active_reception_employees(
                db, exclude_employee_id=shift.employee_id
            )
        }
        rem_left = progress.remainder_cash > money3("0.001") or progress.remainder_bank > money3(
            "0.001"
        )
        if emp_id <= 0 or emp_id not in allowed:
            if rem_left or not last_to:
                raise HotelShiftError("اختر موظف الاستقبال المستلم للعهدة.")
            emp_id = int(last_to)
        shift.carried_to_employee_id = emp_id
        if rem_left and (cnt_cash > 0 or cnt_bank > 0):
            try:
                propose_cash_carry(
                    db,
                    domain="hotel",
                    hotel_shift_id=shift.id,
                    from_employee_id=shift.employee_id,
                    to_employee_id=emp_id,
                    claimed_cash=cnt_cash,
                    claimed_bank=cnt_bank,
                    user_id=user_id,
                    note="متبقي عند الإقفال",
                )
            except ShiftHandoverError as exc:
                raise HotelShiftError(str(exc)) from exc
    else:
        shift.carried_to_employee_id = None
    shift.close_destination = dest
    db.flush()

    # فرق العد يُعلَّق للمراجعة — لا خصم راتب تلقائي
    try:
        _record_hotel_shift_close_variances(db, shift)
    except Exception:  # noqa: BLE001
        pass

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

    if dest == CLOSE_DEST_TREASURY:
        try:
            from app.datetime_local import format_local_dt
            from modules.hotel.shift_handoff import count_hotel_shifts_pending_handoff
            from modules.notifications.treasury_hooks import emit_treasury_handoff_pending

            emp_name = ""
            if shift.employee_id:
                from modules.hr.models import Employee

                emp = db.get(Employee, shift.employee_id)
                if emp:
                    emp_name = (emp.full_name_ar or "").strip()
            emit_treasury_handoff_pending(
                db,
                shift_id=int(shift.id),
                cashier_name=emp_name or operator or f"#{user_id}",
                counted_cash=shift.counted_cash,
                counted_bank=shift.counted_bank,
                closed_at=format_local_dt(shift.closed_at, "%Y-%m-%d %H:%M")
                if shift.closed_at
                else "—",
                pending_count=count_hotel_shifts_pending_handoff(db),
                reminder_slot="initial",
                shift_kind="hotel",
            )
        except Exception:  # noqa: BLE001
            pass
    return shift


def _record_hotel_shift_close_variances(db: Session, shift: HotelShift) -> int:
    """يسجّل عجز/زيادة العد عند الإقفال كمعلّق للمراجعة — بلا خصم راتب."""
    from modules.payments.shift_variance_models import ShiftVarianceSource
    from modules.payments.shift_variances import record_pair_variances

    rows = record_pair_variances(
        db,
        source_type=ShiftVarianceSource.HOTEL_CLOSE,
        claimed_cash=shift.expected_cash,
        received_cash=shift.counted_cash,
        claimed_bank=shift.expected_bank,
        received_bank=shift.counted_bank,
        hotel_shift_id=shift.id,
        from_employee_id=shift.employee_id,
        note=f"عدّ إقفال جلسة فندق #{shift.id}",
    )
    return len(rows)


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


def reopen_hotel_shift_to_draft(
    db: Session,
    *,
    shift_id: int,
    user_id: int,
    admin_username: str,
    reason: str = "",
) -> HotelShift:
    """يعيد وردية فندق مغلقة إلى مفتوحة لتعديل المعدود ثم إعادة الإقفال."""
    shift = db.get(HotelShift, shift_id)
    if shift is None or shift.status != HotelShiftStatus.CLOSED:
        raise HotelShiftError("الوردية غير موجودة أو ليست مغلقة.")
    other = get_open_shift(db, property_id=int(shift.property_id or 1))
    if other is not None:
        raise HotelShiftError(
            f"لا يمكن إعادة الوردية لمسودة: توجد وردية مفتوحة #{other.id}. أغلقها أولاً."
        )
    if getattr(shift, "carried_to_shift_id", None):
        raise HotelShiftError(
            "الرصيد رُحِّل لوردية لاحقة. لا يمكن إعادة هذه الوردية لمسودة."
        )
    try:
        from modules.payments.shift_variances import ShiftVariance, ShiftVarianceStatus

        decided = list(
            db.scalars(
                select(ShiftVariance).where(
                    ShiftVariance.hotel_shift_id == int(shift_id),
                    ShiftVariance.status != ShiftVarianceStatus.PENDING_REVIEW,
                )
            ).all()
        )
        if decided:
            raise HotelShiftError(
                "لا يمكن إعادة المسودة: يوجد عجز/زيادة معتمد على هذه الوردية."
            )
        for row in db.scalars(
            select(ShiftVariance).where(ShiftVariance.hotel_shift_id == int(shift_id))
        ).all():
            db.delete(row)
    except HotelShiftError:
        raise
    except Exception:  # noqa: BLE001
        pass
    why = (reason or "").strip() or "تصحيح أرقام المعدود بعد الإقفال"
    if shift.treasury_handoff_at is not None:
        from modules.hotel.shift_handoff import (
            HotelShiftHandoffError,
            revoke_hotel_shift_handoff,
        )

        try:
            revoke_hotel_shift_handoff(
                db,
                shift_id=int(shift_id),
                user_id=user_id,
                reason=why,
            )
        except HotelShiftHandoffError as exc:
            raise HotelShiftError(str(exc)) from exc
    shift.status = HotelShiftStatus.OPEN
    shift.closed_at = None
    shift.closed_by_id = None
    shift.counted_cash = None
    shift.counted_bank = None
    shift.cash_difference = None
    shift.bank_difference = None
    shift.expected_cash = None
    shift.expected_bank = None
    shift.close_destination = None
    shift.carried_to_employee_id = None
    shift.close_snapshot_json = None
    line = f"[إعادة لمسودة — {admin_username}: {why}]"
    prev = (shift.closing_note or "").strip()
    shift.closing_note = (prev + " | " + line).strip(" | ") if prev else line
    db.flush()
    return shift
