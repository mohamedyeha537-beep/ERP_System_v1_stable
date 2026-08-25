"""ديون حجوزات الفندق — ترحيل عند المغادرة، تحصيل (كامل/جزئي)، ملاحظات وتذكير."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from modules.hotel.booking_models import (
    HotelBooking,
    HotelBookingDebt,
    HotelBookingDebtStatus,
)
from modules.hotel.booking_service import BookingError, log_audit, record_payment


def debt_remaining(debt: HotelBookingDebt) -> Decimal:
    if debt.amount_remaining is not None:
        return Decimal(str(debt.amount_remaining)).quantize(Decimal("0.001"))
    return Decimal(str(debt.amount or 0)).quantize(Decimal("0.001"))


def list_open_debts(db: Session, *, booking_id: int | None = None) -> list[HotelBookingDebt]:
    stmt = (
        select(HotelBookingDebt)
        .options(joinedload(HotelBookingDebt.booking))
        .where(HotelBookingDebt.status == HotelBookingDebtStatus.OPEN)
    )
    if booking_id is not None:
        stmt = stmt.where(HotelBookingDebt.booking_id == booking_id)
    return list(db.scalars(stmt.order_by(HotelBookingDebt.id.desc())).unique().all())


def debt_reminder_date(debt: HotelBookingDebt) -> date | None:
    rem = getattr(debt, "reminder_at", None)
    if rem is None:
        return None
    if isinstance(rem, datetime):
        return rem.date()
    return rem


def normalize_reminder_time(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    parts = text.replace(".", ":").split(":")
    try:
        hh = max(0, min(23, int(parts[0])))
        mm = max(0, min(59, int(parts[1]))) if len(parts) > 1 else 0
        return f"{hh:02d}:{mm:02d}"
    except (TypeError, ValueError):
        return None


def debt_reminder_time_str(debt: HotelBookingDebt) -> str | None:
    return normalize_reminder_time(getattr(debt, "reminder_time", None))


def debt_reminder_is_due(
    debt: HotelBookingDebt, *, as_of: date | datetime | None = None
) -> bool:
    """حان موعد التذكير (تاريخ + وقت اختياري) أو بلا موعد = كل وردية."""
    from app.datetime_local import now_local

    if as_of is None:
        now = now_local()
    elif isinstance(as_of, datetime):
        now = as_of
    else:
        # مقارنة تاريخ فقط — بدون وقت
        rem_d = debt_reminder_date(debt)
        if rem_d is None:
            return True
        return rem_d <= as_of

    rem_d = debt_reminder_date(debt)
    if rem_d is None:
        return True
    today = now.date() if hasattr(now, "date") else now
    if rem_d < today:
        return True
    if rem_d > today:
        return False
    t_s = debt_reminder_time_str(debt)
    if not t_s:
        return True
    try:
        hh, mm = (int(x) for x in t_s.split(":", 1))
        now_mins = now.hour * 60 + now.minute
        return now_mins >= hh * 60 + mm
    except (TypeError, ValueError):
        return True


def list_debts_due_for_shift_followup(db: Session, *, as_of: date | None = None) -> list[HotelBookingDebt]:
    """ديون مفتوحة حان موعد تذكيرها، أو بلا موعد (تذكير كل وردية)."""
    from app.datetime_local import now_local

    now = now_local() if as_of is None else as_of
    rows = list_open_debts(db)
    out: list[HotelBookingDebt] = []
    for d in rows:
        if debt_remaining(d) <= Decimal("0.0005"):
            continue
        if debt_reminder_is_due(d, as_of=now):
            out.append(d)
    return out


def list_booking_debts(db: Session, booking_id: int) -> list[HotelBookingDebt]:
    return list(
        db.scalars(
            select(HotelBookingDebt)
            .where(HotelBookingDebt.booking_id == booking_id)
            .order_by(HotelBookingDebt.id.desc())
        ).all()
    )


def sync_open_debts_with_folio(
    db: Session,
    booking_id: int,
    *,
    user_id: int | None = None,
) -> int:
    """يحدّث ديون المغادرة المفتوحة لتطابق متبقي الفوليو بعد سداد/خصم محفظة."""
    from modules.hotel.folio import build_folio

    try:
        folio_due = max(Decimal("0"), Decimal(str(build_folio(db, int(booking_id)).balance or 0)))
    except Exception:  # noqa: BLE001
        return 0
    folio_due = folio_due.quantize(Decimal("0.001"))
    changed = 0
    for debt in list_open_debts(db, booking_id=int(booking_id)):
        rem = debt_remaining(debt)
        if folio_due <= Decimal("0.0005"):
            debt.amount_remaining = Decimal("0")
            debt.status = HotelBookingDebtStatus.COLLECTED
            debt.collected_at = datetime.now(timezone.utc)
            if user_id is not None:
                debt.collected_by_id = user_id
            log_audit(
                db,
                entity_type="booking_debt",
                entity_id=debt.id,
                action="collect",
                new_value=str(rem),
                reason="تصفير تلقائي بعد سداد/خصم رصيد المحفظة",
                user_id=user_id,
            )
            changed += 1
        elif rem > folio_due + Decimal("0.0005"):
            debt.amount_remaining = folio_due
            log_audit(
                db,
                entity_type="booking_debt",
                entity_id=debt.id,
                action="collect",
                new_value=str((rem - folio_due).quantize(Decimal("0.001"))),
                reason=f"تخفيض الدين ليطابق الفوليو — متبقي {folio_due}",
                user_id=user_id,
            )
            changed += 1
    if changed:
        try:
            from modules.hotel.nav_badges import invalidate_hotel_nav_badges

            invalidate_hotel_nav_badges()
        except Exception:
            pass
        db.flush()
    return changed


def create_checkout_debt(
    db: Session,
    booking: HotelBooking,
    amount: Decimal,
    *,
    user_id: int | None = None,
    note: str | None = None,
    settlement_json: str | None = None,
    reminder_at: date | None = None,
    reminder_time: str | None = None,
) -> HotelBookingDebt:
    amt = Decimal(str(amount)).quantize(Decimal("0.001"))
    if amt <= 0:
        raise BookingError("مبلغ الدين غير صالح.")
    # حد ائتمان الشركة يُفرض عند إنشاء حجز جديد — لا يُحبس النزيل في الشقة
    # عند المغادرة: يُرحَّل المتبقي إلى الذمم لتحرير الشقة والتحصيل لاحقاً.
    existing = db.scalar(
        select(HotelBookingDebt).where(
            HotelBookingDebt.booking_id == booking.id,
            HotelBookingDebt.status == HotelBookingDebtStatus.OPEN,
        )
    )
    if existing is not None:
        raise BookingError("يوجد دين مفتوح على هذا الحجز بالفعل.")
    debt = HotelBookingDebt(
        booking_id=booking.id,
        amount=amt,
        amount_remaining=amt,
        status=HotelBookingDebtStatus.OPEN,
        reason=(note or "").strip() or "دين عند المغادرة — لم يتم التسديد",
        reminder_at=reminder_at,
        reminder_time=normalize_reminder_time(reminder_time) if reminder_at else None,
        settlement_json=settlement_json,
        created_by_id=user_id,
    )
    db.add(debt)
    db.flush()
    try:
        from modules.hotel.nav_badges import invalidate_hotel_nav_badges

        invalidate_hotel_nav_badges()
    except Exception:
        pass
    log_audit(
        db,
        entity_type="booking_debt",
        entity_id=debt.id,
        action="create",
        new_value=str(amt),
        reason=debt.reason,
        user_id=user_id,
    )
    return debt


def collect_booking_debt(
    db: Session,
    debt_id: int,
    *,
    payment_method_id: int,
    user_id: int | None = None,
    note: str | None = None,
    amount: Decimal | None = None,
) -> HotelBookingDebt:
    debt = db.get(HotelBookingDebt, debt_id)
    if debt is None:
        raise BookingError("سجل الدين غير موجود.")
    if debt.status != HotelBookingDebtStatus.OPEN:
        raise BookingError("هذا الدين ليس مفتوحاً.")
    remaining = debt_remaining(debt)
    if remaining <= Decimal("0.0005"):
        raise BookingError("لا يتبقى مبلغ على هذا الدين.")
    try:
        take = (
            Decimal(str(amount)).quantize(Decimal("0.001"))
            if amount is not None
            else remaining
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BookingError("مبلغ التحصيل غير صالح.") from exc
    if take <= Decimal("0.0005"):
        raise BookingError("أدخل مبلغ تحصيل أكبر من صفر.")
    if take > remaining + Decimal("0.0005"):
        raise BookingError(f"المبلغ أكبر من المتبقي ({remaining} د.ل).")

    pay = record_payment(
        db,
        debt.booking_id,
        amount=take,
        payment_method_id=payment_method_id,
        is_deposit=False,
        user_id=user_id,
        note=(note or "").strip()
        or f"تحصيل دين حجز #{debt.booking_id} ({take} من {remaining})",
        allow_closed=True,
    )
    new_rem = (remaining - take).quantize(Decimal("0.001"))
    if new_rem < 0:
        new_rem = Decimal("0")
    debt.amount_remaining = new_rem
    if new_rem <= Decimal("0.0005"):
        debt.amount_remaining = Decimal("0")
        debt.status = HotelBookingDebtStatus.COLLECTED
        debt.collected_at = datetime.now(timezone.utc)
        debt.collected_by_id = user_id
        debt.collection_payment_id = pay.id
    log_audit(
        db,
        entity_type="booking_debt",
        entity_id=debt.id,
        action="collect",
        new_value=str(take),
        reason=f"متبقي بعد التحصيل: {debt.amount_remaining}",
        user_id=user_id,
    )
    db.flush()
    try:
        from modules.hotel.nav_badges import invalidate_hotel_nav_badges

        invalidate_hotel_nav_badges()
    except Exception:
        pass
    return debt


def update_debt_followup(
    db: Session,
    debt_id: int,
    *,
    note: str | None = None,
    reminder_at: date | None = None,
    reminder_time: str | None = None,
    clear_reminder: bool = False,
    user_id: int | None = None,
) -> HotelBookingDebt:
    debt = db.get(HotelBookingDebt, debt_id)
    if debt is None:
        raise BookingError("سجل الدين غير موجود.")
    if debt.status != HotelBookingDebtStatus.OPEN:
        raise BookingError("يمكن تحديث الديون المفتوحة فقط.")
    note_s = (note or "").strip()
    if note_s:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        line = f"[{stamp}] {note_s}"
        prev = (debt.follow_up_notes or "").strip()
        debt.follow_up_notes = f"{prev}\n{line}".strip() if prev else line
    if clear_reminder:
        debt.reminder_at = None
        debt.reminder_time = None
    elif reminder_at is not None:
        debt.reminder_at = reminder_at
        debt.reminder_time = normalize_reminder_time(reminder_time)
    log_audit(
        db,
        entity_type="booking_debt",
        entity_id=debt.id,
        action="followup",
        new_value=(
            f"{debt.reminder_at or ''}"
            + (f" {debt.reminder_time}" if debt.reminder_time else "")
        ).strip(),
        reason=note_s or None,
        user_id=user_id,
    )
    db.flush()
    return debt


def write_off_booking_debt(
    db: Session,
    debt_id: int,
    *,
    reason: str | None,
    user_id: int | None = None,
) -> HotelBookingDebt:
    debt = db.get(HotelBookingDebt, debt_id)
    if debt is None:
        raise BookingError("سجل الدين غير موجود.")
    if debt.status != HotelBookingDebtStatus.OPEN:
        raise BookingError("يمكن شطب الديون المفتوحة فقط.")
    why = (reason or "").strip() or "شطب دين — عدم التسديد (خسارة)"
    debt.status = HotelBookingDebtStatus.WRITTEN_OFF
    debt.written_off_at = datetime.now(timezone.utc)
    debt.written_off_by_id = user_id
    debt.write_off_reason = why
    debt.amount_remaining = Decimal("0")
    log_audit(
        db,
        entity_type="booking_debt",
        entity_id=debt.id,
        action="write_off",
        new_value=str(debt.amount),
        reason=why,
        user_id=user_id,
    )
    db.flush()
    try:
        from modules.hotel.nav_badges import invalidate_hotel_nav_badges

        invalidate_hotel_nav_badges()
    except Exception:
        pass
    return debt


def create_uncollectible_checkout_debt(
    db: Session,
    booking: HotelBooking,
    amount: Decimal,
    *,
    user_id: int | None = None,
    note: str | None = None,
    settlement_json: str | None = None,
) -> HotelBookingDebt:
    """دين مغادرة مسجّل فوراً كـ«غير قابل للتحصيل» (شطب/خسارة) — للأدمن فقط."""
    why = (note or "").strip() or "دين غير قابل للتحصيل عند المغادرة (قرار إداري)"
    debt = create_checkout_debt(
        db,
        booking,
        amount,
        user_id=user_id,
        note=why,
        settlement_json=settlement_json,
        reminder_at=None,
    )
    return write_off_booking_debt(
        db,
        int(debt.id),
        reason=why,
        user_id=user_id,
    )


def open_debts_summary(db: Session) -> tuple[list[HotelBookingDebt], Decimal]:
    """ديون مفتوحة لها متبقٍ فعلي — يستبعد سجلات الدين بعد تصفير الفوليو."""
    rows: list[HotelBookingDebt] = []
    for r in list_open_debts(db):
        rem = debt_remaining(r)
        if rem <= Decimal("0.0005"):
            continue
        try:
            from modules.hotel.folio import booking_balance_due

            due = Decimal(str(booking_balance_due(db, int(r.booking_id)) or 0))
            if due <= Decimal("0.0005"):
                continue
        except Exception:  # noqa: BLE001
            pass
        rows.append(r)
    total = sum((debt_remaining(r) for r in rows), Decimal("0")).quantize(
        Decimal("0.001")
    )
    return rows, total


def debts_followup_counts(db: Session) -> dict[str, int]:
    due = list_debts_due_for_shift_followup(db)
    open_n = sum(
        1 for d in list_open_debts(db) if debt_remaining(d) > Decimal("0.0005")
    )
    return {
        "open": open_n,
        "due_today": len(due),
        "no_reminder": sum(1 for d in due if d.reminder_at is None),
    }


def cleanup_stale_debt_alerts(db: Session) -> int:
    """يصفر ديون مفتوحة بلا متبقٍ ويوقف تتبّع المطالبة بعد السداد — يمنع شارة وهمية."""
    from modules.hotel.follow_up import clear_claim_wa_if_settled

    changed = 0
    # أولاً: طابق ديون المغادرة المفتوحة مع رصيد الفوليو (سداد عبر القبض دون collect_debt)
    booking_ids = {int(d.booking_id) for d in list_open_debts(db)}
    for bid in booking_ids:
        try:
            changed += int(sync_open_debts_with_folio(db, bid) or 0)
        except Exception:  # noqa: BLE001
            continue

    now = datetime.now(timezone.utc)
    for debt in list_open_debts(db):
        if debt_remaining(debt) > Decimal("0.0005"):
            continue
        debt.amount_remaining = Decimal("0")
        debt.status = HotelBookingDebtStatus.COLLECTED
        debt.collected_at = now
        changed += 1

    try:
        watched = list(
            db.scalars(
                select(HotelBooking).where(HotelBooking.claim_wa_until_paid.is_(True))
            ).all()
        )
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        watched = []
    for booking in watched:
        try:
            if clear_claim_wa_if_settled(db, booking):
                changed += 1
        except Exception:  # noqa: BLE001
            continue

    if changed:
        try:
            from modules.hotel.nav_badges import invalidate_hotel_nav_badges

            invalidate_hotel_nav_badges()
        except Exception:
            pass
        db.flush()
    return changed


def debts_nav_badge_count(db: Session) -> int:
    """عدد تقريبي سريع لشارة الذمم — بدون بناء folio لكل الحجوزات.

    الدقة الكاملة تبقى في صفحة الذمم. الشارة تُحدَّث مع كاش التنقل (~120 ث)
    وتُمسح عند التحصيل/المغادرة عبر invalidate_hotel_nav_badges.
    """
    from modules.hotel.booking_models import BookingPaymentStatus, BookingStatus

    ids: set[int] = set()
    for d in list_open_debts(db):
        if debt_remaining(d) > Decimal("0.0005"):
            ids.add(int(d.booking_id))

    try:
        unpaid_ids = db.scalars(
            select(HotelBooking.id).where(
                HotelBooking.booking_status.in_(
                    (BookingStatus.CHECKED_IN, BookingStatus.CHECKED_OUT)
                ),
                HotelBooking.payment_status.in_(
                    (
                        BookingPaymentStatus.UNPAID,
                        BookingPaymentStatus.PARTIALLY_PAID,
                    )
                ),
            )
        ).all()
        ids.update(int(i) for i in unpaid_ids if i)
    except Exception:
        pass
    return len(ids)


def bookings_with_debt_amounts(
    db: Session, booking_ids: list[int]
) -> dict[int, Decimal]:
    """معرّف الحجز → المتبقي الإجمالي (>0) لعرض شارة الدين في قائمة الحجوزات.

    المصدر: كشف الحساب الكامل (إقامة + تمديد + مطعم + مغسلة + خدمات) كتلة واحدة،
    مع أخذ الأعلى بين الفوليو وأي دين مغادرة مسجّل — حتى لا يُخفى جزء من الدين.
    """
    summaries = bookings_list_pay_summaries(db, booking_ids)
    return {
        bid: row["due"]
        for bid, row in summaries.items()
        if row.get("due", Decimal("0")) > Decimal("0.0005")
    }


def bookings_list_pay_summaries(
    db: Session, booking_ids: list[int]
) -> dict[int, dict[str, Decimal]]:
    """ملخص حساب موحّد لكل حجز في القائمة: due / credit / total / paid.

    لا يُخصم من المحفظة هنا — الخصم يتم عند شحن الرصيد أو بطلب الاستقبال.
    """
    if not booking_ids:
        return {}
    from modules.customers.models import Customer
    from modules.hotel.booking_models import BookingStatus
    from modules.hotel.folio import build_folio
    from modules.hotel.models import RoomCharge

    out: dict[int, dict[str, Decimal]] = {}
    id_set = {int(i) for i in booking_ids}

    recorded_debt: dict[int, Decimal] = {}
    for d in list_open_debts(db):
        bid = int(d.booking_id)
        if bid not in id_set:
            continue
        rem = debt_remaining(d)
        if rem > Decimal("0.0005"):
            recorded_debt[bid] = (
                recorded_debt.get(bid, Decimal("0")) + rem
            ).quantize(Decimal("0.001"))

    for bid in id_set:
        booking = db.get(HotelBooking, bid)
        if booking is None:
            continue
        if booking.booking_status in (
            BookingStatus.CANCELLED,
            BookingStatus.NO_SHOW,
            BookingStatus.LATE_CANCELLATION,
        ):
            continue

        total = Decimal("0")
        paid = Decimal("0")
        folio_bal = Decimal("0")
        wallet_left = Decimal("0")
        try:
            folio = build_folio(db, bid)
            total = Decimal(str(folio.total or 0)).quantize(Decimal("0.001"))
            paid = Decimal(str(folio.paid or 0)).quantize(Decimal("0.001"))
            folio_bal = Decimal(str(folio.balance or 0)).quantize(Decimal("0.001"))
        except Exception:
            paid = Decimal(str(booking.paid_amount or 0)).quantize(Decimal("0.001"))
            total = (
                Decimal(str(booking.accommodation_total or 0))
                - Decimal(str(booking.discount_amount or 0))
            ).quantize(Decimal("0.001"))
            folio_bal = (total - paid).quantize(Decimal("0.001"))

        due = max(Decimal("0"), folio_bal)
        credit = max(Decimal("0"), -folio_bal)

        # دين مغادرة مسجّل على *هذا* الحجز فقط — لا فواتير نزيل سابق على نفس الشقة
        rec = recorded_debt.get(bid, Decimal("0"))
        if rec > due:
            due = rec
            # الرصيد الموجب على نفس الحجز يغطّي الدين المسجّل
            if credit > Decimal("0.0005"):
                cover = min(credit, due)
                due = (due - cover).quantize(Decimal("0.001"))
                credit = (credit - cover).quantize(Decimal("0.001"))

        if due <= Decimal("0.0005"):
            has_charge = db.scalar(
                select(RoomCharge.id)
                .where(
                    RoomCharge.booking_id == bid,
                    RoomCharge.is_settled.is_(False),
                )
                .limit(1)
            )
            if has_charge and paid <= Decimal("0.0005"):
                # إشارة ضعيفة — لا نخترع مبلغاً؛ أعد بناء الفوليو إن أمكن
                pass

        # رصيد محفظة العميل المتبقي يظهر كرصيد موجب (بعد تغطية الدين)
        wallet_left = Decimal("0")
        if booking.customer_id and due <= Decimal("0.0005"):
            cust = db.get(Customer, int(booking.customer_id))
            if cust is not None:
                wallet_left = Decimal(str(cust.wallet_balance or 0)).quantize(
                    Decimal("0.001")
                )
            if wallet_left > Decimal("0.0005"):
                credit = (credit + wallet_left).quantize(Decimal("0.001"))

        out[bid] = {
            "due": due.quantize(Decimal("0.001")),
            "credit": credit.quantize(Decimal("0.001")),
            "total": total,
            "paid": paid,
            "wallet_credit": wallet_left.quantize(Decimal("0.001")),
        }
    return out
