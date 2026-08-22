"""مساعدات صلاحيات مركّبة (وظيفة = صلاحية دقيقة أو صلاحية أوسع متوافقة)."""
from __future__ import annotations

from modules.authz.permissions import (
    HOTEL_FINANCE_CLOSE,
    HOTEL_SETTLE_TRANSFER,
    HR_MANAGE,
    HR_PAYROLL_PAY,
    PAYMENTS_MANAGE,
    POS_ADMIN_CLOSE_SHIFT,
    POS_SHIFT_REOPEN,
    POS_SHORTAGE_DEDUCT,
    SALES_EDIT_INVOICE,
    TREASURY_HANDOFF_APPROVE,
    TREASURY_HANDOFF_REVOKE,
)
from modules.authz.service import user_has_permission


def can_approve_treasury_handoff(user) -> bool:
    return user_has_permission(user, TREASURY_HANDOFF_APPROVE) or user_has_permission(
        user, PAYMENTS_MANAGE
    )


def can_revoke_treasury_handoff(user) -> bool:
    return user_has_permission(user, TREASURY_HANDOFF_REVOKE)


def can_reopen_closed_shift(user) -> bool:
    """إعادة جلسة مغلقة لمسودة — أدمن النظام أو من مُنحت له الصلاحية."""
    from modules.platform.business_domain import is_system_admin

    if is_system_admin(user):
        return True
    return user_has_permission(user, POS_SHIFT_REOPEN)


def can_admin_close_pos_shift(user) -> bool:
    if is_treasury_clerk_user(user):
        return False
    return user_has_permission(user, POS_ADMIN_CLOSE_SHIFT) or user_has_permission(
        user, PAYMENTS_MANAGE
    )


def can_admin_close_hotel_shift(user) -> bool:
    if is_treasury_clerk_user(user):
        return False
    return user_has_permission(user, PAYMENTS_MANAGE) or user_has_permission(
        user, HOTEL_FINANCE_CLOSE
    )


def can_apply_shortage_deduction(user) -> bool:
    return (
        user_has_permission(user, POS_SHORTAGE_DEDUCT)
        or user_has_permission(user, HR_MANAGE)
        or user_has_permission(user, PAYMENTS_MANAGE)
    )


def can_pay_payroll(user) -> bool:
    return user_has_permission(user, HR_PAYROLL_PAY) or user_has_permission(
        user, HR_MANAGE
    )


def can_edit_invoice(user) -> bool:
    return user_has_permission(user, SALES_EDIT_INVOICE)


def can_hotel_settle_transfer(user) -> bool:
    """تنفيذ تحويل فندق→مطعم (كاش/مصرف): صلاحية التسوية أو إدارة الخزينة."""
    return user_has_permission(user, HOTEL_SETTLE_TRANSFER) or user_has_permission(
        user, PAYMENTS_MANAGE
    )


def hotel_settle_transfer_blocked_reason(user, session=None) -> str | None:
    """سبب منع التحويل، أو None إن مسموح في الوضع الحالي."""
    from modules.platform.business_domain import BusinessDomain, resolve_finance_domain

    if resolve_finance_domain(user, session) == BusinessDomain.RESTAURANT:
        return (
            "تحويل التسوية إلى المطعم يتم من وضع الفندق فقط. "
            "وضع المطعم لعرض الدين المطلوب دون اتخاذ إجراء."
        )
    if can_hotel_settle_transfer(user):
        return None
    return (
        "ليس لديك صلاحية تنفيذ تحويل التسوية فندق↔مطعم. "
        "يلزم «تنفيذ تحويل التسوية» أو «إدارة الخزينة»."
    )


def can_hotel_settle_transfer_now(user, session=None) -> bool:
    """صلاحية التحويل مع منع وضع المطعم (العرض فقط)."""
    return hotel_settle_transfer_blocked_reason(user, session) is None


def is_restaurant_finance_view(user, session=None) -> bool:
    from modules.platform.business_domain import BusinessDomain, resolve_finance_domain

    return resolve_finance_domain(user, session) == BusinessDomain.RESTAURANT


def treasury_clerk_desk_redirect(user):
    """يعيد أمين الخزينة إلى مكتبه إن حاول فتح جلسة بيع/استقبال."""
    if is_treasury_clerk_user(user):
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/pos/treasury/desk", status_code=302)
    return None


def is_treasury_clerk_user(user) -> bool:
    """دور أمين الخزينة فقط — ليس أدمن النظام حتى لو حمل صلاحيات مالية."""
    from modules.authz.permissions import TREASURY_CLERK_ROLE_NAME_AR
    from modules.platform.business_domain import is_system_admin, user_role_names

    if user is None or is_system_admin(user):
        return False
    return TREASURY_CLERK_ROLE_NAME_AR in user_role_names(user)


def can_settle_from_main_treasury(user) -> bool:
    """أدمن النظام وأمين الخزينة يدفعان من الخزينة الرئيسية عند التسوية."""
    from modules.platform.business_domain import is_system_admin

    if user is None:
        return False
    if is_system_admin(user):
        return True
    if is_treasury_clerk_user(user):
        return True
    return user_has_permission(user, PAYMENTS_MANAGE)


def can_settle_without_hotel_shift(user) -> bool:
    """التسوية من الخزينة لا تتطلب جلسة استقبال مفتوحة."""
    return can_settle_from_main_treasury(user)
