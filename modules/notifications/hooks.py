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
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from modules.customers.models import Customer
    from modules.sales.models import Sale as SaleModel
    from modules.sales.models import SaleLine

    # تحميل الأصناف لملخص واتساب
    try:
        loaded = db.scalar(
            select(SaleModel)
            .where(SaleModel.id == int(sale.id))
            .options(selectinload(SaleModel.lines).selectinload(SaleLine.product))
        )
        if loaded is not None:
            sale = loaded
    except Exception:  # noqa: BLE001
        pass

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
    # ملخص أصناف لواتساب مركز الإشعارات / الكاشير
    try:
        item_bits: list[str] = []
        for line in list(getattr(sale, "lines", None) or [])[:12]:
            pname = ""
            if getattr(line, "product", None) is not None:
                pname = (line.product.name_ar or "").strip()
            if not pname:
                continue
            qty = line.quantity
            item_bits.append(f"{pname}×{qty}")
        if item_bits:
            payload["items_summary"] = " · ".join(item_bits)
    except Exception:  # noqa: BLE001
        pass
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
    employee_name: str = "",
    shortage: Decimal = Decimal("0"),
    cash_shortage: Decimal | None = None,
    bank_shortage: Decimal | None = None,
) -> None:
    """إغلاق جلسة — ويُرسل عجز إن وُجد نقداً و/أو مصرفاً."""
    emp = (employee_name or cashier_name or "").strip()
    if cash_shortage is not None or bank_shortage is not None:
        cash_amt = max(Decimal(str(cash_shortage or 0)), Decimal("0")).quantize(
            Decimal("0.001")
        )
        bank_amt = max(Decimal(str(bank_shortage or 0)), Decimal("0")).quantize(
            Decimal("0.001")
        )
    else:
        # توافق مع الاستدعاءات القديمة: shortage سالب = عجز كاش
        cash_amt = (
            abs(shortage).quantize(Decimal("0.001"))
            if shortage < 0
            else Decimal("0.000")
        )
        bank_amt = Decimal("0.000")
    total_short = (cash_amt + bank_amt).quantize(Decimal("0.001"))
    detail_bits: list[str] = []
    if cash_amt > 0:
        detail_bits.append(f"نقداً: {cash_amt} د.ل")
    if bank_amt > 0:
        detail_bits.append(f"مصرف: {bank_amt} د.ل")
    payload = {
        "shift_id": shift_id,
        "cashier_name": emp or cashier_name,
        "employee_name": emp or cashier_name,
        "shortage": str(
            total_short
            if total_short > 0
            else abs(shortage).quantize(Decimal("0.001"))
        ),
        "cash_shortage": str(cash_amt),
        "bank_shortage": str(bank_amt),
        "shortage_detail": " · ".join(detail_bits) if detail_bits else "",
    }
    emit_event_safe(
        db,
        event_key=POS_SHIFT_CLOSED,
        source_type="pos_shift",
        source_id=shift_id,
        payload=payload,
    )
    if total_short > 0:
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
                cashier_name=emp or cashier_name,
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
