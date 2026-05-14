"""Hotel rooms & deferred payment business logic."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.hotel.models import HotelRoom, RoomCharge
from modules.payments.models import PaymentMethod, SalePayment
from modules.payments.service import record_sale_payment
from modules.sales.models import Sale, SaleStatus


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
) -> HotelRoom:
    n = (number or "").strip()
    if not n:
        raise HotelError("رقم الغرفة مطلوب.")
    if get_room_by_number(db, n) is not None:
        raise HotelError(f"الغرفة رقم {n} موجودة بالفعل.")
    r = HotelRoom(
        number=n,
        guest_name=(guest_name or "").strip() or None,
        notes=(notes or "").strip() or None,
        is_active=True,
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
    is_active: bool | None = None,
) -> HotelRoom:
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
    if is_active is not None:
        r.is_active = is_active
    db.flush()
    return r


def delete_room(db: Session, room_id: int) -> None:
    r = db.get(HotelRoom, room_id)
    if r is None:
        return
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
) -> RoomCharge:
    """يفتح حساب غرفة على فاتورة بيع مكتملة (بدون دفع).

    اسم العميل مطلوب لكل قيد لأن النزلاء يتغيّرون مع الوقت
    (الغرفة ثابتة لكن قد ينزلها أكثر من شخص خلال اليوم).
    لا نُحدِّث `room.guest_name` (الغرفة لا تحمل نزيلاً ثابتاً) — كل قيد
    يحفظ اسم العميل في `guest_name_snapshot` الخاص به.
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
        raise HotelError("اسم العميل/النزيل مطلوب عند القيد على غرفة.")

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

    rc = RoomCharge(
        sale_id=sale_id,
        room_id=room_id,
        guest_name_snapshot=snap,
        note=(note or "").strip() or None,
        created_by_id=user_id,
        is_settled=False,
    )
    db.add(rc)
    db.flush()
    return rc


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
    stmt = stmt.order_by(RoomCharge.created_at.desc()).limit(limit)
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


def settle_charges(
    db: Session,
    *,
    charge_ids: Iterable[int],
    payment_method_id: int,
    user_id: int | None,
) -> list[RoomCharge]:
    """يسوّي مجموعة من الفواتير المعلّقة، ويسجّل الدفع لكل واحدة بالأسلوب المختار."""
    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active:
        raise HotelError("أسلوب الدفع غير صالح.")

    ids = [int(i) for i in charge_ids if str(i).strip()]
    if not ids:
        raise HotelError("لم تختر أي فواتير للتسوية.")

    rows = list(db.scalars(select(RoomCharge).where(RoomCharge.id.in_(ids))))
    if not rows:
        raise HotelError("الفواتير المختارة غير موجودة.")

    settled = []
    now = datetime.now(timezone.utc)
    from modules.refunds.service import sale_outstanding_total

    for rc in rows:
        if rc.is_settled:
            continue
        sale = db.get(Sale, rc.sale_id)
        if sale is None or sale.status != SaleStatus.COMPLETED:
            continue
        due = sale_outstanding_total(db, sale.id)
        if due > 0:
            record_sale_payment(db, sale.id, payment_method_id, due)
        rc.is_settled = True
        rc.settled_at = now
        rc.settled_by_id = user_id
        rc.settlement_payment_method_id = payment_method_id if due > 0 else None
        settled.append(rc)

    db.flush()
    return settled


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
