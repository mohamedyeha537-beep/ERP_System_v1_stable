"""Hotel rooms & deferred payment business logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.hotel.booking_models import HotelRoomType
from modules.hotel.models import HotelRoom, RoomCharge
from modules.payments.models import PaymentMethod, SalePayment
from modules.payments.service import record_sale_payment
from modules.sales.models import Sale, SaleContext, SaleStatus


class HotelError(Exception):
    """خطأ في إدارة الفندق."""


# ============================================================
# Rooms — CRUD
# ============================================================

def list_rooms(db: Session, *, only_active: bool = False) -> list[HotelRoom]:
    stmt = select(HotelRoom).order_by(HotelRoom.is_active.desc(), HotelRoom.number)
    if only_active:
        stmt = stmt.where(HotelRoom.is_active.is_(True))
    return list(db.scalars(stmt))


def get_room(db: Session, room_id: int) -> HotelRoom | None:
    return db.get(HotelRoom, room_id)


def get_room_by_number(db: Session, number: str) -> HotelRoom | None:
    n = (number or "").strip()
    if not n:
        return None
    return db.scalar(select(HotelRoom).where(HotelRoom.number == n))


def create_room(
    db: Session,
    *,
    number: str,
    guest_name: str | None = None,
    notes: str | None = None,
    name_ar: str | None = None,
    nightly_price: Decimal | None = None,
    image_filename: str | None = None,
    room_type_id: int | None = None,
    floor: str | None = None,
    property_id: int = 1,
    show_online: bool = False,
    online_description: str | None = None,
) -> HotelRoom:
    n = (number or "").strip()
    if not n:
        raise HotelError("رقم الغرفة مطلوب.")
    if get_room_by_number(db, n) is not None:
        raise HotelError(f"الغرفة رقم {n} موجودة بالفعل.")
    if room_type_id is not None and db.get(HotelRoomType, int(room_type_id)) is None:
        raise HotelError("نوع الغرفة المحدد غير موجود.")
    r = HotelRoom(
        number=n,
        name_ar=(name_ar or "").strip() or None,
        nightly_price=nightly_price,
        image_filename=(image_filename or "").strip() or None,
        guest_name=(guest_name or "").strip() or None,
        notes=(notes or "").strip() or None,
        room_type_id=room_type_id,
        floor=(floor or "").strip() or None,
        property_id=property_id,
        is_active=True,
        show_online=bool(show_online),
        online_description=(online_description or "").strip() or None,
    )
    db.add(r)
    db.flush()
    return r


def update_room(
    db: Session,
    room_id: int,
    *,
    number: str | None = None,
    guest_name: str | None = None,
    notes: str | None = None,
    name_ar: str | None = None,
    nightly_price: Decimal | None = None,
    clear_nightly_price: bool = False,
    image_filename: str | None = None,
    is_active: bool | None = None,
    room_type_id: int | None = None,
    floor: str | None = None,
    physical_status: "RoomPhysicalStatus | None" = None,
    show_online: bool | None = None,
    online_description: str | None = None,
) -> HotelRoom:
    from modules.hotel.booking_models import RoomPhysicalStatus

    r = db.get(HotelRoom, room_id)
    if r is None:
        raise HotelError("الغرفة غير موجودة.")
    if number is not None:
        n = number.strip()
        if not n:
            raise HotelError("رقم الغرفة مطلوب.")
        existing = get_room_by_number(db, n)
        if existing is not None and existing.id != room_id:
            raise HotelError(f"الغرفة رقم {n} موجودة بالفعل.")
        r.number = n
    if guest_name is not None:
        r.guest_name = guest_name.strip() or None
    if notes is not None:
        r.notes = notes.strip() or None
    if name_ar is not None:
        r.name_ar = name_ar.strip() or None
    if clear_nightly_price:
        r.nightly_price = None
    elif nightly_price is not None:
        r.nightly_price = nightly_price if nightly_price > 0 else None
    if image_filename is not None:
        r.image_filename = image_filename.strip() or None
    if is_active is not None:
        r.is_active = is_active
    if room_type_id is not None:
        if room_type_id > 0 and db.get(HotelRoomType, int(room_type_id)) is None:
            raise HotelError("نوع الغرفة المحدد غير موجود.")
        r.room_type_id = room_type_id if room_type_id > 0 else None
    if floor is not None:
        r.floor = floor.strip() or None
    if physical_status is not None:
        r.physical_status = physical_status
    if show_online is not None:
        r.show_online = bool(show_online)
    if online_description is not None:
        r.online_description = online_description.strip() or None
    db.flush()
    return r


def delete_room(db: Session, room_id: int) -> None:
    r = db.get(HotelRoom, room_id)
    if r is None:
        return
    from modules.hotel.booking_models import HotelBooking

    booking_count = db.scalar(
        select(func.count(HotelBooking.id)).where(HotelBooking.room_id == room_id)
    )
    if (booking_count or 0) > 0:
        raise HotelError(
            "لا يمكن حذف الشقة لأنها مرتبطة بحجوزات — يمكنك تعطيلها بإلغاء «نشطة» بدلاً من الحذف."
        )
    open_count = db.scalar(
        select(func.count(RoomCharge.id))
        .where(RoomCharge.room_id == room_id, RoomCharge.is_settled.is_(False))
    )
    if (open_count or 0) > 0:
        raise HotelError(
            "لا يمكن حذف الغرفة وعليها فواتير مفتوحة — سوّ الحساب أولاً."
        )
    history_count = db.scalar(
        select(func.count(RoomCharge.id)).where(RoomCharge.room_id == room_id)
    )
    if (history_count or 0) > 0:
        raise HotelError(
            "لا يمكن حذف الغرفة لأن لها سجلات تسوية تاريخية — يمكنك "
            "تعطيلها بإلغاء «نشطة» بدلاً من الحذف."
        )
    db.delete(r)
    db.flush()


# ============================================================
# Charges — open & settle
# ============================================================

def open_room_charge(
    db: Session,
    *,
    sale_id: int,
    room_id: int,
    guest_name: str | None,
    note: str | None,
    user_id: int | None,
    booking_id: int | None = None,
) -> RoomCharge:
    """يفتح حساب غرفة على فاتورة بيع مكتملة (بدون دفع).

    اسم النزيل اختياري — يُكتب يدوياً على الفاتورة المطبوعة عند التوقيع.
    كل قيد يحفظ الاسم (إن وُجد) في ``guest_name_snapshot`` الخاص به.
    """
    sale = db.get(Sale, sale_id)
    if sale is None:
        raise HotelError("الفاتورة غير موجودة.")
    if sale.status != SaleStatus.COMPLETED:
        raise HotelError("الفاتورة يجب أن تكون مكتملة قبل قيدها على غرفة.")

    room = db.get(HotelRoom, room_id)
    if room is None or not room.is_active:
        raise HotelError("الغرفة غير موجودة أو غير نشطة.")

    snap = (guest_name or "").strip()
    if not snap:
        snap = None

    if booking_id is None:
        from modules.hotel.booking_service import active_booking_for_room

        active = active_booking_for_room(db, room_id)
        if active is not None:
            booking_id = active.id
            if not snap:
                snap = active.guest_name

    existing_payment = db.scalar(
        select(func.count(SalePayment.id)).where(SalePayment.sale_id == sale_id)
    )
    if (existing_payment or 0) > 0:
        raise HotelError("الفاتورة مدفوعة بالفعل ولا يمكن قيدها على غرفة.")

    existing_charge = db.scalar(
        select(RoomCharge).where(RoomCharge.sale_id == sale_id)
    )
    if existing_charge is not None:
        raise HotelError("الفاتورة مقيّدة على غرفة بالفعل.")

    if booking_id is not None:
        sale.booking_id = booking_id

    rc = RoomCharge(
        sale_id=sale_id,
        room_id=room_id,
        booking_id=booking_id,
        guest_name_snapshot=snap,
        note=(note or "").strip() or None,
        created_by_id=user_id,
        is_settled=False,
    )
    db.add(rc)
    db.flush()
    if booking_id is not None:
        try:
            from modules.hotel.booking_service import get_booking
            from modules.notifications.hotel_hooks import emit_hotel_unpaid_service_added

            booking = get_booking(db, int(booking_id))
            if booking is not None:
                emit_hotel_unpaid_service_added(
                    db,
                    booking,
                    service_name=f"فاتورة مطعم/خدمة #{sale_id}",
                    service_amount=Decimal(str(sale.total or 0)),
                )
        except Exception:  # noqa: BLE001
            pass
    return rc


def ensure_room_charge_for_sale(
    db: Session,
    *,
    sale_id: int,
    room_id: int,
    guest_name: str | None = None,
    note: str | None = None,
    user_id: int | None = None,
) -> RoomCharge:
    """يربط فاتورة شقة مكتملة بغرفة إن لم يكن لها قيد (إصلاح فواتير «غير مربوطة»)."""
    from modules.refunds.service import sale_outstanding_total

    existing = db.scalar(select(RoomCharge).where(RoomCharge.sale_id == sale_id))
    if existing is not None:
        if int(existing.room_id) != int(room_id):
            raise HotelError(
                f"الفاتورة #{sale_id} مربوطة بغرفة أخرى (#{existing.room_id})."
            )
        return existing

    sale = db.get(Sale, sale_id)
    if sale is None:
        raise HotelError("الفاتورة غير موجودة.")
    if sale.context_type != SaleContext.ROOM:
        raise HotelError("هذه الفاتورة ليست طلب شقة.")
    if sale.status != SaleStatus.COMPLETED:
        raise HotelError("الفاتورة يجب أن تكون مكتملة.")
    if sale_outstanding_total(db, sale_id) <= 0:
        raise HotelError("لا يوجد مبلغ مستحق على هذه الفاتورة.")

    room = db.get(HotelRoom, room_id)
    if room is None or not room.is_active:
        raise HotelError("الغرفة غير موجودة أو غير نشطة.")

    snap = (guest_name or "").strip() or None
    booking_id = None
    from modules.hotel.booking_service import active_booking_for_room

    active = active_booking_for_room(db, room_id)
    if active is not None:
        booking_id = active.id
        sale.booking_id = booking_id
        if not snap:
            snap = active.guest_name
    rc = RoomCharge(
        sale_id=sale_id,
        room_id=room_id,
        booking_id=booking_id,
        guest_name_snapshot=snap,
        note=(note or "").strip() or None,
        created_by_id=user_id,
        is_settled=False,
    )
    db.add(rc)
    db.flush()
    if sale_outstanding_total(db, sale_id) <= 0:
        rc.is_settled = True
        rc.settled_at = datetime.now(timezone.utc)
        rc.settled_by_id = user_id
    db.flush()
    return rc


def list_unlinked_room_receivables(db: Session):
    """فواتير شقة عليها رصيد لكن بلا قيد في hotel_room_charges."""
    from modules.receivables.service import build_receivable_rows

    return [
        r
        for r in build_receivable_rows(db, only_with_balance=True)
        if r.context_type == SaleContext.ROOM and r.room_id is None
    ]


def unlinked_room_receivables_total(db: Session) -> Decimal:
    rows = list_unlinked_room_receivables(db)
    total = sum((r.outstanding for r in rows), Decimal("0"))
    return total.quantize(Decimal("0.001"))


def list_charges(
    db: Session,
    *,
    room_id: int | None = None,
    settled: bool | None = None,
    limit: int = 200,
) -> list[RoomCharge]:
    stmt = select(RoomCharge)
    if room_id is not None:
        stmt = stmt.where(RoomCharge.room_id == room_id)
    if settled is not None:
        stmt = stmt.where(RoomCharge.is_settled.is_(settled))
    stmt = stmt.order_by(RoomCharge.id.desc()).limit(limit)
    return list(db.scalars(stmt))


def open_charges_for_room(db: Session, room_id: int) -> list[RoomCharge]:
    return list_charges(db, room_id=room_id, settled=False)


def open_guests_for_room(db: Session, room_id: int) -> list[str]:
    """قائمة بأسماء النزلاء المختلفين الذين عليهم فواتير مفتوحة في الغرفة."""
    seen = []
    for rc in open_charges_for_room(db, room_id):
        name = (rc.guest_name_snapshot or "").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def room_open_total(db: Session, room_id: int) -> Decimal:
    from modules.refunds.service import sale_outstanding_total

    total = Decimal("0")
    for rc in open_charges_for_room(db, room_id):
        total += sale_outstanding_total(db, rc.sale_id)
    return total.quantize(Decimal("0.001"))


def rooms_with_open_balance(db: Session) -> list[tuple[HotelRoom, Decimal, int]]:
    """يرجع كل غرفة عليها رصيد مفتوح + مجموع الرصيد + عدد الفواتير."""
    out: list[tuple[HotelRoom, Decimal, int]] = []
    for room in list_rooms(db):
        open_rows = open_charges_for_room(db, room.id)
        if not open_rows:
            continue
        total = room_open_total(db, room.id)
        if total <= 0:
            continue
        out.append((room, total, len(open_rows)))
    return out


def grand_open_total(db: Session) -> Decimal:
    total = Decimal("0")
    for room, amount, _count in rooms_with_open_balance(db):
        total += amount
    return total.quantize(Decimal("0.001"))


@dataclass(frozen=True)
class PaymentSplit:
    payment_method_id: int
    amount: Decimal


def _booking_prepaid_credit_for_charges(
    db: Session, rows: list[RoomCharge]
) -> tuple[int | None, Decimal]:
    """رصيد الزبون من دفع زائد على الحجز المرتبط بالغرفة."""
    booking_id = None
    for rc in rows:
        if rc.booking_id:
            booking_id = int(rc.booking_id)
            break
    if booking_id is None and rows:
        from modules.hotel.booking_service import active_booking_for_room

        booking = active_booking_for_room(db, int(rows[0].room_id))
        if booking is not None:
            booking_id = int(booking.id)
    if booking_id is None:
        return None, Decimal("0")
    from modules.hotel.folio import build_guest_account

    return booking_id, build_guest_account(db, booking_id).amount_credit


def _default_hotel_settle_payment_method_id(
    db: Session, splits: list[PaymentSplit]
) -> int:
    if splits:
        return int(splits[0].payment_method_id)
    methods = list_hotel_settle_payment_methods(db, only_active=True)
    if not methods:
        raise HotelError("لا توجد وسيلة دفع متاحة لتسوية الرصيد المسبق.")
    return int(methods[0].id)


def settle_charges(
    db: Session,
    *,
    charge_ids: Iterable[int],
    payment_method_id: int,
    user_id: int | None,
    guest_phone: str | None = None,
    guest_name: str | None = None,
    use_loyalty_redeem: bool = False,
) -> list[RoomCharge]:
    """يسوّي الفواتير المختارة دفعة واحدة بأسلوب دفع واحد (كامل المستحق)."""
    from modules.refunds.service import sale_outstanding_total

    ids = [int(i) for i in charge_ids if str(i).strip()]
    if not ids:
        raise HotelError("لم تختر أي فواتير للتسوية.")
    rows = list(db.scalars(select(RoomCharge).where(RoomCharge.id.in_(ids))))
    total_due = Decimal("0")
    for rc in rows:
        if not rc.is_settled:
            total_due += sale_outstanding_total(db, rc.sale_id)
    if total_due <= 0:
        raise HotelError("لا يوجد مبلغ مستحق على الفواتير المختارة.")
    return settle_charges_with_splits(
        db,
        charge_ids=ids,
        payments=[PaymentSplit(payment_method_id, total_due)],
        user_id=user_id,
        guest_phone=guest_phone,
        guest_name=guest_name,
        use_loyalty_redeem=use_loyalty_redeem,
    )


def settle_charges_with_splits(
    db: Session,
    *,
    charge_ids: Iterable[int],
    payments: list[PaymentSplit],
    user_id: int | None,
    guest_phone: str | None = None,
    guest_name: str | None = None,
    use_loyalty_redeem: bool = False,
) -> list[RoomCharge]:
    """تسجيل دفعة أو أكثر (كاش + مصرف + …) على فواتير مختارة — مع دعم الدفع الجزئي."""
    from modules.customers.service import (
        CustomersError,
        attach_customer_to_sale,
        grant_loyalty_after_sale,
        loyalty_settings,
        redeem_points_for_sale,
    )
    from modules.payments.service import PaymentsError
    from modules.payments.service import sum_sale_payments
    from modules.refunds.service import sale_outstanding_total

    try:
        ids = [int(i) for i in charge_ids if str(i).strip()]
    except (TypeError, ValueError) as exc:
        raise HotelError("قائمة الفواتير المختارة غير صالحة.") from exc
    if not ids:
        raise HotelError("لم تختر أي فواتير للتسوية.")

    splits: list[tuple[int, Decimal]] = []
    for p in payments:
        amt = Decimal(str(p.amount or 0)).quantize(Decimal("0.001"))
        if amt <= 0:
            continue
        try:
            from modules.payments.service import assert_hotel_payment_method

            assert_hotel_payment_method(db, p.payment_method_id)
        except Exception as e:
            raise HotelError(str(e)) from e
        splits.append((int(p.payment_method_id), amt))

    total_pay = sum((a for _, a in splits), Decimal("0")).quantize(Decimal("0.001"))

    rows = list(
        db.scalars(
            select(RoomCharge)
            .where(RoomCharge.id.in_(ids))
            .order_by(RoomCharge.created_at.asc(), RoomCharge.id.asc())
        ).all()
    )
    if not rows:
        raise HotelError("الفواتير المختارة غير موجودة.")

    open_rows = [rc for rc in rows if not rc.is_settled]
    if not open_rows:
        raise HotelError("الفواتير المختارة مسوّاة مسبقاً.")

    dues: list[tuple[RoomCharge, Decimal]] = []
    for rc in open_rows:
        sale = db.get(Sale, rc.sale_id)
        if sale is None or sale.status != SaleStatus.COMPLETED:
            continue
        due = sale_outstanding_total(db, sale.id)
        if due > 0:
            dues.append((rc, due))
    total_due = sum((d for _, d in dues), Decimal("0")).quantize(Decimal("0.001"))
    if total_due <= 0:
        raise HotelError("لا يوجد مبلغ مستحق على الفواتير المختارة.")

    prepaid_booking_id, booking_credit = _booking_prepaid_credit_for_charges(
        db, open_rows
    )
    if not splits and booking_credit + Decimal("0.0005") < total_due:
        raise HotelError("أدخل مبلغ دفعة واحد على الأقل.")

    if total_pay > total_due + Decimal("0.0005"):
        raise HotelError(
            f"مجموع الدفعات ({total_pay}) أكبر من المستحق ({total_due})."
        )

    if use_loyalty_redeem and total_pay + booking_credit + Decimal("0.0005") < total_due:
        raise HotelError(
            "صرف النقاط متاح عند تسديد كامل المبلغ المختار في هذه العملية."
        )

    from modules.platform.business_domain import BusinessDomain

    _hotel = BusinessDomain.HOTEL
    loyalty_on = loyalty_settings(db, _hotel)["enabled"]
    cust = None
    if loyalty_on and guest_phone:
        first_sale = db.get(Sale, open_rows[0].sale_id)
        if first_sale:
            try:
                cust = attach_customer_to_sale(
                    db,
                    first_sale,
                    phone=guest_phone,
                    name=guest_name,
                )
                for rc in open_rows[1:]:
                    s = db.get(Sale, rc.sale_id)
                    if s and cust:
                        s.customer_id = cust.id
            except CustomersError as exc:
                raise HotelError(str(exc)) from exc

    pool: list[list] = [[pm_id, amt] for pm_id, amt in splits]
    pool_idx = 0
    now = datetime.now(timezone.utc)
    newly_settled: list[RoomCharge] = []
    credit_pm_id = _default_hotel_settle_payment_method_id(
        db, [PaymentSplit(pm, amt) for pm, amt in splits]
    )

    for rc, _initial_due in dues:
        sale = db.get(Sale, rc.sale_id)
        if sale is None:
            continue
        remaining_due = sale_outstanding_total(db, sale.id)
        if remaining_due <= 0:
            continue

        if booking_credit > Decimal("0.0005") and remaining_due > 0:
            take = min(booking_credit, remaining_due).quantize(Decimal("0.001"))
            if take > 0:
                try:
                    record_sale_payment(db, sale.id, credit_pm_id, take)
                except PaymentsError as exc:
                    raise HotelError(str(exc)) from exc
                if prepaid_booking_id is not None:
                    from modules.hotel.booking_service import (
                        BookingError,
                        apply_prepaid_credit,
                    )

                    try:
                        apply_prepaid_credit(
                            db,
                            prepaid_booking_id,
                            take,
                            note=f"تسوية فاتورة #{sale.id} من رصيد الحجز",
                            user_id=user_id,
                        )
                    except BookingError as exc:
                        raise HotelError(str(exc)) from exc
                booking_credit = (booking_credit - take).quantize(Decimal("0.001"))
                remaining_due = sale_outstanding_total(db, sale.id)

        if (
            loyalty_on
            and cust is not None
            and use_loyalty_redeem
            and remaining_due > 0
        ):
            pool_remaining = sum(
                (p[1] for p in pool[pool_idx:]), Decimal("0")
            )
            if pool_remaining + Decimal("0.0005") >= remaining_due:
                try:
                    _pts, discount = redeem_points_for_sale(
                        db,
                        customer=cust,
                        sale_id=sale.id,
                        sale_total=remaining_due,
                        user_id=user_id,
                        domain=_hotel,
                    )
                    remaining_due = (remaining_due - discount).quantize(
                        Decimal("0.001")
                    )
                    if remaining_due < 0:
                        remaining_due = Decimal("0")
                except CustomersError as exc:
                    raise HotelError(str(exc)) from exc

        while remaining_due > Decimal("0.0005") and pool_idx < len(pool):
            pm_id, pool_amt = pool[pool_idx]
            if pool_amt <= 0:
                pool_idx += 1
                continue
            take = min(pool_amt, remaining_due).quantize(Decimal("0.001"))
            try:
                record_sale_payment(db, sale.id, pm_id, take)
            except PaymentsError as exc:
                raise HotelError(str(exc)) from exc
            pool[pool_idx][1] = (pool_amt - take).quantize(Decimal("0.001"))
            remaining_due = (remaining_due - take).quantize(Decimal("0.001"))
            if pool[pool_idx][1] <= 0:
                pool_idx += 1

        still_due = sale_outstanding_total(db, sale.id)
        if still_due <= Decimal("0.0005"):
            rc.is_settled = True
            rc.settled_at = now
            rc.settled_by_id = user_id
            last_pm = db.scalar(
                select(SalePayment.payment_method_id)
                .where(SalePayment.sale_id == sale.id)
                .order_by(SalePayment.id.desc())
                .limit(1)
            )
            rc.settlement_payment_method_id = last_pm
            newly_settled.append(rc)
            if loyalty_on and cust is not None:
                paid_on_sale = sum_sale_payments(db, sale.id)
                grant_loyalty_after_sale(
                    db,
                    customer=cust,
                    sale_id=sale.id,
                    paid_total=paid_on_sale,
                    user_id=user_id,
                    domain=_hotel,
                )

    leftover = sum((p[1] for p in pool[pool_idx:]), Decimal("0")).quantize(
        Decimal("0.001")
    )
    if leftover > Decimal("0.0005"):
        raise HotelError(
            f"تبقى {leftover} د.ل غير موزّعة — قلّل مبالغ الدفعات أو اختر فواتيراً إضافية."
        )

    db.flush()
    return newly_settled


def settle_room(
    db: Session,
    *,
    room_id: int,
    payment_method_id: int,
    user_id: int | None,
) -> list[RoomCharge]:
    """يسوّي كل الفواتير المفتوحة على غرفة دفعة واحدة."""
    open_ids = [rc.id for rc in open_charges_for_room(db, room_id)]
    if not open_ids:
        raise HotelError("لا توجد فواتير مفتوحة على هذه الغرفة.")
    return settle_charges(
        db,
        charge_ids=open_ids,
        payment_method_id=payment_method_id,
        user_id=user_id,
    )
