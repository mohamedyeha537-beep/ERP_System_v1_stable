from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from modules.authz.kiosk import is_cashier_kiosk_user, requires_pos_pin
from modules.authz.models import User
from modules.delivery.models import DeliveryCashSettlement
from modules.hr.models import Employee, EmployeeStatus
from modules.hr.pos_pin import verify_pos_pin
from modules.hr.service import get_employee_by_user_id
from modules.payments.models import PaymentMethod, PaymentMethodKind, RefundPayment, SalePayment
from modules.pos_shifts.models import PosShift, PosShiftStatus
from modules.refunds.models import SaleReturn
from modules.hotel.models import RoomCharge
from modules.sales.models import Sale, SaleStatus


class PosShiftError(Exception):
    pass


@dataclass
class ShiftFinancialSummary:
    """ملخص مالي للجلسة: قبض مبيعات منفصل عن مصروفات التوصيل."""

    cash_sales: Decimal
    bank_sales: Decimal
    room_account_sales: Decimal
    delivery_expenses: Decimal
    shift_cash_expenses: Decimal
    shift_bank_expenses: Decimal
    cash_refunds: Decimal
    bank_refunds: Decimal
    expected_cash_drawer: Decimal
    expected_bank: Decimal
    opening_cash: Decimal = Decimal("0")
    invoice_sales_total: Decimal = Decimal("0")
    net_collected: Decimal = Decimal("0")
    loyalty_redeem_count: int = 0
    loyalty_points_redeemed: Decimal = Decimal("0")
    loyalty_dinar_cost: Decimal = Decimal("0")


def get_shift(db: Session, shift_id: int) -> PosShift | None:
    return db.get(PosShift, shift_id)


def get_open_shift_for_user(db: Session, user_id: int) -> PosShift | None:
    return db.execute(
        select(PosShift)
        .where(
            PosShift.user_id == user_id,
            PosShift.status == PosShiftStatus.OPEN,
        )
        .options(selectinload(PosShift.employee))
        .order_by(PosShift.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def sync_session_pos_shift(request, db: Session, user_id: int) -> PosShift | None:
    """يضبط `session[pos_shift_id]` بحسب الجلسة المفتوحة في قاعدة البيانات."""
    open_s = get_open_shift_for_user(db, user_id)
    if open_s is not None:
        request.session["pos_shift_id"] = open_s.id
        if open_s.employee_id is not None:
            request.session["pos_employee_id"] = open_s.employee_id
        return open_s
    request.session.pop("pos_shift_id", None)
    return None


def session_pos_employee_id(request) -> int | None:
    raw = request.session.get("pos_employee_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def clear_pos_operator_session(request) -> None:
    request.session.pop("pos_employee_id", None)
    request.session.pop("pos_shift_id", None)


def authenticate_employee_pin(db: Session, user: User, pin: str) -> Employee:
    """يتحقق من الرقم السري ويعيد سجل الموظف."""
    pin = (pin or "").strip()
    if len(pin) != 4 or not pin.isdigit():
        raise PosShiftError("الرقم السري يجب أن يكون 4 أرقام.")

    linked = get_employee_by_user_id(db, user.id)
    if linked is not None:
        if not linked.is_pos_cashier or not linked.pos_pin_hash:
            raise PosShiftError("حسابك غير مفعّل ككاشير. راجع المدير.")
        if linked.status != EmployeeStatus.ACTIVE:
            raise PosShiftError("حساب الموظف غير نشط.")
        if not verify_pos_pin(pin, linked.pos_pin_hash):
            raise PosShiftError("الرقم السري غير صحيح.")
        return linked

    active = list(
        db.scalars(
            select(Employee).where(
                Employee.status == EmployeeStatus.ACTIVE,
                Employee.is_pos_cashier.is_(True),
                Employee.pos_pin_hash.isnot(None),
            )
        ).all()
    )
    matches = [e for e in active if verify_pos_pin(pin, e.pos_pin_hash)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise PosShiftError("رقم سري مكرر لأكثر من موظف — راجع المدير.")
    raise PosShiftError("الرقم السري غير صحيح.")


def pin_authenticate_for_shift(
    db: Session,
    request,
    user: User,
    pin: str,
) -> tuple[PosShift | None, bool]:
    """تحقق PIN — يعيد (جلسة موجودة أو None، هل يلزم صفحة رصيد الافتتاح)."""
    emp = authenticate_employee_pin(db, user, pin)
    existing = get_open_shift_for_user(db, user.id)
    if existing is not None:
        if existing.employee_id and existing.employee_id != emp.id:
            raise PosShiftError("هناك جلسة مفتوحة لموظف آخر. أغلقها أولاً.")
        request.session["pos_employee_id"] = emp.id
        request.session["pos_shift_id"] = existing.id
        if existing.employee_id is None:
            existing.employee_id = emp.id
            db.flush()
        return existing, False
    request.session["pos_employee_id"] = emp.id
    request.session.pop("pos_shift_id", None)
    return None, True


def start_shift_with_pin(
    db: Session,
    request,
    user: User,
    pin: str,
    *,
    opening_note: str | None = None,
    opening_cash: Decimal | str | None = None,
) -> PosShift:
    """للتوافق — يفتح جلسة مباشرة إن وُجد رصيد افتتاح."""
    existing, need_start = pin_authenticate_for_shift(db, request, user, pin)
    if existing is not None:
        return existing
    if need_start and opening_cash is None:
        raise PosShiftError("أدخل رصيد الافتتاح في الدرج.")
    emp_id = session_pos_employee_id(request)
    sh = open_shift(
        db,
        user.id,
        employee_id=emp_id,
        opening_note=opening_note,
        opening_cash=opening_cash,
    )
    request.session["pos_shift_id"] = sh.id
    return sh


def require_open_pos_shift(request, db: Session, user: User) -> PosShift | RedirectResponse:
    s = sync_session_pos_shift(request, db, user.id)
    if requires_pos_pin(user):
        emp_id = session_pos_employee_id(request)
        if emp_id is None:
            return RedirectResponse("/pos/pin", status_code=302)
        if s is None:
            return RedirectResponse("/pos/shift/start", status_code=302)
        if s.employee_id is not None and s.employee_id != emp_id:
            clear_pos_operator_session(request)
            return RedirectResponse("/pos/pin", status_code=302)
        return s
    if s is None:
        return RedirectResponse("/pos/shift/start", status_code=302)
    return s


def session_pos_shift_id(request) -> int | None:
    raw = request.session.get("pos_shift_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _parse_opening_cash(value: Decimal | str | None) -> Decimal:
    if value is None or (isinstance(value, str) and not value.strip()):
        return Decimal("0")
    amt = Decimal(str(value)).quantize(Decimal("0.001"))
    if amt < 0:
        raise PosShiftError("رصيد الافتتاح لا يمكن أن يكون سالباً.")
    return amt


def shift_opening_cash(db: Session, shift_id: int) -> Decimal:
    raw = db.scalar(select(PosShift.opening_cash).where(PosShift.id == shift_id))
    return Decimal(str(raw or 0)).quantize(Decimal("0.001"))


def open_shift(
    db: Session,
    user_id: int,
    *,
    employee_id: int | None = None,
    opening_note: str | None = None,
    opening_cash: Decimal | str | None = None,
    warehouse_id: int | None = None,
) -> PosShift:
    if get_open_shift_for_user(db, user_id) is not None:
        raise PosShiftError("لديك جلسة مفتوحة بالفعل. أغلقها قبل فتح جلسة جديدة.")
    note = (opening_note or "").strip() or None
    oc = _parse_opening_cash(opening_cash)
    resolved_wh: int | None = None
    if warehouse_id is not None:
        from modules.inventory.models import Warehouse

        w = db.get(Warehouse, int(warehouse_id))
        if w is not None and w.is_active and w.deduct_sales_enabled:
            resolved_wh = w.id
    sh = PosShift(
        user_id=user_id,
        employee_id=employee_id,
        status=PosShiftStatus.OPEN,
        opening_note=note,
        opening_cash=oc,
        warehouse_id=resolved_wh,
    )
    db.add(sh)
    db.flush()
    try:
        from modules.authz.models import User
        from modules.notifications.marketing_hooks import emit_pos_shift_opened

        cashier_name = ""
        u = db.get(User, int(user_id))
        cashier_name = (u.username if u else "") or ""
        emit_pos_shift_opened(db, shift_id=sh.id, cashier_name=cashier_name)
    except Exception:  # noqa: BLE001
        pass
    return sh


def compute_shift_financial_summary(db: Session, shift_id: int) -> ShiftFinancialSummary:
    """قبض مبيعات (كاش/مصرف) ومصروفات توصيل منفصلة عن رصيد الدرج المتوقع."""
    cash_sales = db.execute(
        select(func.coalesce(func.sum(SalePayment.amount), 0))
        .select_from(SalePayment)
        .join(Sale, Sale.id == SalePayment.sale_id)
        .join(PaymentMethod, PaymentMethod.id == SalePayment.payment_method_id)
        .where(
            Sale.pos_shift_id == shift_id,
            PaymentMethod.kind == PaymentMethodKind.CASH,
        )
    ).scalar_one()
    cash_sales = Decimal(str(cash_sales or 0)).quantize(Decimal("0.001"))

    bank_sales = db.execute(
        select(func.coalesce(func.sum(SalePayment.amount), 0))
        .select_from(SalePayment)
        .join(Sale, Sale.id == SalePayment.sale_id)
        .join(PaymentMethod, PaymentMethod.id == SalePayment.payment_method_id)
        .where(
            Sale.pos_shift_id == shift_id,
            PaymentMethod.kind == PaymentMethodKind.BANK,
        )
    ).scalar_one()
    bank_sales = Decimal(str(bank_sales or 0)).quantize(Decimal("0.001"))

    delivery_expenses = db.execute(
        select(func.coalesce(func.sum(DeliveryCashSettlement.amount), 0))
        .select_from(DeliveryCashSettlement)
        .join(Sale, Sale.id == DeliveryCashSettlement.sale_id)
        .where(Sale.pos_shift_id == shift_id)
    ).scalar_one()
    delivery_expenses = Decimal(str(delivery_expenses or 0)).quantize(Decimal("0.001"))

    cash_refunds = db.execute(
        select(func.coalesce(func.sum(RefundPayment.amount), 0))
        .select_from(RefundPayment)
        .join(SaleReturn, SaleReturn.id == RefundPayment.sale_return_id)
        .join(Sale, Sale.id == SaleReturn.original_sale_id)
        .join(PaymentMethod, PaymentMethod.id == RefundPayment.payment_method_id)
        .where(
            Sale.pos_shift_id == shift_id,
            PaymentMethod.kind == PaymentMethodKind.CASH,
        )
    ).scalar_one()
    cash_refunds = Decimal(str(cash_refunds or 0)).quantize(Decimal("0.001"))

    bank_refunds = db.execute(
        select(func.coalesce(func.sum(RefundPayment.amount), 0))
        .select_from(RefundPayment)
        .join(SaleReturn, SaleReturn.id == RefundPayment.sale_return_id)
        .join(Sale, Sale.id == SaleReturn.original_sale_id)
        .join(PaymentMethod, PaymentMethod.id == RefundPayment.payment_method_id)
        .where(
            Sale.pos_shift_id == shift_id,
            PaymentMethod.kind == PaymentMethodKind.BANK,
        )
    ).scalar_one()
    bank_refunds = Decimal(str(bank_refunds or 0)).quantize(Decimal("0.001"))

    room_account_sales_raw = db.execute(
        select(func.coalesce(func.sum(Sale.total), 0))
        .select_from(RoomCharge)
        .join(Sale, Sale.id == RoomCharge.sale_id)
        .where(
            Sale.pos_shift_id == shift_id,
            Sale.status == SaleStatus.COMPLETED,
        )
    ).scalar_one()
    room_account_sales = Decimal(str(room_account_sales_raw or 0)).quantize(
        Decimal("0.001")
    )

    from modules.customers.loyalty_shift_reports import loyalty_redeem_summary_for_shift
    from modules.pos_shifts.shift_expenses import (
        sum_shift_bank_expenses,
        sum_shift_cash_expenses,
    )

    loyalty = loyalty_redeem_summary_for_shift(db, shift_id)
    shift_cash_expenses = sum_shift_cash_expenses(db, shift_id)
    shift_bank_expenses = sum_shift_bank_expenses(db, shift_id)
    opening_cash = shift_opening_cash(db, shift_id)

    expected_cash = (
        opening_cash
        + cash_sales
        - delivery_expenses
        - shift_cash_expenses
        - cash_refunds
    ).quantize(Decimal("0.001"))
    expected_bank = (bank_sales - shift_bank_expenses - bank_refunds).quantize(
        Decimal("0.001")
    )

    invoice_sales_total_raw = db.execute(
        select(func.coalesce(func.sum(Sale.total), 0)).where(
            Sale.pos_shift_id == shift_id,
            Sale.status == SaleStatus.COMPLETED,
        )
    ).scalar_one()
    invoice_sales_total = Decimal(str(invoice_sales_total_raw or 0)).quantize(
        Decimal("0.001")
    )
    net_collected = (cash_sales + bank_sales).quantize(Decimal("0.001"))

    return ShiftFinancialSummary(
        cash_sales=cash_sales,
        bank_sales=bank_sales,
        room_account_sales=room_account_sales,
        delivery_expenses=delivery_expenses,
        shift_cash_expenses=shift_cash_expenses,
        shift_bank_expenses=shift_bank_expenses,
        cash_refunds=cash_refunds,
        bank_refunds=bank_refunds,
        opening_cash=opening_cash,
        expected_cash_drawer=expected_cash,
        expected_bank=expected_bank,
        invoice_sales_total=invoice_sales_total,
        net_collected=net_collected,
        loyalty_redeem_count=loyalty.redeem_count,
        loyalty_points_redeemed=loyalty.points_redeemed,
        loyalty_dinar_cost=loyalty.dinar_cost,
    )


def compute_expected_cash(db: Session, shift_id: int) -> Decimal:
    """نقد متوقع في الدرج = مبيعات كاش − توصيل − مصروفات الجلسة − مرتجعات."""
    return compute_shift_financial_summary(db, shift_id).expected_cash_drawer


def compute_expected_bank(db: Session, shift_id: int) -> Decimal:
    """صافي تحصيلات المصرف في الجلسة − مرتجعات المصرف."""
    return compute_shift_financial_summary(db, shift_id).expected_bank


def compute_expected_room(db: Session, shift_id: int) -> Decimal:
    """إجمالي فواتير الشقق (قيد على حساب الشقة) في الجلسة."""
    return compute_shift_financial_summary(db, shift_id).room_account_sales


def _finalize_shift_close(
    db: Session,
    sh: PosShift,
    *,
    counted_cash: Decimal,
    counted_bank: Decimal | None,
    counted_room: Decimal | None = None,
    closing_note: str | None,
) -> PosShift:
    shift_id = sh.id
    expected_c = compute_expected_cash(db, shift_id)
    cash_diff = (counted_cash - expected_c).quantize(Decimal("0.001"))
    sh.status = PosShiftStatus.CLOSED
    sh.closed_at = datetime.now(timezone.utc)
    sh.counted_cash = counted_cash.quantize(Decimal("0.001"))
    sh.expected_cash = expected_c
    sh.cash_difference = cash_diff
    expected_b = compute_expected_bank(db, shift_id)
    cb = (
        counted_bank if counted_bank is not None else expected_b
    ).quantize(Decimal("0.001"))
    sh.counted_bank = cb
    sh.expected_bank = expected_b
    sh.bank_difference = (cb - expected_b).quantize(Decimal("0.001"))
    expected_room = compute_expected_room(db, shift_id)
    cr = (
        counted_room if counted_room is not None else expected_room
    ).quantize(Decimal("0.001"))
    sh.counted_room = cr
    sh.expected_room = expected_room
    sh.room_difference = (cr - expected_room).quantize(Decimal("0.001"))
    sh.closing_note = (closing_note or "").strip() or None
    db.flush()
    from modules.pos_shifts.loyalty_settlement import settle_loyalty_on_shift_close
    from modules.pos_shifts.shortages import record_shortages_for_closed_shift

    loyalty_absorbed = settle_loyalty_on_shift_close(
        db,
        sh,
        counted_cash=counted_cash,
        expected_cash=expected_c,
        user_id=sh.user_id,
    )
    record_shortages_for_closed_shift(
        db, sh, loyalty_cash_absorbed=loyalty_absorbed
    )
    try:
        from modules.pos_shifts.shift_close_gl import run_shift_close_gl

        gl_result = run_shift_close_gl(db, sh)
        sh.gl_backfilled_entries = int(gl_result.backfilled_entries)
        sh.gl_operational_net = gl_result.operational_net
        sh.gl_revenue_net = gl_result.gl_revenue_net
        sh.gl_gap = gl_result.gl_gap
        db.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        from modules.authz.models import User
        from modules.notifications.hooks import emit_pos_shift_closed

        cashier_name = ""
        if sh.user_id:
            u = db.get(User, int(sh.user_id))
            cashier_name = (u.username if u else "") or ""
        emit_pos_shift_closed(
            db,
            shift_id=shift_id,
            cashier_name=cashier_name,
            shortage=cash_diff,
        )
        from modules.notifications.treasury_hooks import emit_treasury_shift_closed

        emit_treasury_shift_closed(
            db,
            shift_id=shift_id,
            cashier_name=cashier_name,
            counted_cash=sh.counted_cash or Decimal("0"),
            expected_cash=sh.expected_cash or Decimal("0"),
            cash_difference=sh.cash_difference or Decimal("0"),
            counted_bank=sh.counted_bank or Decimal("0"),
            expected_bank=sh.expected_bank or Decimal("0"),
            bank_difference=sh.bank_difference or Decimal("0"),
        )
    except Exception:  # noqa: BLE001
        pass
    return sh


def close_shift(
    db: Session,
    *,
    shift_id: int,
    user_id: int,
    counted_cash: Decimal,
    counted_bank: Decimal,
    counted_room: Decimal,
    closing_note: str | None = None,
) -> PosShift:
    sh = db.get(PosShift, shift_id)
    if sh is None or sh.user_id != user_id:
        raise PosShiftError("الجلسة غير موجودة أو لا تخصّك.")
    if sh.status != PosShiftStatus.OPEN:
        raise PosShiftError("هذه الجلسة مغلقة مسبقاً.")
    from modules.sales.service import list_pending_orders_blocking_shift_close

    pending = list_pending_orders_blocking_shift_close(
        db, user_id=user_id, pos_shift_id=shift_id
    )
    if pending:
        n = len(pending)
        raise PosShiftError(
            f"لا يمكن إغلاق الجلسة: لديك {n} طلب/طلبات عالقة. "
            "افتح كل طلب من شاشة الكاشير أو من القائمة أدناه، ثم "
            "أتمِم البيع والدفع أو ألغِ الطلب حسب ما تم فعلياً، وبعدها أعد محاولة الإغلاق."
        )
    if counted_bank is None:
        raise PosShiftError("أدخل المبلغ المعدود لخزينة المصرف بعد مراجعة الفواتير.")
    if counted_room is None:
        raise PosShiftError(
            "أدخل إجماليل فواتير الشقق (قيد على حساب الشقة) بعد مراجعتها."
        )
    return _finalize_shift_close(
        db,
        sh,
        counted_cash=counted_cash,
        counted_bank=counted_bank,
        counted_room=counted_room,
        closing_note=closing_note,
    )


def admin_close_shift(
    db: Session,
    *,
    shift_id: int,
    admin_user_id: int,
    admin_username: str,
    counted_cash: Decimal | None = None,
    counted_bank: Decimal | None = None,
    counted_room: Decimal | None = None,
    closing_note: str | None = None,
    cancel_safe_drafts: bool = True,
    force_ignore_pending: bool = False,
) -> PosShift:
    """إغلاق إداري لجلسة عالقة — لا يتطلب تسجيل دخول الكاشير الأصلي."""
    sh = db.get(PosShift, shift_id)
    if sh is None:
        raise PosShiftError("الجلسة غير موجودة.")
    if sh.status != PosShiftStatus.OPEN:
        raise PosShiftError("الجلسة مغلقة مسبقاً.")

    from modules.sales.service import (
        SalesError,
        cancel_sale,
        list_pending_orders_blocking_shift_close,
        list_pos_open_drafts,
    )

    if cancel_safe_drafts:
        for sale in list_pos_open_drafts(
            db, user_id=sh.user_id, pos_shift_id=shift_id, limit=100
        ):
            if sale.sent_to_kitchen_at is not None:
                continue
            try:
                cancel_sale(db, sale.id)
            except SalesError:
                continue

    pending = list_pending_orders_blocking_shift_close(
        db, user_id=sh.user_id, pos_shift_id=shift_id
    )
    if pending and not force_ignore_pending:
        n = len(pending)
        raise PosShiftError(
            f"لا يمكن الإغلاق: {n} طلب/طلبات عالقة (بعضها مُرسَل للمطبخ). "
            "أكملها من الكاشير، أو فعّل «تجاهل الطلبات العالقة» عند الإغلاق الإداري."
        )

    expected_c = compute_expected_cash(db, shift_id)
    expected_b = compute_expected_bank(db, shift_id)
    cc = (
        counted_cash.quantize(Decimal("0.001"))
        if counted_cash is not None
        else expected_c
    )
    cb = (
        counted_bank.quantize(Decimal("0.001"))
        if counted_bank is not None
        else expected_b
    )
    cr = (
        counted_room.quantize(Decimal("0.001"))
        if counted_room is not None
        else compute_expected_room(db, shift_id)
    )

    parts = [f"[إغلاق إداري — {admin_username}]"]
    if force_ignore_pending and pending:
        parts.append(f"تجاهل {len(pending)} طلب/طلبات عالقة.")
    extra = (closing_note or "").strip()
    if extra:
        parts.append(extra)
    note = " ".join(parts)

    return _finalize_shift_close(
        db,
        sh,
        counted_cash=cc,
        counted_bank=cb,
        counted_room=cr,
        closing_note=note,
    )


def shift_open_age_hours(sh: PosShift, *, now: datetime | None = None) -> float:
    """عمر الجلسة المفتوحة بالساعات."""
    ref = now or datetime.now(timezone.utc)
    opened = sh.opened_at
    if opened is None:
        return 0.0
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return max(0.0, (ref - opened).total_seconds() / 3600.0)


def list_stale_open_shifts(
    db: Session, *, stale_hours: float = 24.0, limit: int = 50
) -> list[PosShift]:
    """جلسات مفتوحة منذ أكثر من stale_hours."""
    open_shifts = list(
        db.scalars(
            select(PosShift)
            .where(PosShift.status == PosShiftStatus.OPEN)
            .options(selectinload(PosShift.employee), selectinload(PosShift.user))
            .order_by(PosShift.opened_at.asc())
            .limit(limit)
        ).all()
    )
    return [s for s in open_shifts if shift_open_age_hours(s) >= stale_hours]


def list_completed_sales_for_shift(db: Session, shift_id: int) -> list[Sale]:
    return list(
        db.scalars(
            select(Sale)
            .where(
                Sale.pos_shift_id == shift_id,
                Sale.status == SaleStatus.COMPLETED,
            )
            .options(selectinload(Sale.table), selectinload(Sale.customer))
            .order_by(Sale.id.asc())
        ).all()
    )


@dataclass
class ShiftSaleRow:
    sale_id: int
    total: Decimal
    context: str
    context_label: str
    created_at: datetime | None
    payment_label: str
    can_print: bool = False
    can_change_payment: bool = False
    can_refund: bool = False


def build_shift_sale_rows(
    db: Session, shift_id: int, user: User | None = None
) -> list[ShiftSaleRow]:
    from modules.sales.invoice_actions import invoice_action_flags, sale_context_label_ar

    sales = list_completed_sales_for_shift(db, shift_id)
    rows: list[ShiftSaleRow] = []
    for s in sales:
        pay = db.execute(
            select(SalePayment, PaymentMethod)
            .join(PaymentMethod, PaymentMethod.id == SalePayment.payment_method_id)
            .where(SalePayment.sale_id == s.id)
            .limit(1)
        ).first()
        if pay:
            _, pm = pay
            pl = f"{pm.name_ar} ({pm.kind.value})"
        else:
            pl = "— (غرفة/آجل)"
        ctx = s.context_type.value if s.context_type else ""
        flags = invoice_action_flags(db, user, s) if user is not None else {}
        rows.append(
            ShiftSaleRow(
                sale_id=s.id,
                total=Decimal(str(s.total or 0)).quantize(Decimal("0.001")),
                context=ctx,
                context_label=sale_context_label_ar(s),
                created_at=s.created_at,
                payment_label=pl,
                can_print=flags.get("can_print", False),
                can_change_payment=flags.get("can_change_payment", False),
                can_refund=flags.get("can_refund", False),
            )
        )
    return rows
