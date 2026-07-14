"""قائمة «حالة الطلبات» — مسودات الجلسة مع الإجراء التالي للكاشير."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from modules.sales.models import Sale, SaleStatus
from modules.sales.order_policy import can_pay_sale, load_order_policy
from modules.sales.order_pipeline import build_pipeline_view, sale_is_delivery
from modules.sales.pos_orders_hub import list_pos_shift_orders_hub


@dataclass
class OrderStatusRow:
    id: int
    label: str
    context_kind: str
    context_label: str
    pipeline_status: str
    pipeline_key: str
    stage: int
    can_send: bool
    can_present: bool
    can_checkout: bool
    can_driver: bool
    driver_name: str | None
    driver_button_label: str
    is_active: bool


def _order_context_meta(sale: Sale) -> tuple[str, str]:
    ctx = sale.context_type.value if sale.context_type else "TABLE"
    if ctx == "ROOM":
        return "ROOM", "شقة"
    if ctx == "EXTERNAL":
        ext = sale.external_order_type.value if sale.external_order_type else "PICKUP"
        if ext == "DELIVERY":
            return "DELIVERY", "توصيل"
        return "PICKUP", "استلام"
    return "TABLE", "طاولة"


def _pipeline_stage(key: str) -> int:
    if key in ("entering",):
        return 0
    if key in ("sent", "kitchen_working"):
        return 1
    if key in ("ready", "ready_serve"):
        return 2
    if key in (
        "served_await_pay",
        "served_need_driver",
        "with_driver",
    ):
        return 3
    return 1


def sale_can_send_to_kitchen(sale: Sale, *, room_session_ok: bool) -> bool:
    if sale.status != SaleStatus.DRAFT or sale.sent_to_kitchen_at:
        return False
    if not sale.lines:
        return False
    ctx = sale.context_type.value if sale.context_type else "TABLE"
    if ctx == "TABLE":
        return bool(sale.table_id)
    if ctx == "ROOM":
        return room_session_ok
    if ctx == "EXTERNAL":
        if not sale.customer_id:
            return False
        ext = sale.external_order_type.value if sale.external_order_type else "PICKUP"
        if ext == "DELIVERY":
            return bool(sale.delivery_zone_id)
        return True
    return False


def list_order_status_rows(
    db: Session,
    *,
    pos_shift_id: int,
    user_id: int,
    current_sale_id: int | None = None,
    room_session_ok: bool = False,
    limit: int = 25,
) -> list[OrderStatusRow]:
    from modules.authz.models import User

    user = db.get(User, user_id)
    if user is None:
        return []
    hub = list_pos_shift_orders_hub(
        db,
        pos_shift_id=pos_shift_id,
        user=user,
        user_id=user_id,
        current_sale_id=current_sale_id,
        limit=limit,
    )
    policy = load_order_policy(db)
    rows: list[OrderStatusRow] = []
    for h in hub:
        if not h.can_open:
            continue
        sale = db.get(Sale, h.id)
        if sale is None:
            continue
        is_del = sale_is_delivery(sale)
        can_send = sale_can_send_to_kitchen(
            sale, room_session_ok=room_session_ok
        ) and policy.kitchen_workflow_enabled
        can_present = (
            h.can_open
            and bool(sale.sent_to_kitchen_at)
            and sale.served_to_customer_at is None
            and h.pipeline_key in ("ready", "ready_serve")
        )
        can_checkout = h.can_open and can_pay_sale(db, sale, policy)
        from modules.delivery.drivers_service import (
            driver_button_label,
            driver_name_from_label,
        )

        driver_name = driver_name_from_label(h.driver_label)
        show_driver_btn = (
            is_del
            and bool(sale.sent_to_kitchen_at)
            and sale.status == SaleStatus.DRAFT
        )
        ctx_kind, ctx_label = _order_context_meta(sale)
        if ctx_kind == "TABLE":
            continue
        rows.append(
            OrderStatusRow(
                id=h.id,
                label=h.label,
                context_kind=ctx_kind,
                context_label=ctx_label,
                pipeline_status=h.pipeline_status,
                pipeline_key=h.pipeline_key,
                stage=_pipeline_stage(h.pipeline_key),
                can_send=can_send,
                can_present=can_present,
                can_checkout=can_checkout,
                can_driver=show_driver_btn,
                driver_name=driver_name,
                driver_button_label=driver_button_label(driver_name),
                is_active=h.is_active,
            )
        )
    return rows
