from __future__ import annotations

import enum
import json
import logging
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import Integer, event, select
from sqlalchemy.orm import Session as ORMSession

from infra.config import get_settings
from infra.db import Base
from modules.sales.service import SalesError, create_online_sale_completed
from modules.sync.models import (
    SyncEvent,
    SyncEventAction,
    SyncEventStatus,
    SyncIncomingEvent,
    SyncRecordMap,
    SyncState,
)

log = logging.getLogger("sync")

# جداول تُسجَّل محلياً أثناء الأوفلاين ثم تُدفع عند عودة الاتصال
TRACKED_TABLES = {
    "sales",
    "products",
    "product_categories",
    "customers",
    "sale_payments",
    "hotel_rooms",
    "hotel_room_types",
    "hotel_bookings",
    "hotel_booking_guests",
    "hotel_booking_room_assignments",
    "hotel_booking_services",
    "hotel_booking_payments",
    "hotel_booking_debts",
    "hotel_invoices",
    "hotel_invoice_items",
}

# ربط المفاتيح الأجنبية الشائعة عند تطبيق أحداث بعيدة
_FK_TABLE_HINTS: dict[tuple[str, str], str] = {
    ("products", "category_id"): "product_categories",
    ("sales", "customer_id"): "customers",
    ("sales", "booking_id"): "hotel_bookings",
    ("sale_payments", "sale_id"): "sales",
    ("hotel_rooms", "room_type_id"): "hotel_room_types",
    ("hotel_bookings", "room_type_id"): "hotel_room_types",
    ("hotel_booking_guests", "booking_id"): "hotel_bookings",
    ("hotel_booking_room_assignments", "booking_id"): "hotel_bookings",
    ("hotel_booking_room_assignments", "room_id"): "hotel_rooms",
    ("hotel_booking_services", "booking_id"): "hotel_bookings",
    ("hotel_booking_payments", "booking_id"): "hotel_bookings",
    ("hotel_booking_debts", "booking_id"): "hotel_bookings",
    ("hotel_invoices", "booking_id"): "hotel_bookings",
    ("hotel_invoice_items", "invoice_id"): "hotel_invoices",
}

MAX_SYNC_RETRIES = 50


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _generate_event_id() -> str:
    return f"{now_utc().strftime('%Y%m%d%H%M%S%f')}-{uuid.uuid4().hex[:8]}"


def get_site_id() -> str:
    site = get_settings().sync_site_id
    return (site or "local").strip() or "local"


def _serialize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _serialize_record(instance: Any) -> str:
    data = {}
    for col in instance.__table__.columns:
        data[col.name] = _serialize_value(getattr(instance, col.name, None))
    # الفاتورة تحتاج البنود لتطبيقها على السيرفر المركزي
    if getattr(instance, "__tablename__", None) == "sales":
        lines = []
        for line in getattr(instance, "lines", None) or []:
            lines.append(
                {
                    "id": getattr(line, "id", None),
                    "product_id": getattr(line, "product_id", None),
                    "quantity": _serialize_value(getattr(line, "quantity", None)),
                    "unit_price": _serialize_value(getattr(line, "unit_price", None)),
                    "line_total": _serialize_value(getattr(line, "line_total", None)),
                    "line_note": getattr(line, "line_note", None),
                }
            )
        data["lines"] = lines
    return json.dumps(data, ensure_ascii=False)


def _record_pk(instance: Any) -> str:
    values = [getattr(instance, c.name) for c in instance.__table__.primary_key.columns]
    return ":".join(str(v) for v in values)


def _source_timestamp(instance: Any) -> datetime:
    for attr in ("updated_at", "modified_at", "created_at"):
        val = getattr(instance, attr, None)
        if isinstance(val, datetime):
            return val
    return now_utc()


def _is_tracked(instance: Any) -> bool:
    return hasattr(instance, "__tablename__") and instance.__tablename__ in TRACKED_TABLES


def queue_event(db: ORMSession, instance: Any, action: str) -> SyncEvent | None:
    if not _is_tracked(instance):
        return None
    event = SyncEvent(
        event_id=_generate_event_id(),
        site_id=get_site_id(),
        action=action,
        table_name=instance.__tablename__,
        record_id=_record_pk(instance),
        payload_json=_serialize_record(instance),
        source_updated_at=_source_timestamp(instance),
    )
    db.add(event)
    return event


def _before_flush(session: ORMSession, flush_context: Any, instances: Any) -> None:
    if session.info.get("_sync_processing"):
        return
    settings = get_settings()
    if not settings.sync_enabled:
        return
    queued = session.info.setdefault("_sync_queued", set())
    session.info["_sync_processing"] = True
    try:
        for action, collection in (
            (SyncEventAction.INSERT.value, session.new),
            (SyncEventAction.UPDATE.value, session.dirty),
            (SyncEventAction.DELETE.value, session.deleted),
        ):
            for instance in list(collection):
                if not _is_tracked(instance):
                    continue
                if isinstance(
                    instance,
                    (SyncEvent, SyncIncomingEvent, SyncState, SyncRecordMap),
                ):
                    continue
                if id(instance) in queued:
                    continue
                queue_event(session, instance, action)
                queued.add(id(instance))
    finally:
        session.info["_sync_processing"] = False


event.listen(ORMSession, "before_flush", _before_flush)


# =============================================================================
# Helpers for applying remote events
# =============================================================================


def _model_for_table(table_name: str) -> Any | None:
    for cls in Base.registry._class_registry.values():
        if hasattr(cls, "__tablename__") and cls.__tablename__ == table_name:
            return cls
    return None


def _pk_column(model: Any):
    cols = list(model.__table__.primary_key.columns)
    return cols[0] if cols else None


def _cast_pk(value: Any, column: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, Integer) or str(value).isdigit():
        try:
            return int(value)
        except Exception:
            pass
    return value


def _get_map(db: ORMSession, site_id: str, table: str, remote_id: str) -> SyncRecordMap | None:
    return db.scalar(
        select(SyncRecordMap).where(
            SyncRecordMap.site_id == site_id,
            SyncRecordMap.table_name == table,
            SyncRecordMap.remote_record_id == str(remote_id),
        )
    )


def _set_map(
    db: ORMSession,
    site_id: str,
    table: str,
    remote_id: str,
    local_id: Any,
) -> None:
    m = _get_map(db, site_id, table, remote_id)
    if m is None:
        m = SyncRecordMap(
            site_id=site_id,
            table_name=table,
            remote_record_id=str(remote_id),
            local_record_id=str(local_id),
        )
        db.add(m)
    else:
        m.local_record_id = str(local_id)


def get_or_create_sync_state(db: ORMSession, site_id: str) -> SyncState:
    state = db.scalar(select(SyncState).where(SyncState.site_id == site_id))
    if state is None:
        state = SyncState(site_id=site_id)
        db.add(state)
    return state


def _map_fk(
    db: ORMSession,
    site_id: str,
    table: str,
    remote_id: Any,
) -> Any | None:
    if remote_id is None:
        return None
    m = _get_map(db, site_id, table, str(remote_id))
    if m is None:
        return None
    return m.local_record_id


def _apply_columns(
    db: ORMSession,
    obj: Any,
    payload: dict[str, Any],
    site_id: str,
    table: str,
) -> None:
    for col in obj.__table__.columns:
        if col.primary_key:
            continue
        if col.name not in payload:
            continue
        value = payload[col.name]
        hint = _FK_TABLE_HINTS.get((table, col.name))
        if hint is not None and value is not None:
            mapped = _map_fk(db, site_id, hint, value)
            if mapped is not None:
                value = mapped
        elif col.name in (
            "parent_id",
            "kitchen_department_id",
            "kitchen_section_id",
            "sales_warehouse_id",
        ):
            mapped = _map_fk(db, site_id, "kitchen_departments", value)
            if mapped is None:
                mapped = _map_fk(db, site_id, "kitchen_sections", value)
            if mapped is None:
                mapped = _map_fk(db, site_id, "warehouses", value)
            if mapped is not None:
                value = mapped
        if value is None and not col.nullable:
            continue
        if isinstance(value, str) and col.name.endswith("_at"):
            try:
                value = datetime.fromisoformat(value)
            except Exception:
                pass
        setattr(obj, col.name, value)


def apply_generic_event(db: ORMSession, event: SyncIncomingEvent) -> tuple[bool, str]:
    model = _model_for_table(event.table_name)
    if model is None:
        return False, f"unknown table {event.table_name}"

    try:
        payload = json.loads(event.payload_json)
    except Exception as exc:
        return False, f"invalid payload json: {exc}"

    pk_col = _pk_column(model)
    remote_id = payload.get(pk_col.name) if pk_col is not None else event.record_id
    if remote_id is None:
        remote_id = event.record_id

    m = _get_map(db, event.site_id, event.table_name, str(remote_id))

    if event.action == SyncEventAction.DELETE.value:
        target_id = m.local_record_id if m else str(remote_id)
        obj = db.get(model, _cast_pk(target_id, pk_col)) if pk_col is not None else None
        if obj is not None:
            db.delete(obj)
        if m:
            db.delete(m)
        return True, "deleted"

    if m:
        obj = db.get(model, _cast_pk(m.local_record_id, pk_col)) if pk_col is not None else None
    else:
        obj = None

    if obj is None:
        # محاولة إنشاء باستخدام نفس المعرّف إن كان متاحاً
        obj = db.get(model, _cast_pk(remote_id, pk_col)) if pk_col is not None else None
        if obj is None:
            obj = model()
            if pk_col is not None and payload.get(pk_col.name) is not None:
                casted = _cast_pk(payload[pk_col.name], pk_col)
                if db.get(model, casted) is None:
                    setattr(obj, pk_col.name, casted)
            db.add(obj)

    _apply_columns(db, obj, payload, event.site_id, event.table_name)
    db.flush()

    pk_val = getattr(obj, pk_col.name) if pk_col is not None else remote_id
    _set_map(db, event.site_id, event.table_name, str(remote_id), pk_val)
    return True, f"applied {event.action}"


def apply_sale_event(db: ORMSession, event: SyncIncomingEvent) -> tuple[bool, str]:
    try:
        payload = json.loads(event.payload_json)
    except Exception as exc:
        return False, f"invalid payload json: {exc}"

    lines = payload.get("lines") or payload.get("sale_lines")
    if not lines:
        return False, "sale has no lines"



    mapped_lines = []
    for line in lines:
        product_id = line.get("product_id") if isinstance(line, dict) else line[0]
        qty = line.get("quantity") if isinstance(line, dict) else line[1]
        mapped = _map_fk(db, event.site_id, "products", product_id)
        if mapped is None:
            # نفس المعرّف إن وُجد المنتج محلياً (نسخ متطابقة / أول مزامنة)
            mapped = product_id
        mapped_lines.append((int(mapped), Decimal(str(qty))))

    external_order_id = f"{event.site_id}:{event.record_id}"
    try:
        sale = create_online_sale_completed(
            db,
            external_order_id=external_order_id,
            lines=mapped_lines,
            user_id=None,
        )
        _set_map(db, event.site_id, "sales", str(event.record_id), sale.id)
    except SalesError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"sale apply failed: {exc}"
    return True, f"sale {sale.id} created"


def apply_remote_event(db: ORMSession, event: SyncIncomingEvent) -> tuple[bool, str]:
    if event.table_name == "sales" and event.action == SyncEventAction.INSERT.value:
        return apply_sale_event(db, event)
    if event.table_name in TRACKED_TABLES:
        return apply_generic_event(db, event)
    return False, f"unsupported table {event.table_name}"


# =============================================================================
# Local queue management
# =============================================================================


def _pending_filter():
    from sqlalchemy import and_, or_

    return or_(
        SyncEvent.status == SyncEventStatus.PENDING.value,
        and_(
            SyncEvent.status == SyncEventStatus.FAILED.value,
            SyncEvent.retries < MAX_SYNC_RETRIES,
        ),
    )


def count_pending_events(db: ORMSession) -> int:
    """عدّ سريع للأحداث المعلّقة بدون تحميل الصفوف."""
    from sqlalchemy import func

    return int(
        db.scalar(select(func.count()).select_from(SyncEvent).where(_pending_filter()))
        or 0
    )


def get_pending_events(db: ORMSession, limit: int = 100) -> list[SyncEvent]:
    """أحداث معلّقة + فاشلة قابلة لإعادة المحاولة بعد انقطاع الإنترنت."""
    rows = list(
        db.scalars(
            select(SyncEvent)
            .where(_pending_filter())
            .order_by(SyncEvent.id)
            .limit(limit)
        ).all()
    )
    for ev in rows:
        if ev.status == SyncEventStatus.FAILED.value:
            ev.status = SyncEventStatus.PENDING.value
            ev.last_error = None
    return rows


_SYNC_STATUS_CACHE: dict[str, Any] = {"at": 0.0, "payload": None}
_SYNC_STATUS_TTL = 5.0


def get_sync_status(db: ORMSession) -> dict[str, Any]:
    import time

    now = time.monotonic()
    cached = _SYNC_STATUS_CACHE.get("payload")
    if cached is not None and (now - float(_SYNC_STATUS_CACHE["at"])) < _SYNC_STATUS_TTL:
        return cached

    settings = get_settings()
    site_id = get_site_id()
    state = get_or_create_sync_state(db, site_id)
    pending_count = count_pending_events(db)
    configured = bool(
        (settings.online_sync_url or "").strip() and (settings.online_sync_api_key or "").strip()
    )
    payload = {
        "enabled": settings.sync_enabled,
        "configured": configured,
        "site_id": site_id,
        "online": bool(state.is_online) if configured else True,
        "local_ok": True,
        "pending_count": pending_count,
        "last_push_at": state.last_push_at.isoformat() if state.last_push_at else None,
        "last_pull_at": state.last_pull_at.isoformat() if state.last_pull_at else None,
        "last_error": state.last_error,
        "mode": "offline_first",
        "message_ar": _status_message_ar(
            enabled=settings.sync_enabled,
            configured=configured,
            online=bool(state.is_online) if configured else True,
            pending=pending_count,
        ),
    }
    _SYNC_STATUS_CACHE["at"] = now
    _SYNC_STATUS_CACHE["payload"] = payload
    return payload


def _status_message_ar(*, enabled: bool, configured: bool, online: bool, pending: int) -> str:
    if not enabled:
        return "يعمل محلياً بالكامل بدون إنترنت (المزامنة السحابية مطفأة)"
    if not configured:
        return "يعمل أوفلاين — فعّل ONLINE_SYNC_URL لمزامنة السحابة عند عودة الإنترنت"
    if not online:
        return f"أوفلاين — العمل مستمر محلياً · بانتظار المزامنة ({pending})"
    if pending:
        return f"متصل — جارٍ دفع {pending} حدث معلّق"
    return "متصل — المزامنة محدّثة"


def mark_events_sent(
    db: ORMSession,
    event_ids: list[str],
    *,
    acked: bool = False,
    error: str | None = None,
) -> None:
    for event_id in event_ids:
        ev = db.scalar(select(SyncEvent).where(SyncEvent.event_id == event_id))
        if ev is None:
            continue
        ev.sent_at = now_utc()
        if error:
            ev.status = SyncEventStatus.FAILED.value
            ev.retries += 1
            ev.last_error = error
        else:
            ev.status = SyncEventStatus.ACKED.value if acked else SyncEventStatus.SENT.value
            ev.acked_at = now_utc() if acked else ev.acked_at


def update_sync_state(
    db: ORMSession,
    *,
    is_online: bool | None = None,
    last_push_at: datetime | None = None,
    last_pull_at: datetime | None = None,
    last_error: str | None = None,
) -> None:
    state = get_or_create_sync_state(db, get_site_id())
    if is_online is not None:
        state.is_online = is_online
    if last_push_at is not None:
        state.last_push_at = last_push_at
    if last_pull_at is not None:
        state.last_pull_at = last_pull_at
    if last_error is not None:
        state.last_error = last_error
