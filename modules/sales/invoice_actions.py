"""صلاحيات وإجراءات الفاتورة المكتملة (تقرير الجلسة، جميع الطلبات)."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from modules.authz.models import User
from modules.authz.permissions import (
    PAYMENTS_MANAGE,
    SALES_CORRECT_PAYMENT,
    SALES_CREATE,
    SALES_PRINT_RECEIPT,
    SALES_REFUND,
)
from modules.authz.service import user_has_permission
from modules.payments.service import sum_sale_payments
from modules.refunds.service import get_open_room_charge, sale_remaining_total
from modules.sales.models import Sale, SaleStatus


from modules.sales.service import sale_context_label_ar as _sale_context_label_ar


def sale_context_label_ar(sale: Sale) -> str:
    return _sale_context_label_ar(sale, detailed=True)


def user_can_print_receipt(user: User) -> bool:
    return user_has_permission(user, SALES_PRINT_RECEIPT) or user_has_permission(
        user, SALES_CREATE
    )


def user_can_correct_payment(user: User) -> bool:
    return user_has_permission(user, SALES_CORRECT_PAYMENT) or user_has_permission(
        user, PAYMENTS_MANAGE
    )


def user_can_refund_sale(user: User) -> bool:
    return user_has_permission(user, SALES_REFUND)


def sale_allows_payment_correction(db: Session, sale: Sale) -> bool:
    if sale.status != SaleStatus.COMPLETED:
        return False
    paid = sum_sale_payments(db, sale.id)
    if paid <= 0:
        return False
    if get_open_room_charge(db, sale.id) is not None and paid <= 0:
        return False
    return True


def sale_allows_refund(db: Session, sale: Sale) -> bool:
    if sale.status != SaleStatus.COMPLETED:
        return False
    net = sale_remaining_total(db, sale.id)
    if net <= 0:
        return False
    paid = sum_sale_payments(db, sale.id)
    room = get_open_room_charge(db, sale.id)
    if paid > 0 or room is not None:
        return True
    return False


def sale_payment_method_label(db: Session, sale_id: int) -> str | None:
    """اسم وسيلة/خزينة الدفع المسجّلة على الفاتورة."""
    from modules.payments.service import list_sale_payments

    pays = list_sale_payments(db, sale_id)
    if not pays:
        if get_open_room_charge(db, sale_id):
            return "حساب شقة"
        return None
    names = []
    for p in pays:
        if p.method:
            names.append(p.method.name_ar)
        elif p.payment_method_id:
            from modules.payments.models import PaymentMethod

            pm = db.get(PaymentMethod, p.payment_method_id)
            if pm:
                names.append(pm.name_ar)
    return "، ".join(dict.fromkeys(names)) if names else None


def sale_primary_payment_method_id(db: Session, sale_id: int) -> int | None:
    from modules.payments.service import list_sale_payments

    pays = list_sale_payments(db, sale_id)
    if not pays:
        return None
    return pays[0].payment_method_id


def invoice_action_flags(db: Session, user: User, sale: Sale) -> dict[str, bool]:
    return {
        "can_print": user_can_print_receipt(user),
        "can_change_payment": user_can_correct_payment(user)
        and sale_allows_payment_correction(db, sale),
        "can_refund": user_can_refund_sale(user) and sale_allows_refund(db, sale),
    }
