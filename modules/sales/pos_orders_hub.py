"""قائمة طلبات الجلسة لنافذة «جميع الطلبات» في نقطة البيع."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload

from modules.authz.models import User
from modules.authz.permissions import SALES_EDIT_INVOICE
from modules.authz.service import user_has_permission
from modules.delivery.models import DeliveryHandoff
from modules.pos_shifts.models import PosShift, PosShiftStatus
from modules.payments.models import PaymentMethod
from modules.payments.service import list_sale_payments, sum_sale_payments
from modules.refunds.service import (
    get_open_room_charge,
    sale_outstanding_total,
    sale_remaining_total,
)
from modules.sales.models import Sale, SaleSource, SaleStatus
from modules.sales.order_pipeline import (
    build_pipeline_view,
    ensure_sale_shift,
    sale_is_delivery,
    sync_served_from_kds,
    _kitchen_agg,
)
from modules.sales.order_policy import can_pay_sale, load_order_policy
from modules.sales.service import sale_pos_order_label


@dataclass
class PosOrderHubRow:
    id: int
    label: str
    pipeline_status: str
    pipeline_key: str
    pipeline_hint: str
    pay_status: str
    pay_status_key: str
    total: Decimal
    created_at: datetime | None
    payment_method_name: str | None
    payment_method_id: int | None
    driver_label: str | None
    can_open: bool
    can_checkout: bool
    can_mark_served: bool
    can_assign_driver: bool
    can_change_payment: bool
    can_refund: bool
    can_print: bool
    can_edit_invoice: bool
    is_active: bool


@dataclass
class PosHubShiftOption:
    shift_id: int
    label: str
    is_today: bool
    is_current: bool
    is_open: bool


def _shift_option_label(sh: PosShift) -> str:
    from app.datetime_local import format_local_dt

    opened = sh.opened_at
    time_part = format_local_dt(opened, "%H:%M") if opened else "—"
    date_part = format_local_dt(opened, "%Y-%m-%d") if opened else "—"
    status = "مفتوحة" if sh.status == PosShiftStatus.OPEN else "مغلقة"
    emp = getattr(sh, "employee", None)
    who = emp.full_name_ar if emp and getattr(emp, "full_name_ar", None) else "—"
    return f"جلسة #{sh.id} — {date_part} {time_part} — {who} ({status})"


def list_pos_hub_shift_options(
    db: Session,
    *,
    current_shift_id: int | None,
    limit: int = 80,
) -> list[PosHubShiftOption]:
    """خيارات الجلسات لقائمة نافذة جميع الطلبات (اليوم أولاً ثم الأقدم)."""
    from app.datetime_local import now_local

    today = now_local().date()
    shifts = list(
        db.scalars(
            select(PosShift)
            .options(selectinload(PosShift.employee))
            .order_by(PosShift.id.desc())
            .limit(limit)
        ).all()
    )
    rows: list[PosHubShiftOption] = []
    for sh in shifts:
        opened = sh.opened_at
        if opened and opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        from app.datetime_local import to_local

        local_opened = to_local(opened) if opened else None
        is_today = bool(local_opened and local_opened.date() == today)
        rows.append(
            PosHubShiftOption(
                shift_id=sh.id,
                label=_shift_option_label(sh),
                is_today=is_today,
                is_current=current_shift_id is not None and sh.id == current_shift_id,
                is_open=sh.status == PosShiftStatus.OPEN,
            )
        )
    rows.sort(key=lambda r: (not r.is_current, not r.is_today, -r.shift_id))
    return rows


def _pay_status(db: Session, sale: Sale) -> tuple[str, str]:
    if sale.status != SaleStatus.COMPLETED:
        return "—", "na"
    net = sale_remaining_total(db, sale.id)
    if net <= 0:
        return "مسترد بالكامل", "refunded"
    outstanding = sale_outstanding_total(db, sale.id)
    paid = sum_sale_payments(db, sale.id)
    room = get_open_room_charge(db, sale.id)
    if room is not None and paid <= 0:
        return "على حساب شقة", "room"
    if outstanding <= 0 and paid > 0:
        return "مدفوع", "paid"
    if paid > 0 and outstanding > 0:
        rem = outstanding.quantize(Decimal("0.001"))
        return f"جزئي — متبقي {rem}", "partial"
    return "غير مدفوع", "unpaid"


def _payment_method_label(db: Session, sale: Sale) -> str | None:
    if sale.status != SaleStatus.COMPLETED:
        return None
    pays = list_sale_payments(db, sale.id)
    names = []
    for p in pays:
        if p.method:
            names.append(p.method.name_ar)
        else:
            pm = db.get(PaymentMethod, p.payment_method_id)
            if pm:
                names.append(pm.name_ar)
    label = "، ".join(dict.fromkeys(names)) if names else None
    if sale.customer_id:
        from modules.customers.service import loyalty_discount_for_sale

        loyalty = loyalty_discount_for_sale(db, sale.id, sale.customer_id)
        if loyalty > 0:
            ltxt = f"نقاط ولاء ({loyalty.quantize(Decimal('0.001'))} د.ل)"
            label = f"{label} + {ltxt}" if label else ltxt
    if not label:
        if get_open_room_charge(db, sale.id):
            return "حساب شقة"
        return None
    return label


def list_pos_shift_orders_hub(
    db: Session,
    *,
    pos_shift_id: int,
    user: User,
    user_id: int,
    current_sale_id: int | None = None,
    limit: int = 150,
) -> list[PosOrderHubRow]:
    pos_filter = and_(
        Sale.source == SaleSource.POS,
        or_(
            Sale.pos_shift_id == pos_shift_id,
            and_(Sale.pos_shift_id.is_(None), Sale.created_by_id == user_id),
        ),
    )
    web_chat_filter = and_(
        Sale.source == SaleSource.ONLINE,
        or_(
            Sale.external_order_id.like("webchat:%"),
            Sale.external_order_id.like("webstore:%"),
        ),
        or_(
            Sale.pos_shift_id == pos_shift_id,
            and_(
                Sale.status == SaleStatus.DRAFT,
                Sale.sent_to_kitchen_at.is_(None),
            ),
        ),
    )
    stmt = (
        select(Sale)
        .where(or_(pos_filter, web_chat_filter))
        .options(
            selectinload(Sale.table),
            selectinload(Sale.customer),
        )
        .order_by(Sale.id.desc())
        .limit(limit)
    )
    sales = list(db.scalars(stmt).all())
    for s in sales:
        ensure_sale_shift(db, s, pos_shift_id)
        if s.status == SaleStatus.DRAFT:
            sync_served_from_kds(db, s)

    sale_ids = [s.id for s in sales]
    kmap = _kitchen_agg(db, sale_ids)
    handoffs: dict[int, DeliveryHandoff] = {}
    if sale_ids:
        for h in db.scalars(
            select(DeliveryHandoff).where(DeliveryHandoff.sale_id.in_(sale_ids))
        ).all():
            handoffs[h.sale_id] = h

    policy = load_order_policy(db)
    from modules.sales.invoice_actions import (
        sale_primary_payment_method_id,
        user_can_correct_payment,
        user_can_print_receipt,
        user_can_refund_sale,
    )

    rows: list[PosOrderHubRow] = []
    for s in sales:
        pipe = build_pipeline_view(
            db, s, kitchen_agg=kmap.get(s.id), handoff=handoffs.get(s.id)
        )
        pay_lbl, pay_key = _pay_status(db, s)
        net = (
            sale_remaining_total(db, s.id)
            if s.status == SaleStatus.COMPLETED
            else Decimal(str(s.total or 0))
        )
        paid = sum_sale_payments(db, s.id) if s.status == SaleStatus.COMPLETED else Decimal("0")
        can_open = s.status == SaleStatus.DRAFT
        can_checkout = s.status == SaleStatus.DRAFT and can_pay_sale(db, s, policy)
        is_del = sale_is_delivery(s)
        can_mark_served = (
            s.status == SaleStatus.DRAFT
            and is_del
            and bool(s.sent_to_kitchen_at)
            and s.served_to_customer_at is None
        )
        can_assign_driver = (
            s.status == SaleStatus.DRAFT
            and is_del
            and bool(s.sent_to_kitchen_at)
        )
        can_change_payment = (
            s.status == SaleStatus.COMPLETED
            and user_can_correct_payment(user)
            and pay_key in ("paid", "partial")
            and paid > 0
        )
        can_refund = (
            s.status == SaleStatus.COMPLETED
            and user_can_refund_sale(user)
            and net > 0
            and pay_key in ("paid", "partial", "room")
            and (paid > 0 or pay_key == "room")
        )
        can_print = (
            s.status == SaleStatus.COMPLETED and user_can_print_receipt(user)
        )
        can_edit_invoice = (
            s.status == SaleStatus.COMPLETED
            and user_has_permission(user, SALES_EDIT_INVOICE)
        )
        pm_name = _payment_method_label(db, s)
        pm_id = sale_primary_payment_method_id(db, s.id)
        label = sale_pos_order_label(s)
        from modules.messaging.chat_order_service import is_online_guest_sale

        if is_online_guest_sale(s):
            label = f"💬 شات · {label}"
        rows.append(
            PosOrderHubRow(
                id=s.id,
                label=label,
                pipeline_status=pipe.label,
                pipeline_key=pipe.key,
                pipeline_hint=pipe.hint,
                pay_status=pay_lbl,
                pay_status_key=pay_key,
                total=Decimal(str(s.total or 0)),
                created_at=s.created_at,
                payment_method_name=pm_name,
                payment_method_id=pm_id,
                driver_label=pipe.driver_label,
                can_open=can_open,
                can_checkout=can_checkout,
                can_mark_served=can_mark_served,
                can_assign_driver=can_assign_driver,
                can_change_payment=can_change_payment,
                can_refund=can_refund,
                can_print=can_print,
                can_edit_invoice=can_edit_invoice,
                is_active=current_sale_id is not None and s.id == current_sale_id,
            )
        )
    return rows
