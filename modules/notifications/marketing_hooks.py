"""إشعارات التسويق والولاء والإحالات والمطبخ — المرحلة الأخيرة."""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from modules.notifications.events import (
    KITCHEN_ITEM_CANCELLED,
    KITCHEN_TICKET_CREATED,
    LOYALTY_ACCOUNT_CREATED,
    ORDER_CANCELLED,
    POS_CASH_OVERAGE,
    POS_SHIFT_OPENED,
    REFERRAL_FIRST_ORDER,
    REFERRAL_LINK_CREATED,
    REFERRAL_PRODUCT_SHARED,
    REFERRAL_REFERRER_REWARDED,
)
from modules.notifications.service import emit_event_background_safe, emit_event_safe

if TYPE_CHECKING:
    from modules.customers.models import Customer
    from modules.sales.models import Sale


def _sale_payload(sale: Sale, **extra: Any) -> dict[str, Any]:
    total = Decimal(str(sale.total or 0)).quantize(Decimal("0.001"))
    return {
        "order_id": sale.id,
        "sale_id": sale.id,
        "total": str(total),
        **extra,
    }


def emit_referral_link_created(db: Session, customer: Customer, *, code: str) -> None:
    emit_event_safe(
        db,
        event_key=REFERRAL_LINK_CREATED,
        source_type="customer",
        source_id=customer.id,
        payload={
            "customer_id": customer.id,
            "customer_name": customer.name or "",
            "phone": customer.phone or "",
            "referral_code": code,
        },
    )


def emit_referral_product_shared(
    db: Session,
    *,
    customer: Customer,
    product_id: int,
    product_name: str,
    referral_code: str,
    share_url: str,
    share_message: str,
) -> None:
    share_nonce = uuid.uuid4().hex
    emit_event_safe(
        db,
        event_key=REFERRAL_PRODUCT_SHARED,
        source_type="shop_share",
        source_id=int(product_id),
        payload={
            "customer_id": customer.id,
            "customer_name": customer.name or "",
            "phone": customer.phone or "",
            "product_id": int(product_id),
            "product_name": product_name,
            "referral_code": referral_code,
            "share_url": share_url,
            "share_nonce": share_nonce,
            "message": share_message,
        },
    )


def notify_referral_product_shared_background(
    *,
    customer_id: int,
    customer_name: str,
    phone: str,
    product_id: int,
    product_name: str,
    referral_code: str,
    share_url: str,
    share_message: str,
) -> None:
    share_nonce = uuid.uuid4().hex
    emit_event_background_safe(
        REFERRAL_PRODUCT_SHARED,
        "shop_share",
        int(product_id),
        {
            "customer_id": customer_id,
            "customer_name": customer_name,
            "phone": phone,
            "product_id": int(product_id),
            "product_name": product_name,
            "referral_code": referral_code,
            "share_url": share_url,
            "share_nonce": share_nonce,
            "message": share_message,
        },
    )


def emit_referral_first_order_completed(
    db: Session,
    *,
    referrer: Customer,
    buyer: Customer,
    sale_id: int,
    referral_code: str,
    referrer_points: Decimal,
    buyer_points: Decimal,
) -> None:
    from modules.customers.service import format_money_plain, points_to_dinars

    buyer_pts_val = format_money_plain(points_to_dinars(db, buyer_points))
    own_code = (buyer.referral_code or "").strip()
    referral_share_block = ""
    if own_code:
        referral_share_block = (
            f"🎁 كود إحالتك للمشاركة مع أصدقائك: {own_code}\n\n"
        )
    emit_event_safe(
        db,
        event_key=REFERRAL_FIRST_ORDER,
        source_type="referral",
        source_id=sale_id,
        payload={
            "customer_id": buyer.id,
            "sale_id": sale_id,
            "order_id": sale_id,
            "referral_code": referral_code,
            "buyer_referral_code": own_code,
            "referral_code_line": referral_share_block,
            "referral_share_block": referral_share_block,
            "referrer_name": referrer.name or "",
            "referrer_phone": referrer.phone or "",
            "customer_name": buyer.name or "",
            "phone": buyer.phone or "",
            "referrer_points": f"{referrer_points:.0f}",
            "buyer_points": f"{buyer_points:.0f}",
            "buyer_points_value": buyer_pts_val,
            "points": f"{buyer_points:.0f}",
            "points_value": buyer_pts_val,
            "message": (
                f"إحالة ناجحة! فاتورة #{sale_id}\n"
                f"المُحيل: +{referrer_points:.0f} نقطة — المشتري: +{buyer_points:.0f} نقطة"
            ),
        },
    )


def emit_referral_referrer_rewarded(
    db: Session,
    *,
    referrer: Customer,
    buyer: Customer,
    sale_id: int,
    referral_code: str,
    referrer_points: Decimal,
) -> None:
    if referrer_points <= 0:
        return
    from modules.customers.service import format_money_plain, points_to_dinars

    pts_val = format_money_plain(points_to_dinars(db, referrer_points))
    emit_event_safe(
        db,
        event_key=REFERRAL_REFERRER_REWARDED,
        source_type="referral",
        source_id=sale_id,
        payload={
            "referrer_customer_id": referrer.id,
            "referrer_name": referrer.name or "",
            "referrer_phone": referrer.phone or "",
            "customer_id": referrer.id,
            "customer_name": referrer.name or "",
            "phone": referrer.phone or "",
            "buyer_name": buyer.name or "",
            "referral_code": referral_code,
            "referrer_points": f"{referrer_points:.0f}",
            "points": f"{referrer_points:.0f}",
            "points_value": pts_val,
            "sale_id": sale_id,
            "order_id": sale_id,
        },
    )


def emit_loyalty_account_created(db: Session, customer: Customer) -> None:
    emit_event_safe(
        db,
        event_key=LOYALTY_ACCOUNT_CREATED,
        source_type="customer",
        source_id=customer.id,
        payload={
            "customer_id": customer.id,
            "customer_name": customer.name or "",
            "phone": customer.phone or "",
            "balance": f"{Decimal(customer.points_balance or 0):.0f}",
        },
    )


def emit_order_cancelled(db: Session, sale: Sale, *, reason: str = "") -> None:
    from modules.customers.models import Customer

    cust = db.get(Customer, int(sale.customer_id)) if sale.customer_id else None
    emit_event_safe(
        db,
        event_key=ORDER_CANCELLED,
        source_type="order",
        source_id=sale.id,
        payload=_sale_payload(
            sale,
            customer_id=cust.id if cust else None,
            customer_name=(cust.name if cust else "") or "عميلنا",
            phone=(cust.phone if cust else "") or "",
            reason=(reason or "").strip(),
        ),
    )


def emit_kitchen_ticket_created(
    db: Session,
    *,
    sale: Sale,
    ticket_id: int,
    section_name: str = "",
) -> None:
    emit_event_safe(
        db,
        event_key=KITCHEN_TICKET_CREATED,
        source_type="kitchen",
        source_id=ticket_id,
        payload={
            **_sale_payload(sale),
            "ticket_id": ticket_id,
            "section_name": section_name,
            "message": f"تذكرة مطبخ #{ticket_id} — طلب #{sale.id}" + (
                f" ({section_name})" if section_name else ""
            ),
        },
    )


def emit_kitchen_item_cancelled(
    db: Session,
    *,
    sale: Sale,
    reason: str,
    item_count: int = 1,
) -> None:
    emit_event_safe(
        db,
        event_key=KITCHEN_ITEM_CANCELLED,
        source_type="kitchen",
        source_id=sale.id,
        payload={
            **_sale_payload(sale),
            "reason": (reason or "").strip(),
            "item_count": str(item_count),
            "message": f"إلغاء {item_count} صنف/أصناف — طلب #{sale.id}\n{(reason or '').strip()}",
        },
    )


def emit_pos_shift_opened(
    db: Session,
    *,
    shift_id: int,
    cashier_name: str = "",
) -> None:
    emit_event_safe(
        db,
        event_key=POS_SHIFT_OPENED,
        source_type="pos_shift",
        source_id=shift_id,
        payload={
            "shift_id": shift_id,
            "cashier_name": cashier_name,
        },
    )


def emit_pos_cash_overage(
    db: Session,
    *,
    shift_id: int,
    cashier_name: str = "",
    overage: Decimal,
) -> None:
    emit_event_safe(
        db,
        event_key=POS_CASH_OVERAGE,
        source_type="pos_shift",
        source_id=shift_id,
        payload={
            "shift_id": shift_id,
            "cashier_name": cashier_name,
            "shortage": str(overage.quantize(Decimal("0.001"))),
            "message": f"فائض جلسة #{shift_id}: {overage} — {cashier_name}",
        },
    )
