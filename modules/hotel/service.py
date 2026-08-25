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
from modules.payments.models import PaymentMethod, PaymentMethodKind, SalePayment
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


def list_occupied_rooms_for_pos(db: Session) -> list[HotelRoom]:
    """شقق مسكونة فقط لنقطة البيع — مع اسم/هاتف نزيل رقم 1 للعرض وواتساب."""
    from sqlalchemy.orm import joinedload

    from modules.hotel.booking_models import BookingStatus, HotelBooking, RoomPhysicalStatus
    from modules.hotel.booking_service import primary_staying_guest_contact

    occupied_ids = {
        int(rid)
        for (rid,) in db.execute(
            select(HotelBooking.room_id).where(
                HotelBooking.booking_status == BookingStatus.CHECKED_IN,
                HotelBooking.room_id.is_not(None),
            )
        ).all()
        if rid is not None
    }
    physical_ids = {
        int(rid)
        for (rid,) in db.execute(
            select(HotelRoom.id).where(
                HotelRoom.is_active.is_(True),
                HotelRoom.physical_status == RoomPhysicalStatus.OCCUPIED,
            )
        ).all()
    }
    ids = occupied_ids | physical_ids
    if not ids:
        return []
    rooms = list(
        db.scalars(
            select(HotelRoom)
            .where(HotelRoom.id.in_(ids), HotelRoom.is_active.is_(True))
            .order_by(HotelRoom.number)
        ).all()
    )
    bookings = list(
        db.scalars(
            select(HotelBooking)
            .options(joinedload(HotelBooking.guests))
            .where(
                HotelBooking.booking_status == BookingStatus.CHECKED_IN,
                HotelBooking.room_id.in_(ids),
            )
            .order_by(HotelBooking.id.desc())
        )
        .unique()
        .all()
    )
    by_room: dict[int, HotelBooking] = {}
    for b in bookings:
        rid = int(b.room_id) if b.room_id else 0
        if rid and rid not in by_room:
            by_room[rid] = b
    for room in rooms:
        booking = by_room.get(int(room.id))
        name, phone = primary_staying_guest_contact(booking)
        if not name:
            name = (room.guest_name or "").strip()
        # للقالب: يظهر نزيل 1 بجانب رقم الشقة
        room.guest_name = name or None
        setattr(room, "pos_guest_name", name or "")
        setattr(room, "pos_guest_phone", phone or "")
        setattr(room, "pos_booking_id", int(booking.id) if booking else None)
    return rooms


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
    lock_no: str | None = None,
    property_id: int = 1,
    show_online: bool = False,
    online_description: str | None = None,
    rooms_count: int | None = None,
    beds_count: int | None = None,
    double_beds_count: int | None = None,
    single_beds_count: int | None = None,
    allows_infant: bool = False,
) -> HotelRoom:
    n = (number or "").strip()
    if not n:
        raise HotelError("رقم الغرفة مطلوب.")
    if get_room_by_number(db, n) is not None:
        raise HotelError(f"الغرفة رقم {n} موجودة بالفعل.")
    if room_type_id is not None and db.get(HotelRoomType, int(room_type_id)) is None:
        raise HotelError("نوع الغرفة المحدد غير موجود.")
    double_n, single_n, total_beds = _resolve_bed_counts(
        double_beds_count, single_beds_count, beds_count
    )
    r = HotelRoom(
        number=n,
        name_ar=(name_ar or "").strip() or None,
        nightly_price=nightly_price,
        image_filename=(image_filename or "").strip() or None,
        guest_name=(guest_name or "").strip() or None,
        notes=(notes or "").strip() or None,
        room_type_id=room_type_id,
        floor=(floor or "").strip() or None,
        lock_no=_normalize_lock_no(lock_no),
        property_id=property_id,
        is_active=True,
        show_online=bool(show_online),
        online_description=(online_description or "").strip() or None,
        rooms_count=_clamp_count(rooms_count, default=1),
        double_beds_count=double_n,
        single_beds_count=single_n,
        beds_count=total_beds,
        allows_infant=bool(allows_infant),
    )
    db.add(r)
    db.flush()
    return r


def _normalize_lock_no(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    # الأقفال عادة 8 خانات رقمية — الإبقاء على الأرقام فقط مع حشو يساري
    digits = "".join(ch for ch in raw if ch.isalnum())
    if not digits:
        return None
    if digits.isdigit() and len(digits) < 8:
        digits = digits.zfill(8)
    return digits[:16]


def _clamp_count(value: int | str | None, *, default: int = 1, max_n: int = 20) -> int:
    try:
        n = int(value) if value is not None and str(value).strip() != "" else default
    except (TypeError, ValueError):
        n = default
    return max(0, min(max_n, n))


def _resolve_bed_counts(
    double_beds_count: int | str | None,
    single_beds_count: int | str | None,
    beds_count: int | str | None = None,
) -> tuple[int, int, int]:
    """يرجع (زوجية، فردية، الإجمالي). إن وُجدت الأنواع الجديدة تُفضَّل على الإجمالي القديم."""
    has_split = double_beds_count is not None or single_beds_count is not None
    if has_split:
        double_n = _clamp_count(double_beds_count, default=0)
        single_n = _clamp_count(single_beds_count, default=0)
    else:
        total = _clamp_count(beds_count, default=1)
        double_n, single_n = total, 0
    return double_n, single_n, double_n + single_n


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
    lock_no: str | None = None,
    physical_status: "RoomPhysicalStatus | None" = None,
    show_online: bool | None = None,
    online_description: str | None = None,
    rooms_count: int | None = None,
    beds_count: int | None = None,
    double_beds_count: int | None = None,
    single_beds_count: int | None = None,
    allows_infant: bool | None = None,
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
    if lock_no is not None:
        r.lock_no = _normalize_lock_no(lock_no)
    if physical_status is not None:
        r.physical_status = physical_status
    if show_online is not None:
        r.show_online = bool(show_online)
    if online_description is not None:
        r.online_description = online_description.strip() or None
    if rooms_count is not None:
        r.rooms_count = _clamp_count(rooms_count, default=1)
    if double_beds_count is not None or single_beds_count is not None:
        double_n, single_n, total_beds = _resolve_bed_counts(
            double_beds_count
            if double_beds_count is not None
            else getattr(r, "double_beds_count", None),
            single_beds_count
            if single_beds_count is not None
            else getattr(r, "single_beds_count", None),
        )
        r.double_beds_count = double_n
        r.single_beds_count = single_n
        r.beds_count = total_beds
    elif beds_count is not None:
        double_n, single_n, total_beds = _resolve_bed_counts(None, None, beds_count)
        r.double_beds_count = double_n
        r.single_beds_count = single_n
        r.beds_count = total_beds
    if allows_infant is not None:
        r.allows_infant = bool(allows_infant)
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

    # حد دين الشركة عند تحميل خدمات على حساب الشركة
    if booking_id is not None:
        from modules.customers.company_credit import (
            CompanyCreditError,
            assert_company_can_accept_debt,
        )
        from modules.customers.models import Customer
        from modules.hotel.booking_service import get_booking
        from modules.hotel.company_agreement_service import (
            allocate_charge,
            infer_service_code,
        )
        from modules.hotel.folio import is_laundry_service
        from modules.hotel.breakfast_settle import sale_is_hotel_breakfast

        booking_pre = get_booking(db, int(booking_id))
        sale_total = Decimal(str(sale.total or 0)).quantize(Decimal("0.001"))
        code = infer_service_code(
            name_ar=note or "",
            is_pos=True,
            is_laundry=is_laundry_service(note or ""),
            is_breakfast=sale_is_hotel_breakfast(db, sale),
        )
        split = allocate_charge(
            db,
            int(booking_id),
            amount=sale_total,
            service_code=code,
            name_ar=f"فاتورة #{sale_id}",
            consume_limit=True,
        )
        if split.company_amount > Decimal("0.0005"):
            cid = getattr(booking_pre, "company_customer_id", None) if booking_pre else None
            if cid:
                try:
                    assert_company_can_accept_debt(
                        db,
                        db.get(Customer, int(cid)),
                        split.company_amount,
                        exclude_booking_id=int(booking_id),
                    )
                except CompanyCreditError as exc:
                    raise HotelError(str(exc)) from exc
    else:
        split = None
        code = None

    rc = RoomCharge(
        sale_id=sale_id,
        room_id=room_id,
        booking_id=booking_id,
        guest_name_snapshot=snap,
        note=(note or "").strip() or None,
        created_by_id=user_id,
        is_settled=False,
        service_code=split.service_code if split else code,
        folio_side=split.folio_side if split else None,
        company_amount=split.company_amount if split else None,
        guest_amount=split.guest_amount if split else None,
    )
    db.add(rc)
    db.flush()
    if booking_id is not None:
        try:
            from modules.hotel.booking_service import (
                _recalc_payment_status,
                get_booking,
            )
            from modules.notifications.hotel_hooks import emit_hotel_unpaid_service_added

            booking = get_booking(db, int(booking_id))
            if booking is not None:
                _recalc_payment_status(db, booking)
                # إشعار فقط إن كان جزء على النزيل أو إعلام الشركة
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
    if booking_id is not None:
        try:
            from modules.hotel.booking_service import _recalc_payment_status, get_booking

            booking = get_booking(db, int(booking_id))
            if booking is not None:
                _recalc_payment_status(db, booking)
        except Exception:  # noqa: BLE001
            pass
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


def room_open_total(
    db: Session,
    room_id: int,
    *,
    include_hotel_breakfast: bool = True,
) -> Decimal:
    from modules.refunds.service import sale_outstanding_total

    total = Decimal("0")
    for rc in open_charges_for_room(db, room_id):
        if not include_hotel_breakfast:
            from modules.hotel.breakfast_settle import sale_is_hotel_breakfast

            if sale_is_hotel_breakfast(db, rc.sale):
                continue
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


@dataclass(frozen=True)
class SettleIndexCharge:
    """صف فاتورة مفتوحة لعرض التسوية الجماعية على صفحة القائمة."""

    charge_id: int
    sale_id: int
    outstanding: Decimal
    guest_name: str
    booking_id: int | None
    can_settle: bool
    is_breakfast: bool


def settle_index_charges_by_room(
    db: Session,
) -> dict[int, list[SettleIndexCharge]]:
    """فواتير مفتوحة لكل غرفة ذات رصيد — مع صلاحية التسوية (ربط حجز)."""
    from modules.hotel.breakfast_settle import sale_is_hotel_breakfast
    from modules.refunds.service import sale_outstanding_total

    out: dict[int, list[SettleIndexCharge]] = {}
    for room, _total, _count in rooms_with_open_balance(db):
        rows: list[SettleIndexCharge] = []
        for rc in open_charges_for_room(db, room.id):
            due = sale_outstanding_total(db, rc.sale_id).quantize(Decimal("0.001"))
            if due <= Decimal("0.0005"):
                continue
            sale = rc.sale
            if sale is None:
                sale = db.get(Sale, int(rc.sale_id))
            booking_id = _resolve_booking_id_for_charge(db, rc)
            rows.append(
                SettleIndexCharge(
                    charge_id=int(rc.id),
                    sale_id=int(rc.sale_id),
                    outstanding=due,
                    guest_name=(rc.guest_name_snapshot or "").strip(),
                    booking_id=int(booking_id) if booking_id is not None else None,
                    can_settle=booking_id is not None,
                    is_breakfast=bool(
                        sale is not None and sale_is_hotel_breakfast(db, sale)
                    ),
                )
            )
        out[int(room.id)] = rows
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


@dataclass(frozen=True)
class TreasuryTransferSplit:
    """سطر تحويل فندق→مطعم عند التسوية (قد يُقسَّم كاش + مصرف)."""

    from_payment_method_id: int
    to_payment_method_id: int
    amount: Decimal
    bank_ref: str = ""


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
            from modules.authz.models import User
            from modules.payments.service import assert_hotel_payment_method

            pay_user = db.get(User, int(user_id)) if user_id else None
            assert_hotel_payment_method(db, p.payment_method_id, user=pay_user)
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
    from modules.hotel.restaurant_settle import (
        ensure_room_settle_clearing_pm,
        pick_hotel_treasury_with_balance,
        transfer_hotel_to_restaurant_for_meal,
    )

    # رصيد النزيل موجود نقداً في خزينة الفندق مسبقاً → نحوّل للمطعم ونُقفل الفاتورة بمقاصة
    clearing_pm_id = int(ensure_room_settle_clearing_pm(db).id)

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
                try:
                    src_pm = pick_hotel_treasury_with_balance(db, take)
                    transfer_hotel_to_restaurant_for_meal(
                        db,
                        amount=take,
                        from_payment_method_id=int(src_pm.id),
                        user_id=user_id,
                        sale_id=sale.id,
                        room_id=rc.room_id,
                    )
                except PaymentsError as exc:
                    raise HotelError(
                        f"تعذّر تحويل المبلغ من خزينة الفندق إلى المطعم: {exc}"
                    ) from exc
                try:
                    # إقفال فاتورة المطعم دون إضافة نقد لخزينة أخرى (النقد انتقل بالتحويل)
                    record_sale_payment(
                        db, sale.id, clearing_pm_id, take, for_hotel_settle=True
                    )
                except PaymentsError as exc:
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
                record_sale_payment(
                    db, sale.id, pm_id, take, for_hotel_settle=True
                )
            except PaymentsError as exc:
                raise HotelError(str(exc)) from exc
            try:
                transfer_hotel_to_restaurant_for_meal(
                    db,
                    amount=take,
                    from_payment_method_id=pm_id,
                    user_id=user_id,
                    sale_id=sale.id,
                    room_id=rc.room_id,
                )
            except PaymentsError as exc:
                raise HotelError(
                    f"تعذّر تحويل المبلغ من خزينة الفندق إلى المطعم: {exc}"
                ) from exc
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


def _resolve_booking_id_for_charge(db: Session, rc: RoomCharge) -> int | None:
    if rc.booking_id:
        return int(rc.booking_id)
    from modules.hotel.booking_service import active_booking_for_room

    active = active_booking_for_room(db, int(rc.room_id))
    if active is not None:
        return int(active.id)
    # حجز واحد مؤكد/مقيم على الشقة يكفي للتسوية دون ربط يدوي مسبق
    linkable = list_linkable_bookings_for_room(db, int(rc.room_id), limit=5)
    if len(linkable) == 1:
        return int(linkable[0].id)
    return None


def list_linkable_bookings_for_room(db: Session, room_id: int, *, limit: int = 40):
    """حجوزات يمكن ربط فواتير الشقة بها (مؤكد أو مقيم)."""
    from modules.hotel.booking_models import BookingStatus, HotelBooking

    return list(
        db.scalars(
            select(HotelBooking)
            .where(
                HotelBooking.room_id == int(room_id),
                HotelBooking.booking_status.in_(
                    (BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN)
                ),
            )
            .order_by(HotelBooking.check_in.desc(), HotelBooking.id.desc())
            .limit(limit)
        ).all()
    )


def link_open_charges_to_booking(
    db: Session,
    *,
    room_id: int,
    booking_id: int,
    charge_ids: Iterable[int] | None = None,
) -> int:
    """يربط فواتير مفتوحة على الشقة بحجز (للتسوية كدين على النزيل)."""
    from modules.hotel.booking_models import BookingStatus
    from modules.hotel.booking_service import get_booking

    booking = get_booking(db, int(booking_id))
    if booking is None:
        raise HotelError("الحجز غير موجود.")
    if int(booking.room_id or 0) != int(room_id):
        raise HotelError("الحجز ليس على نفس الشقة.")
    if booking.booking_status not in (
        BookingStatus.CONFIRMED,
        BookingStatus.CHECKED_IN,
    ):
        raise HotelError("اربط بحجز مؤكد أو مقيم فقط.")

    open_rows = open_charges_for_room(db, int(room_id))
    if charge_ids is not None:
        want = {int(i) for i in charge_ids if str(i).strip()}
        open_rows = [rc for rc in open_rows if int(rc.id) in want]
    if not open_rows:
        raise HotelError("لا توجد فواتير مفتوحة للربط.")

    guest = (booking.guest_name or "").strip() or None
    n = 0
    for rc in open_rows:
        rc.booking_id = int(booking.id)
        if not (rc.guest_name_snapshot or "").strip() and guest:
            rc.guest_name_snapshot = guest
        sale = db.get(Sale, rc.sale_id)
        if sale is not None:
            sale.booking_id = int(booking.id)
        n += 1
    db.flush()
    try:
        from modules.hotel.booking_service import _recalc_payment_status

        _recalc_payment_status(db, booking)
    except Exception:  # noqa: BLE001
        pass
    return n


def convert_open_charges_to_restaurant_pickup(
    db: Session,
    *,
    room_id: int,
    payment_method_id: int,
    charge_ids: Iterable[int] | None = None,
    user_id: int | None = None,  # noqa: ARG001 — للتوافق مع مسار الواجهة/التدقيق لاحقاً
) -> int:
    """يحوّل فواتير قيد الشقة إلى طلب استلام من المطعم + تحصيل فوري.

    يغيّر نوع الطلب فعلياً إلى EXTERNAL/PICKUP، يحذف قيد الشقة،
    ويسجّل دفعة على خزينة المطعم للمبلغ المتبقي.
    """
    from modules.payments.service import (
        PaymentsError,
        is_pos_sale_payment_method,
        record_sale_payment,
    )
    from modules.refunds.service import sale_outstanding_total
    from modules.sales.models import ExternalOrderType, SaleContext

    pm = db.get(PaymentMethod, int(payment_method_id))
    if not is_pos_sale_payment_method(pm):
        raise HotelError("اختر وسيلة دفع صالحة لتحصيل المطعم.")

    open_rows = open_charges_for_room(db, int(room_id))
    if charge_ids is not None:
        want = {int(i) for i in charge_ids if str(i).strip()}
        open_rows = [rc for rc in open_rows if int(rc.id) in want]
    if not open_rows:
        raise HotelError("لا توجد فواتير مفتوحة للتحويل.")

    n = 0
    for rc in open_rows:
        if rc.is_settled:
            continue
        sale = db.get(Sale, rc.sale_id)
        if sale is None or sale.status != SaleStatus.COMPLETED:
            raise HotelError(f"الفاتورة #{rc.sale_id} غير صالحة للتحويل.")
        due = sale_outstanding_total(db, sale.id).quantize(Decimal("0.001"))
        if due <= Decimal("0.0005"):
            db.delete(rc)
            n += 1
            continue

        sale.context_type = SaleContext.EXTERNAL
        sale.external_order_type = ExternalOrderType.PICKUP
        sale.booking_id = None
        sale.table_id = None
        sale.delivery_zone_id = None
        sale.delivery_zone_name = None
        sale.delivery_fee = Decimal("0")
        db.delete(rc)
        db.flush()
        try:
            record_sale_payment(
                db,
                sale.id,
                int(payment_method_id),
                due,
            )
        except PaymentsError as exc:
            raise HotelError(str(exc)) from exc
        n += 1
    db.flush()
    return n


def _ensure_booking_service_for_settled_sale(
    db: Session,
    *,
    booking_id: int,
    sale: Sale,
    amount: Decimal,
    user_id: int | None,
    charged_to_guest: bool = True,
) -> None:
    """يسجّل فاتورة المطعم على الحجز بعد التسوية مع المطعم.

    charged_to_guest=True  → مستحق على النزيل
    charged_to_guest=False → تكلفة فندق (إفطار مشمول) بلا مطالبة للنزيل
    """
    from modules.hotel.booking_models import HotelBookingService
    from modules.hotel.booking_service import BookingError, add_booking_service
    from modules.hotel.folio import folio_pos_charge_description

    existing = db.scalar(
        select(HotelBookingService.id).where(
            HotelBookingService.booking_id == int(booking_id),
            HotelBookingService.sale_id == int(sale.id),
        )
    )
    if existing is not None:
        return
    label = folio_pos_charge_description(
        db, sale, int(sale.id), amount, include_item_details=False
    )
    room_label = ""
    if not charged_to_guest:
        from modules.hotel.booking_models import HotelBooking

        booking = db.get(HotelBooking, int(booking_id))
        room = None
        if booking is not None and booking.room_id:
            room = db.get(HotelRoom, int(booking.room_id))
        room_label = (room.number if room else None) or (
            str(booking.room_id) if booking and booking.room_id else "—"
        )
        if "إفطار" not in label and "افطار" not in label:
            label = f"إفطار مشمول — شقة #{room_label} — {label}"
        else:
            label = f"{label} — شقة #{room_label}"
    note = (
        f"تسوية فندق→مطعم · فاتورة #{sale.id}"
        if charged_to_guest
        else (
            f"تكلفة إفطار فندق على حساب شقة #{room_label} "
            f"· فاتورة #{sale.id} (غير على النزيل)"
        )
    )
    try:
        add_booking_service(
            db,
            int(booking_id),
            name_ar=label,
            quantity=Decimal("1"),
            unit_price=amount,
            sale_id=int(sale.id),
            notes=note,
            user_id=user_id,
            notify=False,
            charged_to_guest=charged_to_guest,
        )
    except BookingError as exc:
        raise HotelError(str(exc)) from exc


def _collect_settle_dues(
    db: Session,
    charge_ids: Iterable[int],
    *,
    hotel_breakfast_only: bool | None = None,
) -> list[tuple[RoomCharge, Sale, Decimal, int]]:
    """يجمع الفواتير المستحقة للتسوية مع التحقق من نوع الإفطار عند الحاجة."""
    from modules.hotel.breakfast_settle import sale_is_hotel_breakfast
    from modules.refunds.service import sale_outstanding_total

    try:
        ids = [int(i) for i in charge_ids if str(i).strip()]
    except (TypeError, ValueError) as exc:
        raise HotelError("قائمة الفواتير المختارة غير صالحة.") from exc
    if not ids:
        raise HotelError("لم تختر أي فواتير للتسوية.")

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

    dues: list[tuple[RoomCharge, Sale, Decimal, int]] = []
    for rc in open_rows:
        sale = db.get(Sale, rc.sale_id)
        if sale is None or sale.status != SaleStatus.COMPLETED:
            continue
        due = sale_outstanding_total(db, sale.id).quantize(Decimal("0.001"))
        if due <= Decimal("0.0005"):
            continue
        is_bf = sale_is_hotel_breakfast(db, sale)
        if hotel_breakfast_only is True and not is_bf:
            raise HotelError(
                f"الفاتورة #{sale.id} ليست إفطاراً مشمولاً — "
                "استخدم زر «تسوية» العادي لوجبات النزيل."
            )
        if hotel_breakfast_only is False and is_bf:
            raise HotelError(
                f"الفاتورة #{sale.id} إفطار مشمول — "
                "استخدم زر «تسوية إفطار (تكلفة فندق)» حتى لا تُضاف على حساب النزيل."
            )
        booking_id = _resolve_booking_id_for_charge(db, rc)
        if booking_id is None:
            raise HotelError(
                f"الفاتورة #{sale.id} غير مرتبطة بحجز نشط — اربطها بحجز قبل التسوية."
            )
        if rc.booking_id is None:
            rc.booking_id = booking_id
            sale.booking_id = booking_id
        dues.append((rc, sale, due, booking_id))

    if not dues:
        raise HotelError("لا يوجد مبلغ مستحق على الفواتير المختارة.")
    return dues


def settle_charges_to_room_account(
    db: Session,
    *,
    charge_ids: Iterable[int],
    user_id: int | None,
    from_payment_method_id: int | None = None,
    transfers: list[TreasuryTransferSplit] | None = None,
) -> list[RoomCharge]:
    """تسوية فندق→مطعم لوجبات النزيل (تُحصَّل لاحقاً من حساب الغرفة)."""
    return _settle_charges_hotel_to_restaurant(
        db,
        charge_ids=charge_ids,
        user_id=user_id,
        from_payment_method_id=from_payment_method_id,
        transfers=transfers,
        charged_to_guest=True,
        hotel_breakfast_only=False,
    )


def settle_charges_as_hotel_breakfast_cost(
    db: Session,
    *,
    charge_ids: Iterable[int],
    user_id: int | None,
    from_payment_method_id: int | None = None,
    transfers: list[TreasuryTransferSplit] | None = None,
) -> list[RoomCharge]:
    """تسوية إفطار مشمول: تحويل للمطعم + تسجيل تكلفة فندق بلا مطالبة للنزيل."""
    return _settle_charges_hotel_to_restaurant(
        db,
        charge_ids=charge_ids,
        user_id=user_id,
        from_payment_method_id=from_payment_method_id,
        transfers=transfers,
        charged_to_guest=False,
        hotel_breakfast_only=True,
    )


def _normalize_settle_transfers(
    db: Session,
    *,
    total_due: Decimal,
    from_payment_method_id: int | None,
    transfers: list[TreasuryTransferSplit] | None,
    require_operation_ref: bool = True,
) -> list[TreasuryTransferSplit]:
    """يبني أسطر التحويل ويتحقق من المجموع والرصيد."""
    from modules.payments.service import (
        PaymentsError,
        assert_bank_operation_ref,
        method_current_balance,
    )
    from modules.hotel.restaurant_settle import (
        is_restaurant_settle_wallet,
        is_settle_source_wallet,
        pick_hotel_treasury_with_balance,
        restaurant_settle_target_pm,
    )

    rows: list[TreasuryTransferSplit] = []
    if transfers:
        for t in transfers:
            amt = Decimal(str(t.amount or 0)).quantize(Decimal("0.001"))
            if amt <= Decimal("0.0005"):
                continue
            ref = (getattr(t, "bank_ref", "") or "").strip()
            if not ref:
                raise HotelError(
                    "أدخل رقم المرجع من حساب توا بعد تنفيذ التحويل خارج النظام، "
                    "ثم سجّله هنا. لا يُحفظ التحويل بدون مرجع يؤكد التنفيذ الفعلي."
                )
            rows.append(
                TreasuryTransferSplit(
                    from_payment_method_id=int(t.from_payment_method_id),
                    to_payment_method_id=int(t.to_payment_method_id),
                    amount=amt,
                    bank_ref=ref,
                )
            )
    elif from_payment_method_id:
        src = db.get(PaymentMethod, int(from_payment_method_id))
        if src is None or not is_settle_source_wallet(src, allow_main=True):
            raise HotelError(
                "اختر خزينة فندق أو الخزينة الرئيسية (كاش أو مصرف) كمصدر للتحويل."
            )
        kind = (
            src.kind
            if src.kind in (PaymentMethodKind.CASH, PaymentMethodKind.BANK)
            else PaymentMethodKind.CASH
        )
        to_pm = restaurant_settle_target_pm(db, kind=kind)
        rows = [
            TreasuryTransferSplit(
                from_payment_method_id=int(src.id),
                to_payment_method_id=int(to_pm.id),
                amount=total_due,
            )
        ]
    else:
        try:
            src = pick_hotel_treasury_with_balance(db, total_due, allow_main=True)
        except PaymentsError as exc:
            raise HotelError(str(exc)) from exc
        kind = (
            src.kind
            if src.kind in (PaymentMethodKind.CASH, PaymentMethodKind.BANK)
            else PaymentMethodKind.CASH
        )
        to_pm = restaurant_settle_target_pm(db, kind=kind)
        rows = [
            TreasuryTransferSplit(
                from_payment_method_id=int(src.id),
                to_payment_method_id=int(to_pm.id),
                amount=total_due,
            )
        ]

    if not rows:
        raise HotelError("أدخل سطر تحويل واحد على الأقل (من / إلى / المبلغ).")

    xfer_total = sum((r.amount for r in rows), Decimal("0")).quantize(Decimal("0.001"))
    if abs(xfer_total - total_due) > Decimal("0.001"):
        raise HotelError(
            f"مجموع التحويلات ({xfer_total} د.ل) يجب أن يساوي "
            f"مستحق الفواتير المختارة ({total_due} د.ل)."
        )

    by_from: dict[int, Decimal] = {}
    for r in rows:
        from_pm = db.get(PaymentMethod, r.from_payment_method_id)
        to_pm = db.get(PaymentMethod, r.to_payment_method_id)
        if from_pm is None or not is_settle_source_wallet(from_pm, allow_main=True):
            raise HotelError(
                "مصدر التحويل يجب أن يكون خزينة فندق أو الخزينة الرئيسية."
            )
        if to_pm is None or not is_restaurant_settle_wallet(to_pm):
            raise HotelError("وجهة التحويل يجب أن تكون خزينة مطعم.")
        if require_operation_ref and not (r.bank_ref or "").strip():
            raise HotelError(
                "أدخل رقم المرجع من حساب توا بعد تنفيذ التحويل خارج النظام، "
                "ثم سجّله هنا. لا يُحفظ التحويل بدون مرجع يؤكد التنفيذ الفعلي."
            )
        if require_operation_ref:
            try:
                assert_bank_operation_ref(from_pm, to_pm, r.bank_ref)
            except PaymentsError as exc:
                raise HotelError(str(exc)) from exc
        by_from[r.from_payment_method_id] = (
            by_from.get(r.from_payment_method_id, Decimal("0")) + r.amount
        )

    for pm_id, need in by_from.items():
        bal = method_current_balance(db, int(pm_id))
        if bal + Decimal("0.0005") < need:
            pm = db.get(PaymentMethod, pm_id)
            name = pm.name_ar if pm else f"#{pm_id}"
            raise HotelError(
                f"رصيد «{name}» غير كافٍ ({bal} د.ل) لتحويل {need} د.ل."
            )
    return rows


def _settle_charges_hotel_to_restaurant(
    db: Session,
    *,
    charge_ids: Iterable[int],
    user_id: int | None,
    from_payment_method_id: int | None,
    charged_to_guest: bool,
    hotel_breakfast_only: bool | None,
    transfers: list[TreasuryTransferSplit] | None = None,
) -> list[RoomCharge]:
    """تحويل خزينة (قد يُقسَّم) + إقفال فاتورة مطعم + قيد على الحجز.

    إقفال فاتورة المطعم بالمقاصة ≠ سداد من النزيل — الدين يبقى على الحجز
    عندما ``charged_to_guest=True``.
    """
    from modules.payments.service import PaymentsError
    from modules.hotel.restaurant_settle import (
        ensure_room_settle_clearing_pm,
        transfer_hotel_to_restaurant_for_meal,
    )

    dues = _collect_settle_dues(
        db, charge_ids, hotel_breakfast_only=hotel_breakfast_only
    )
    total_due = sum((d for _, _, d, _ in dues), Decimal("0")).quantize(Decimal("0.001"))
    xfer_rows = _normalize_settle_transfers(
        db,
        total_due=total_due,
        from_payment_method_id=from_payment_method_id,
        transfers=transfers,
        require_operation_ref=not (
            hotel_breakfast_only is True and not transfers and not from_payment_method_id
        ),
    )

    room_id_hint = dues[0][0].room_id if dues else None
    try:
        auto_breakfast = (
            hotel_breakfast_only is True
            and not transfers
            and not from_payment_method_id
        )
        for row in xfer_rows:
            transfer_hotel_to_restaurant_for_meal(
                db,
                amount=row.amount,
                from_payment_method_id=row.from_payment_method_id,
                to_payment_method_id=row.to_payment_method_id,
                user_id=user_id,
                sale_id=None,
                room_id=room_id_hint,
                bank_ref=row.bank_ref,
                require_operation_ref=False if auto_breakfast else None,
            )
    except PaymentsError as exc:
        raise HotelError(
            f"تعذّر تحويل المبلغ من خزينة الفندق إلى المطعم: {exc}"
        ) from exc

    clearing_pm_id = int(ensure_room_settle_clearing_pm(db).id)
    now = datetime.now(timezone.utc)
    newly_settled: list[RoomCharge] = []
    primary_from = int(xfer_rows[0].from_payment_method_id)

    for rc, sale, due, booking_id in dues:
        try:
            record_sale_payment(
                db, sale.id, clearing_pm_id, due, for_hotel_settle=True
            )
        except PaymentsError as exc:
            raise HotelError(str(exc)) from exc

        _ensure_booking_service_for_settled_sale(
            db,
            booking_id=booking_id,
            sale=sale,
            amount=due,
            user_id=user_id,
            charged_to_guest=charged_to_guest,
        )

        rc.is_settled = True
        rc.settled_at = now
        rc.settled_by_id = user_id
        rc.settlement_payment_method_id = primary_from
        newly_settled.append(rc)

    db.flush()
    return newly_settled
