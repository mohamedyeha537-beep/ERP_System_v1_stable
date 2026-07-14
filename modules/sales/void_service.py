"""إلغاء/مسح أصناف أو فاتورة بعد الإرسال للمطبخ — بتفويض مشرف."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from modules.authz.models import User
from modules.authz.permissions import SALES_VOID_SUPERVISOR
from modules.authz.service import user_has_permission
from modules.hr.models import Employee, EmployeeStatus
from modules.hr.pos_pin import verify_pos_pin
from modules.inventory.service import apply_movement, get_product_sales_warehouse_id
from modules.inventory.models import StockMovementType
from modules.sales.models import Sale, SaleLine, SaleStatus
from modules.sales.service import (
    SalesError,
    _aggregate_component_needs_for_delta,
    _recalc_sale_total,
    line_kitchen_sent_qty,
    load_sale_with_lines,
)


class VoidError(SalesError):
    pass


def _employee_is_void_supervisor(db: Session, emp: Employee) -> bool:
    if emp.is_pos_supervisor:
        return True
    if emp.user_id is None:
        return False
    user = db.get(User, emp.user_id)
    return user is not None and user_has_permission(user, SALES_VOID_SUPERVISOR)


def void_supervisor_configured(db: Session) -> bool:
    """هل يوجد مشرف يمكنه اعتماد الإلغاء (PIN + صلاحية)؟"""
    for emp in db.scalars(
        select(Employee).where(
            Employee.status == EmployeeStatus.ACTIVE,
            Employee.pos_pin_hash.isnot(None),
        )
    ).all():
        if _employee_is_void_supervisor(db, emp):
            return True
    return False


def _normalize_phone_digits(phone: str) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())[-10:]


def find_void_supervisor_for_phone(db: Session, phone: str) -> Employee | None:
    """يُطابق مشرف الإلغاء برقم واتساب المشرف أو هاتف الموظف."""
    from modules.settings.service import get_setting

    target = _normalize_phone_digits(phone)
    if not target:
        return None
    admin = _normalize_phone_digits(get_setting(db, "messaging_admin_phone") or "")
    matches: list[Employee] = []
    for emp in db.scalars(
        select(Employee).where(Employee.status == EmployeeStatus.ACTIVE)
    ).all():
        if not _employee_is_void_supervisor(db, emp):
            continue
        emp_phone = _normalize_phone_digits(emp.phone or "")
        if emp_phone and emp_phone == target:
            matches.append(emp)
    if len(matches) == 1:
        return matches[0]
    if admin and target == admin:
        for emp in db.scalars(
            select(Employee).where(Employee.status == EmployeeStatus.ACTIVE)
        ).all():
            if _employee_is_void_supervisor(db, emp):
                return emp
    return None


def void_pin_failure_can_request_supervisor(msg: str) -> bool:
    return "غير صحيح" in msg or "يجب أن يكون 4" in msg


def authenticate_void_supervisor(db: Session, pin: str) -> Employee:
    pin = (pin or "").strip()
    if len(pin) != 4 or not pin.isdigit():
        raise VoidError("كود المشرف يجب أن يكون 4 أرقام.")
    if not void_supervisor_configured(db):
        raise VoidError(
            "لم يُضبط مشرف للإلغاء بعد. فعّل «مشرف نقطة بيع» للموظف وحدّد PIN، "
            "أو امنح صلاحية اعتماد الإلغاء لحساب مرتبط."
        )
    matches: list[Employee] = []
    for emp in db.scalars(
        select(Employee).where(
            Employee.status == EmployeeStatus.ACTIVE,
            Employee.pos_pin_hash.isnot(None),
        )
    ).all():
        if not verify_pos_pin(pin, emp.pos_pin_hash):
            continue
        if _employee_is_void_supervisor(db, emp):
            matches.append(emp)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise VoidError("كود المشرف مكرر لأكثر من موظف — راجع المدير.")
    raise VoidError("كود المشرف غير صحيح.")


def _restore_inventory_for_void(
    db: Session,
    *,
    sale_id: int,
    items: list[tuple[SaleLine, Decimal]],
    user_id: int | None,
    note: str,
) -> None:
    needs = _aggregate_component_needs_for_delta(db, items)
    if not needs:
        return
    from modules.sales.models import Sale

    sale = db.get(Sale, sale_id)
    psid = sale.pos_shift_id if sale else None
    for pid, need in needs.items():
        sales_wh = get_product_sales_warehouse_id(db, pid, pos_shift_id=psid)
        apply_movement(
            db,
            product_id=pid,
            quantity_delta=need,
            movement_type=StockMovementType.SALE_RETURN,
            user_id=user_id,
            sale_id=sale_id,
            warehouse_id=sales_wh,
            note=note[:500],
        )


def void_sent_line(
    db: Session,
    *,
    sale_id: int,
    line_id: int,
    reason: str,
    supervisor: Employee,
    user_id: int | None,
) -> Sale:
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise VoidError("اذكر سبب المسح (3 أحرف على الأقل).")

    sale = load_sale_with_lines(db, sale_id)
    if sale is None or sale.status != SaleStatus.DRAFT:
        raise VoidError("الفاتورة غير موجودة أو ليست قابلة للتعديل.")
    if sale.sent_to_kitchen_at is None:
        raise VoidError("هذا الصنف لم يُرسَل للمطبخ — استخدم الحذف العادي.")

    line = db.get(
        SaleLine,
        line_id,
        options=(selectinload(SaleLine.product),),
    )
    if line is None or line.sale_id != sale_id:
        raise VoidError("البند غير موجود.")
    sent = line_kitchen_sent_qty(line)
    if sent <= 0:
        raise VoidError("هذا الصنف لم يُرسَل للمطبخ بعد.")

    void_qty = line.quantity
    void_items = [(line, void_qty)]
    sup_label = (supervisor.full_name_ar or "").strip() or f"#{supervisor.id}"
    note = f"إلغاء صنف — {sup_label}: {reason}"
    _restore_inventory_for_void(
        db,
        sale_id=sale_id,
        items=[(line, sent)],
        user_id=user_id,
        note=note,
    )

    from modules.kds.service import create_void_kitchen_tickets

    create_void_kitchen_tickets(db, sale, [(line, sent)], reason)
    db.delete(line)
    db.flush()
    _recalc_sale_total(db, sale)

    if not sale.lines:
        _finalize_voided_sale(db, sale, reason=reason)
    db.flush()
    return sale


def void_sent_sale(
    db: Session,
    *,
    sale_id: int,
    reason: str,
    supervisor: Employee,
    user_id: int | None,
) -> Sale:
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise VoidError("اذكر سبب الإلغاء (3 أحرف على الأقل).")

    sale = load_sale_with_lines(db, sale_id)
    if sale is None or sale.status != SaleStatus.DRAFT:
        raise VoidError("الفاتورة غير موجودة أو ليست قابلة للإلغاء.")
    if sale.sent_to_kitchen_at is None:
        raise VoidError("الطلب لم يُرسَل للمطبخ — استخدم إلغاء الطلب العادي.")
    if not sale.lines:
        raise VoidError("الفاتورة فارغة.")

    sup_label = (supervisor.full_name_ar or "").strip() or f"#{supervisor.id}"
    note = f"إلغاء فاتورة — {sup_label}: {reason}"
    void_items: list[tuple[SaleLine, Decimal]] = []
    for ln in list(sale.lines):
        sent = line_kitchen_sent_qty(ln)
        if sent > 0:
            void_items.append((ln, sent))

    if void_items:
        _restore_inventory_for_void(
            db,
            sale_id=sale_id,
            items=void_items,
            user_id=user_id,
            note=note,
        )
        from modules.kds.service import cancel_sale_kitchen_tickets, create_void_kitchen_tickets

        create_void_kitchen_tickets(db, sale, void_items, reason)
        cancel_sale_kitchen_tickets(db, sale_id)

    _finalize_voided_sale(db, sale, reason=reason)
    db.flush()
    return sale


def cancel_kds_rejected_sale(
    db: Session,
    *,
    sale_id: int,
    reason: str,
    user_id: int | None,
) -> Sale:
    """إلغاء طلب مرسل للمطبخ بعد رفضه من KDS، بدون PIN لأن الرفض مصدره المطبخ."""
    reason = (reason or "").strip() or "رفض من شاشة المطبخ"
    sale = load_sale_with_lines(db, sale_id)
    if sale is None or sale.status != SaleStatus.DRAFT:
        raise VoidError("الطلب غير موجود أو لم يعد قابلاً للإلغاء.")
    if sale.sent_to_kitchen_at is None:
        raise VoidError("الطلب لم يُرسل للمطبخ.")

    void_items: list[tuple[SaleLine, Decimal]] = []
    for ln in list(sale.lines):
        sent = line_kitchen_sent_qty(ln)
        if sent > 0:
            void_items.append((ln, sent))

    if void_items:
        _restore_inventory_for_void(
            db,
            sale_id=sale_id,
            items=void_items,
            user_id=user_id,
            note=f"إلغاء رفض المطبخ: {reason}"[:500],
        )

    from modules.kds.service import cancel_sale_kitchen_tickets

    cancel_sale_kitchen_tickets(db, sale_id)
    _finalize_voided_sale(db, sale, reason=reason)
    db.flush()
    return sale


def _finalize_voided_sale(db: Session, sale: Sale, *, reason: str = "") -> None:
    sale.status = SaleStatus.CANCELLED
    try:
        from modules.notifications.marketing_hooks import emit_order_cancelled

        emit_order_cancelled(db, sale, reason=reason or "إلغاء بعد المطبخ")
    except Exception:  # noqa: BLE001
        pass
