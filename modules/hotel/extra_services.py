"""إدارة قائمة الخدمات الإضافية للحجز."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.hotel.booking_models import HotelServiceCatalog
from modules.hotel.booking_service import BookingError


def list_service_catalog(
    db: Session, *, only_active: bool = True
) -> list[HotelServiceCatalog]:
    q = select(HotelServiceCatalog).order_by(
        HotelServiceCatalog.sort_order, HotelServiceCatalog.name_ar
    )
    if only_active:
        q = q.where(HotelServiceCatalog.is_active.is_(True))
    return list(db.scalars(q).all())


def get_service_catalog_item(db: Session, item_id: int) -> HotelServiceCatalog | None:
    return db.get(HotelServiceCatalog, item_id)


def save_service_catalog_item(
    db: Session,
    *,
    name_ar: str,
    default_price: Decimal,
    item_id: int | None = None,
    sort_order: int = 0,
    is_active: bool = True,
    product_id: int | None = None,
) -> HotelServiceCatalog:
    name = (name_ar or "").strip()
    if not name:
        raise BookingError("اسم الخدمة مطلوب.")
    price = Decimal(str(default_price)).quantize(Decimal("0.001"))
    if price < 0:
        raise BookingError("السعر لا يمكن أن يكون سالباً.")
    if item_id:
        item = db.get(HotelServiceCatalog, item_id)
        if item is None:
            raise BookingError("الخدمة غير موجودة.")
    else:
        item = HotelServiceCatalog()
        db.add(item)
    item.name_ar = name
    item.default_price = price
    item.sort_order = int(sort_order or 0)
    item.is_active = bool(is_active)
    item.product_id = product_id
    db.flush()
    return item


def set_service_catalog_active(db: Session, item_id: int, *, active: bool) -> None:
    item = db.get(HotelServiceCatalog, item_id)
    if item is None:
        raise BookingError("الخدمة غير موجودة.")
    item.is_active = active


def delete_service_catalog_item(db: Session, item_id: int) -> None:
    item = db.get(HotelServiceCatalog, item_id)
    if item is None:
        raise BookingError("الخدمة غير موجودة.")
    db.delete(item)
