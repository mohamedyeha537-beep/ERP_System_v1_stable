"""سائقو التوصيل وتسليم الطلبات."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.delivery.models import DeliveryDriver, DeliveryHandoff
from modules.sales.models import ExternalOrderType, Sale, SaleContext


class DeliveryDriverError(Exception):
    pass


def driver_name_from_label(driver_label: str | None) -> str | None:
    if not (driver_label or "").strip():
        return None
    return driver_label.split(" — ", 1)[0].strip() or None


def driver_button_label(full_name: str | None, *, max_full_len: int = 10) -> str:
    """عنوان زر السائق — الاسم الأول إن كان الاسم طويلاً."""
    if not (full_name or "").strip():
        return "تسليم السائق"
    name = full_name.strip()
    if len(name) <= max_full_len:
        return name
    parts = name.split()
    return parts[0] if parts else name[:max_full_len]


def _norm_phone(phone: str) -> str:
    p = re.sub(r"\D", "", (phone or "").strip())
    if not p:
        raise DeliveryDriverError("رقم هاتف السائق مطلوب.")
    if len(p) < 9:
        raise DeliveryDriverError("رقم الهاتف قصير جداً.")
    return p


def list_recent_drivers(db: Session, *, limit: int = 40) -> list[DeliveryDriver]:
    return list(
        db.scalars(
            select(DeliveryDriver)
            .where(DeliveryDriver.is_active.is_(True))
            .order_by(
                DeliveryDriver.last_used_at.is_(None),
                DeliveryDriver.last_used_at.desc(),
                DeliveryDriver.use_count.desc(),
                DeliveryDriver.name,
            )
            .limit(limit)
        ).all()
    )


def upsert_driver(db: Session, *, name: str, phone: str) -> DeliveryDriver:
    nm = (name or "").strip()
    if not nm:
        raise DeliveryDriverError("اسم السائق مطلوب.")
    ph = _norm_phone(phone)
    row = db.scalars(
        select(DeliveryDriver).where(DeliveryDriver.phone == ph).limit(1)
    ).first()
    now = datetime.now(timezone.utc)
    if row is None:
        row = DeliveryDriver(name=nm, phone=ph, use_count=1, last_used_at=now)
        db.add(row)
    else:
        row.name = nm
        row.use_count = int(row.use_count or 0) + 1
        row.last_used_at = now
        row.is_active = True
    db.flush()
    return row


def get_handoff(db: Session, sale_id: int) -> DeliveryHandoff | None:
    return db.scalars(
        select(DeliveryHandoff).where(DeliveryHandoff.sale_id == sale_id).limit(1)
    ).first()


def assign_driver_to_sale(
    db: Session,
    sale: Sale,
    *,
    name: str,
    phone: str,
    user_id: int | None,
    note: str | None = None,
) -> DeliveryHandoff:
    if sale.context_type != SaleContext.EXTERNAL:
        raise DeliveryDriverError("تسليم السائق لطلبات التوصيل الخارجية فقط.")
    if sale.external_order_type != ExternalOrderType.DELIVERY:
        raise DeliveryDriverError("هذا الطلب ليس توصيلاً.")
    driver = upsert_driver(db, name=name, phone=phone)
    existing = get_handoff(db, sale.id)
    if existing is not None:
        existing.driver_id = driver.id
        existing.driver_name = driver.name
        existing.driver_phone = driver.phone
        existing.note = (note or "").strip() or None
        existing.handed_at = datetime.now(timezone.utc)
        db.flush()
        ho = existing
    else:
        ho = DeliveryHandoff(
            sale_id=sale.id,
            driver_id=driver.id,
            driver_name=driver.name,
            driver_phone=driver.phone,
            created_by_id=user_id,
            note=(note or "").strip() or None,
        )
        db.add(ho)
        db.flush()
    try:
        from modules.notifications.hooks import (
            emit_order_driver_assigned,
            emit_order_out_for_delivery,
        )

        emit_order_driver_assigned(
            db,
            sale,
            driver_name=driver.name,
            driver_phone=driver.phone or "",
            driver_id=driver.id,
        )
        emit_order_out_for_delivery(db, sale, driver_name=driver.name)
    except Exception:  # noqa: BLE001
        pass
    return ho
