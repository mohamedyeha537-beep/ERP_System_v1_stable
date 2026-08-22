"""تسعير الإقامة — أسعار أساسية وقواعد نهاية الأسبوع."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.hotel.booking_models import HotelPricingRule, HotelRatePlan, HotelRoomType


def nightly_rate_for_date(
    db: Session, *, room_type_id: int, night: date
) -> Decimal:
    rt = db.get(HotelRoomType, room_type_id)
    if rt is None:
        return Decimal("0")
    base = Decimal(str(rt.base_price or 0))
    plans = list(
        db.scalars(
            select(HotelRatePlan).where(
                HotelRatePlan.room_type_id == room_type_id,
                HotelRatePlan.is_active.is_(True),
            )
        ).all()
    )
    if not plans:
        return base.quantize(Decimal("0.001"))
    plan = plans[0]
    dow = night.weekday()
    rules = list(
        db.scalars(
            select(HotelPricingRule).where(
                HotelPricingRule.rate_plan_id == plan.id,
                HotelPricingRule.is_active.is_(True),
            )
        ).all()
    )
    for rule in rules:
        if rule.date_from and rule.date_to:
            if rule.date_from <= night <= rule.date_to:
                return Decimal(str(rule.price)).quantize(Decimal("0.001"))
        if rule.day_of_week is not None and int(rule.day_of_week) == dow:
            return Decimal(str(rule.price)).quantize(Decimal("0.001"))
    return Decimal(str(plan.base_price or base)).quantize(Decimal("0.001"))


def nightly_rate_for_stay(
    db: Session, *, room_type_id: int, check_in: date, check_out: date
) -> Decimal:
    """متوسط السعر الليلي للإقامة (للعرض السريع)."""
    nights = max(1, (check_out - check_in).days)
    total = Decimal("0")
    d = check_in
    for _ in range(nights):
        total += nightly_rate_for_date(db, room_type_id=room_type_id, night=d)
        d += timedelta(days=1)
    return (total / nights).quantize(Decimal("0.001"))


def accommodation_total(
    db: Session,
    *,
    room_type_id: int,
    check_in: date,
    check_out: date,
    arrival_at=None,
    departure_at=None,
    first_chargeable_night: date | None = None,
) -> Decimal:
    from modules.hotel.checkin_stay import billable_night_units

    start = first_chargeable_night or check_in
    units = billable_night_units(
        db,
        check_in=check_in,
        check_out=check_out,
        arrival_at=arrival_at,
        departure_at=departure_at,
        first_chargeable_night=start,
    )
    if units <= 0:
        return Decimal("0")
    # ليالٍ تقويمية كاملة: سعر كل ليلة حسب تاريخ الليلة المحسوب
    calendar = max(0, (check_out - start).days)
    if calendar >= 1 and units == Decimal(calendar):
        total = Decimal("0")
        d = start
        while d < check_out:
            total += nightly_rate_for_date(db, room_type_id=room_type_id, night=d)
            d += timedelta(days=1)
        return total.quantize(Decimal("0.001"))
    rate = nightly_rate_for_date(db, room_type_id=room_type_id, night=start)
    return (rate * units).quantize(Decimal("0.001"))


def accommodation_segment_for_room(
    db: Session,
    *,
    room: "HotelRoom | None" = None,
    room_type_id: int | None = None,
    seg_in: date,
    seg_out: date,
    arrival_at=None,
    departure_at=None,
) -> Decimal:
    """إجمالي إقامة لقطعة من التواريخ — يدعم سعر الشقة المخصص أو نوع الغرفة."""
    from modules.hotel.checkin_stay import billable_night_units

    if seg_out < seg_in:
        return Decimal("0")
    units = billable_night_units(
        db,
        check_in=seg_in,
        check_out=seg_out,
        arrival_at=arrival_at,
        departure_at=departure_at,
        first_chargeable_night=seg_in,
    )
    if units <= 0:
        return Decimal("0")
    if room is not None and room.nightly_price is not None:
        return (Decimal(str(room.nightly_price)) * units).quantize(Decimal("0.001"))
    rt_id = (room.room_type_id if room is not None else None) or room_type_id
    if rt_id:
        return accommodation_total(
            db,
            room_type_id=rt_id,
            check_in=seg_in,
            check_out=seg_out,
            arrival_at=arrival_at,
            departure_at=departure_at,
            first_chargeable_night=seg_in,
        )
    return Decimal("0")
