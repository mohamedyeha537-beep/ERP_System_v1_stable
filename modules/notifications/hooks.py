"""نقاط ربط الأحداث من طبقة المجال."""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from modules.notifications.events import (
    CUSTOMER_CREATED,
    LOYALTY_POINTS_EARNED,
    LOYALTY_POINTS_REDEEMED,
    ORDER_CONFIRMED,
    ORDER_CREATED,
    ORDER_DELIVERED,
    ORDER_DRIVER_ASSIGNED,
    ORDER_OUT_FOR_DELIVERY,
    ORDER_SENT_TO_KITCHEN,
    POS_CASH_SHORTAGE,
    POS_INVOICE_CANCEL_REQUESTED,
    POS_ITEM_VOID_REQUESTED,
    POS_SHIFT_CLOSED,
)
from modules.notifications.service import emit_event_safe

if TYPE_CHECKING:
    from modules.customers.models import Customer
    from modules.sales.models import Sale


def _sale_payload(db: Session, sale: Sale, **extra: Any) -> dict[str, Any]:
    from modules.customers.models import Customer

    cust: Customer | None = None
    if sale.customer_id:
        cust = db.get(Customer, int(sale.customer_id))
    total = Decimal(str(sale.total or 0)).quantize(Decimal("0.001"))
    order_type = ""
    if hasattr(sale, "external_order_type") and sale.external_order_type:
        order_type = sale.external_order_type.value
    payload: dict[str, Any] = {
        "customer_id": cust.id if cust else None,
        "customer_name": (cust.name if cust else "") or "عميلنا",
        "phone": (cust.phone if cust else "") or "",
        "order_id": sale.id,
        "sale_id": sale.id,
        "total": str(total),
        "order_type": order_type,
        "payment_status": "paid" if sale.status.value == "COMPLETED" else "pending",
    }
    payload.update(extra)
    return payload


def _emit_sale_event(
    db: Session,
    *,
    event_key: str,
    sale: Sale,
    extra: dict[str, Any] | None = None,
) -> None:
    emit_event_safe(
        db,
        event_key=event_key,
        source_type="order",
        source_id=sale.id,
        payload=_sale_payload(db, sale, **(extra or {})),
    )


def emit_order_created(db: Session, sale: Sale) -> None:
    _emit_sale_event(db, event_key=ORDER_CREATED, sale=sale)


def emit_order_confirmed(db: Session, sale: Sale) -> None:
    _emit_sale_event(db, event_key=ORDER_CONFIRMED, sale=sale)


def emit_order_sent_to_kitchen(db: Session, sale: Sale) -> None:
    _emit_sale_event(db, event_key=ORDER_SENT_TO_KITCHEN, sale=sale)


def emit_order_driver_assigned(
    db: Session,
    sale: Sale,
    *,
    driver_name: str,
    driver_phone: str,
    driver_id: int | None = None,
) -> None:
    _emit_sale_event(
        db,
        event_key=ORDER_DRIVER_ASSIGNED,
        sale=sale,
        extra={
            "driver_name": driver_name,
            "driver_phone": driver_phone,
            "driver_id": driver_id,
            "phone": driver_phone,
            "recipient_override": "driver",
        },
    )


def emit_order_out_for_delivery(
    db: Session,
    sale: Sale,
    *,
    driver_name: str = "",
) -> None:
    _emit_sale_event(
        db,
        event_key=ORDER_OUT_FOR_DELIVERY,
        sale=sale,
        extra={"driver_name": driver_name},
    )


def emit_order_delivered(db: Session, sale: Sale, *, driver_name: str = "") -> None:
    _emit_sale_event(
        db,
        event_key=ORDER_DELIVERED,
        sale=sale,
        extra={"driver_name": driver_name} if driver_name else {},
    )


def emit_loyalty_points_earned(
    db: Session,
    *,
    customer: Customer,
    sale_id: int | None,
    points: Decimal,
) -> None:
    if points <= 0:
        return
    from modules.customers.service import format_money_plain, points_to_dinars

    points_value = points_to_dinars(db, points)
    balance = Decimal(customer.points_balance or 0)
    balance_value = points_to_dinars(db, balance)
    emit_event_safe(
        db,
        event_key=LOYALTY_POINTS_EARNED,
        source_type="loyalty",
        source_id=sale_id,
        payload={
            "customer_id": customer.id,
            "customer_name": customer.name or "",
            "phone": customer.phone or "",
            "points": f"{points:.0f}",
            "points_value": format_money_plain(points_value),
            "balance": f"{balance:.0f}",
            "balance_value": format_money_plain(balance_value),
            "sale_id": sale_id or "",
            "order_id": sale_id or "",
        },
    )


def emit_loyalty_points_redeemed(
    db: Session,
    *,
    customer: Customer,
    sale_id: int,
    points: Decimal,
    earned: Decimal = Decimal("0"),
) -> None:
    if points <= 0:
        return
    from modules.customers.service import format_money_plain, points_to_dinars

    earned_pts = Decimal(str(earned or 0)).quantize(Decimal("0.001"))
    earn_line = ""
    if earned_pts > 0:
        earned_value = points_to_dinars(db, earned_pts)
        earn_line = (
            f"كما حصلت على {earned_pts:.0f} نقطة جديدة من هذه الفاتورة، "
            f"بقيمة {format_money_plain(earned_value)} د.ل.\n\n"
        )
    balance = Decimal(customer.points_balance or 0)
    emit_event_safe(
        db,
        event_key=LOYALTY_POINTS_REDEEMED,
        source_type="loyalty",
        source_id=sale_id,
        payload={
            "customer_id": customer.id,
            "customer_name": customer.name or "",
            "phone": customer.phone or "",
            "points": f"{points:.0f}",
            "points_value": format_money_plain(points_to_dinars(db, points)),
            "earned_points": f"{earned_pts:.0f}",
            "earned_points_value": format_money_plain(points_to_dinars(db, earned_pts)),
            "earn_line": earn_line,
            "balance": f"{balance:.0f}",
            "balance_value": format_money_plain(points_to_dinars(db, balance)),
            "sale_id": sale_id,
            "order_id": sale_id,
        },
    )


def emit_customer_created(db: Session, customer: Customer) -> None:
    emit_event_safe(
        db,
        event_key=CUSTOMER_CREATED,
        source_type="customer",
        source_id=customer.id,
        payload={
            "customer_id": customer.id,
            "customer_name": customer.name or "",
            "phone": customer.phone or "",
        },
    )


def emit_pos_shift_closed(
    db: Session,
    *,
    shift_id: int,
    cashier_name: str = "",
    shortage: Decimal = Decimal("0"),
) -> None:
    payload = {
        "shift_id": shift_id,
        "cashier_name": cashier_name,
        "shortage": str(abs(shortage).quantize(Decimal("0.001"))),
    }
    emit_event_safe(
        db,
        event_key=POS_SHIFT_CLOSED,
        source_type="pos_shift",
        source_id=shift_id,
        payload=payload,
    )
    if shortage < 0:
        emit_event_safe(
            db,
            event_key=POS_CASH_SHORTAGE,
            source_type="pos_shift",
            source_id=shift_id,
            payload={**payload, "shortage_amount": payload["shortage"]},
        )
    elif shortage > 0:
        try:
            from modules.notifications.marketing_hooks import emit_pos_cash_overage

            emit_pos_cash_overage(
                db,
                shift_id=shift_id,
                cashier_name=cashier_name,
                overage=shortage,
            )
        except Exception:  # noqa: BLE001
            pass


def _supervisor_void_payload(
    db: Session,
    *,
    sale: Sale,
    reason: str,
    cashier_user_id: int | None,
    kind: str,
    line_id: int | None = None,
) -> tuple[dict[str, Any], int]:
    from modules.notifications.action_handler import create_pending_action

    act = create_pending_action(
        db,
        action_key=kind,
        payload={
            "kind": kind,
            "sale_id": sale.id,
            "line_id": line_id,
            "reason": (reason or "").strip(),
            "cashier_user_id": cashier_user_id,
        },
    )
    from modules.authz.models import User

    cashier_name = ""
    if cashier_user_id:
        u = db.get(User, int(cashier_user_id))
        cashier_name = (u.username if u else "") or ""
    payload = _sale_payload(
        db,
        sale,
        action_id=act.id,
        reason=(reason or "").strip(),
        cashier_name=cashier_name,
        line_id=line_id or "",
    )
    return payload, act.id


def emit_pos_item_void_requested(
    db: Session,
    *,
    sale: Sale,
    line_id: int,
    reason: str,
    cashier_user_id: int | None,
) -> None:
    payload, _aid = _supervisor_void_payload(
        db,
        sale=sale,
        reason=reason,
        cashier_user_id=cashier_user_id,
        kind="void_line",
        line_id=line_id,
    )
    emit_event_safe(
        db,
        event_key=POS_ITEM_VOID_REQUESTED,
        source_type="pos",
        source_id=sale.id,
        payload=payload,
    )


def emit_pos_invoice_cancel_requested(
    db: Session,
    *,
    sale: Sale,
    reason: str,
    cashier_user_id: int | None,
) -> None:
    payload, _aid = _supervisor_void_payload(
        db,
        sale=sale,
        reason=reason,
        cashier_user_id=cashier_user_id,
        kind="void_sale",
    )
    emit_event_safe(
        db,
        event_key=POS_INVOICE_CANCEL_REQUESTED,
        source_type="pos",
        source_id=sale.id,
        payload=payload,
    )


def emit_inventory_low_stock_if_needed(
    db: Session,
    *,
    product_id: int,
    warehouse_id: int,
) -> None:
    from modules.notifications.inventory_hooks import emit_inventory_low_stock_if_needed as _emit

    _emit(db, product_id=product_id, warehouse_id=warehouse_id)
