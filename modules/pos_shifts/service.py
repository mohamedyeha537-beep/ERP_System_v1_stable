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
    from modules.hr.pos_pin import validate_pos_pin

    try:
        pin = validate_pos_pin(pin)
    except ValueError as e:
        raise PosShiftError(str(e)) from e

    linked = get_employee_by_user_id(db, user.id)
    if linked is not None:
        if not linked.is_pos_cashier or not linked.pos_pin_hash:
            raise PosShiftError(
                "حسابك غير مفعّل ككاشير أو بلا رقم سري. "
                "من «الموظفون» فعّل «كاشير نقطة بيع» وعيّن 4 أرقام."
            )
        if linked.status != EmployeeStatus.ACTIVE:
            raise PosShiftError("حساب الموظف غير نشط.")
        if not verify_pos_pin(pin, linked.pos_pin_hash):
            raise PosShiftError(
                "الرقم السري غير صحيح. أعد تعيينه من «الموظفون» على نفس الموظف المرتبط ثم احفظ."
            )
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
    if not active:
        raise PosShiftError("لا يوجد كاشير برقم سري. من «الموظفون» عيّن رقم سري لكاشير.")
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


def shift_opening_bank(db: Session, shift_id: int) -> Decimal:
    raw = db.scalar(select(PosShift.opening_bank).where(PosShift.id == shift_id))
    return Decimal(str(raw or 0)).quantize(Decimal("0.001"))


def _pos_carry_label(sh: PosShift) -> str:
    if getattr(sh, "employee", None) is not None and (sh.employee.full_name_ar or "").strip():
        return sh.employee.full_name_ar.strip()
    if getattr(sh, "user", None) is not None and (sh.user.username or "").strip():
        return sh.user.username.strip()
    return f"جلسة مطعم #{sh.id}"


def get_pending_pos_carry(db: Session, *, employee_id: int | None = None) -> PosShift | None:
    from modules.payments.shift_carry import CLOSE_DEST_NEXT_SHIFT
    from sqlalchemy import or_

    stmt = (
        select(PosShift)
        .options(selectinload(PosShift.employee), selectinload(PosShift.user))
        .where(
            PosShift.status == PosShiftStatus.CLOSED,
            PosShift.close_destination == CLOSE_DEST_NEXT_SHIFT,
            PosShift.carried_to_shift_id.is_(None),
        )
        .order_by(PosShift.id.asc())
    )
    if employee_id:
        stmt = stmt.where(
            or_(
                PosShift.carried_to_employee_id == int(employee_id),
                PosShift.carried_to_employee_id.is_(None),
            )
        )
    return db.execute(stmt.limit(1)).scalar_one_or_none()


def list_active_pos_cashiers(
    db: Session, *, exclude_employee_id: int | None = None
) -> list[dict]:
    from modules.authz.models import User
    from modules.platform.business_domain import BusinessDomain

    rows = list(
        db.scalars(
            select(Employee)
            .options(selectinload(Employee.department))
            .join(User, User.id == Employee.user_id)
            .where(
                Employee.status == EmployeeStatus.ACTIVE,
                Employee.user_id.is_not(None),
                User.is_active.is_(True),
                Employee.business_domain.in_(
                    (BusinessDomain.RESTAURANT.value, BusinessDomain.SHARED.value)
                ),
            )
            .order_by(Employee.full_name_ar)
        ).all()
    )
    cashiers = [
        emp
        for emp in rows
        if getattr(emp, "is_pos_cashier", False)
        or str(getattr(getattr(emp, "department", None), "code", "") or "").upper()
        in ("POS", "REST", "KITCHEN", "CASHIER")
    ]
    chosen = cashiers or rows
    skip = int(exclude_employee_id) if exclude_employee_id else None
    out: list[dict] = []
    for emp in chosen:
        if skip is not None and int(emp.id) == skip:
            continue
        out.append(
            {
                "id": int(emp.id),
                "name": (emp.full_name_ar or "").strip() or f"موظف #{emp.id}",
            }
        )
    return out


def peek_pending_pos_carry_offer(db: Session, *, employee_id: int | None = None):
    from modules.payments.shift_carry import ShiftCarryOffer, money3

    src = get_pending_pos_carry(db, employee_id=employee_id)
    if src is None:
        return None
    recipient = ""
    if getattr(src, "carried_to_employee_id", None):
        emp = db.get(Employee, int(src.carried_to_employee_id))
        if emp is not None:
            recipient = (emp.full_name_ar or "").strip()
    return ShiftCarryOffer(
        shift_id=src.id,
        cash=money3(src.counted_cash),
        bank=money3(src.counted_bank),
        cashier_label=_pos_carry_label(src),
        closed_at=src.closed_at,
        recipient_label=recipient,
        remainder_needs_count=True,
    )


def consume_pending_pos_carry(
    db: Session,
    shift: PosShift,
    *,
    received_cash=None,
    received_bank=None,
) -> PosShift | None:
    from modules.payments.shift_carry import money3
    from modules.payments.shift_variance_models import ShiftVarianceSource
    from modules.payments.shift_variances import record_pair_variances

    src = get_pending_pos_carry(db, employee_id=shift.employee_id)
    if src is None:
        return None
    if received_cash is None or received_bank is None:
        raise PosShiftError(
            "يوجد رصيد مرحّل من الجلسة السابقة. أدخل المبلغ الذي استلمته وعددته فعلياً."
        )
    claimed_cash = money3(src.counted_cash)
    claimed_bank = money3(src.counted_bank)
    rec_cash = money3(received_cash)
    rec_bank = money3(received_bank)
    if rec_cash < 0 or rec_bank < 0:
        raise PosShiftError("مبلغ الاستلام غير صالح.")
    shift.opening_cash = rec_cash
    shift.opening_bank = rec_bank
    shift.received_from_shift_id = src.id
    src.carried_to_shift_id = shift.id
    auto = (
        f"استلام من الجلسة #{src.id} "
        f"(المسلِّم: كاش {claimed_cash} / مصرف {claimed_bank} — "
        f"المستلم: كاش {rec_cash} / مصرف {rec_bank})"
    )
    prev = (shift.opening_note or "").strip()
    shift.opening_note = f"{prev} — {auto}".strip(" —") if prev else auto
    record_pair_variances(
        db,
        source_type=ShiftVarianceSource.POS_CARRY,
        claimed_cash=claimed_cash,
        received_cash=rec_cash,
        claimed_bank=claimed_bank,
        received_bank=rec_bank,
        pos_shift_id=src.id,
        from_employee_id=src.employee_id,
        to_employee_id=shift.employee_id,
        note=f"تسليم عهدة من جلسة مطعم #{src.id} إلى جلسة #{shift.id}",
    )
    db.flush()
    return src


def open_shift(
    db: Session,
    user_id: int,
    *,
    employee_id: int | None = None,
    opening_note: str | None = None,
    opening_cash: Decimal | str | None = None,
    warehouse_id: int | None = None,
    received_cash: Decimal | str | None = None,
    received_bank: Decimal | str | None = None,
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
    consume_pending_pos_carry(
        db, sh, received_cash=received_cash, received_bank=received_bank
    )
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
    opening_bank = shift_opening_bank(db, shift_id)

    expected_cash = (
        opening_cash
        + cash_sales
        - delivery_expenses
        - shift_cash_expenses
        - cash_refunds
    ).quantize(Decimal("0.001"))
    expected_bank = (
        opening_bank + bank_sales - shift_bank_expenses - bank_refunds
    ).quantize(Decimal("0.001"))

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
    responsible_employee_id: int | None = None,
    close_destination: str | None = None,
    carried_to_employee_id: int | None = None,
    enforce_close_policy: bool = True,
) -> PosShift:
    # ربط الموظف المسؤول قبل تسجيل العجز (مهم عند إغلاق الأدمن بدون PIN)
    if responsible_employee_id is not None and int(responsible_employee_id) > 0:
        from modules.hr.models import Employee

        emp = db.get(Employee, int(responsible_employee_id))
        if emp is None:
            raise PosShiftError("الموظف المسؤول عن الصندوق غير موجود.")
        sh.employee_id = int(emp.id)
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
    from modules.payments.shift_carry import (
        CLOSE_DEST_NEXT_SHIFT,
        CLOSE_DEST_TREASURY,
        ShiftCarryError,
        load_pos_shift_close_policy,
        resolve_close_destination,
    )

    try:
        dest = resolve_close_destination(
            db,
            close_destination,
            enforce_policy=enforce_close_policy,
            policy=load_pos_shift_close_policy(db),
        )
    except ShiftCarryError as exc:
        raise PosShiftError(str(exc)) from exc
    if dest == CLOSE_DEST_NEXT_SHIFT:
        emp_id = int(carried_to_employee_id) if carried_to_employee_id else 0
        allowed = {
            int(e["id"])
            for e in list_active_pos_cashiers(db, exclude_employee_id=sh.employee_id)
        }
        if emp_id <= 0 or emp_id not in allowed:
            raise PosShiftError("اختر الكاشير المستلم للعهدة.")
        pending = get_pending_pos_carry(db, employee_id=emp_id)
        if pending is not None:
            raise PosShiftError(
                f"يوجد رصيد مرحّل للموظف من الجلسة #{pending.id} لم يُستلم بعد."
            )
        sh.carried_to_employee_id = emp_id
    else:
        sh.carried_to_employee_id = None
        dest = CLOSE_DEST_TREASURY
    sh.close_destination = dest
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
        from modules.pos_shifts.shortages import difference_shortage_amount

        cashier_name = ""
        if sh.user_id:
            u = db.get(User, int(sh.user_id))
            cashier_name = (u.username if u else "") or ""
        employee_name = ""
        if sh.employee_id:
            from modules.hr.models import Employee

            emp = db.get(Employee, int(sh.employee_id))
            employee_name = ((emp.full_name_ar if emp else "") or "").strip()
        if not employee_name:
            employee_name = cashier_name
        cash_short = difference_shortage_amount(sh.cash_difference)
        if loyalty_absorbed and loyalty_absorbed > 0:
            cash_short = max(
                cash_short - Decimal(str(loyalty_absorbed)), Decimal("0")
            ).quantize(Decimal("0.001"))
        bank_short = difference_shortage_amount(sh.bank_difference)
        emit_pos_shift_closed(
            db,
            shift_id=shift_id,
            cashier_name=cashier_name,
            employee_name=employee_name,
            shortage=cash_diff,
            cash_shortage=cash_short,
            bank_shortage=bank_short,
        )
        from modules.notifications.treasury_hooks import emit_treasury_shift_closed

        emit_treasury_shift_closed(
            db,
            shift_id=shift_id,
            cashier_name=employee_name or cashier_name,
            counted_cash=sh.counted_cash or Decimal("0"),
            expected_cash=sh.expected_cash or Decimal("0"),
            cash_difference=sh.cash_difference or Decimal("0"),
            counted_bank=sh.counted_bank or Decimal("0"),
            expected_bank=sh.expected_bank or Decimal("0"),
            bank_difference=sh.bank_difference or Decimal("0"),
        )
        from modules.payments.shift_carry import is_next_shift_destination

        if not is_next_shift_destination(getattr(sh, "close_destination", None)):
            from modules.notifications.treasury_hooks import emit_treasury_handoff_pending
            from app.datetime_local import format_local_dt

            emit_treasury_handoff_pending(
                db,
                shift_id=shift_id,
                cashier_name=employee_name or cashier_name,
                counted_cash=sh.counted_cash or Decimal("0"),
                counted_bank=sh.counted_bank or Decimal("0"),
                closed_at=format_local_dt(sh.closed_at, "%Y-%m-%d %H:%M") if sh.closed_at else "",
                reminder_slot="initial",
                shift_kind="restaurant",
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
    responsible_employee_id: int | None = None,
    close_destination: str | None = None,
    carried_to_employee_id: int | None = None,
    enforce_close_policy: bool = True,
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
    # بدون موظف مرتبط (إغلاق بأدمن): يجب اختيار المسؤول عن الصندوق لربط أي عجز بالراتب
    if sh.employee_id is None and not (
        responsible_employee_id and int(responsible_employee_id) > 0
    ):
        raise PosShiftError(
            "اختر الموظف المسؤول عن الصندوق قبل الإغلاق — "
            "حتى يُربط أي عجز بخصم الراتب في صفحة الخصومات."
        )
    return _finalize_shift_close(
        db,
        sh,
        counted_cash=counted_cash,
        counted_bank=counted_bank,
        counted_room=counted_room,
        closing_note=closing_note,
        close_destination=close_destination,
        carried_to_employee_id=carried_to_employee_id,
        enforce_close_policy=enforce_close_policy,
        responsible_employee_id=responsible_employee_id,
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
    responsible_employee_id: int | None = None,
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

    if counted_cash is None:
        raise PosShiftError("أدخل المبلغ المعدود للكاش — لا يُقفَل تلقائياً بالمتوقع.")
    if counted_bank is None:
        raise PosShiftError("أدخل المبلغ المعدود للمصرف — لا يُقفَل تلقائياً بالمتوقع.")
    cc = counted_cash.quantize(Decimal("0.001"))
    cb = counted_bank.quantize(Decimal("0.001"))
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

    emp_id = responsible_employee_id
    if sh.employee_id is None and not (emp_id and int(emp_id) > 0):
        raise PosShiftError(
            "اختر الموظف المسؤول عن الصندوق قبل الإغلاق الإداري — "
            "حتى يُربط أي عجز بخصم الراتب."
        )

    return _finalize_shift_close(
        db,
        sh,
        counted_cash=cc,
        counted_bank=cb,
        counted_room=cr,
        closing_note=note,
        responsible_employee_id=emp_id,
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


def reopen_closed_shift_to_draft(
    db: Session,
    *,
    shift_id: int,
    user_id: int,
    admin_username: str,
    reason: str = "",
) -> PosShift:
    """يعيد جلسة مغلقة إلى مفتوحة لتعديل المعدود ثم إعادة الإقفال."""
    sh = db.get(PosShift, shift_id)
    if sh is None or sh.status != PosShiftStatus.CLOSED:
        raise PosShiftError("الجلسة غير موجودة أو ليست مغلقة.")
    other = get_open_shift_for_user(db, int(sh.user_id))
    if other is not None:
        raise PosShiftError(
            f"لا يمكن إعادة الجلسة لمسودة: للكاشير جلسة مفتوحة #{other.id}. أغلقها أولاً."
        )
    if getattr(sh, "carried_to_shift_id", None):
        raise PosShiftError(
            "الرصيد رُحِّل لجلسة لاحقة. لا يمكن إعادة هذه الجلسة لمسودة."
        )
    from modules.pos_shifts.models import PosShiftShortage
    from modules.pos_shifts.shortages import _shortage_is_resolved

    shortage_rows = list(
        db.scalars(
            select(PosShiftShortage).where(PosShiftShortage.shift_id == int(shift_id))
        ).all()
    )
    resolved = [r for r in shortage_rows if _shortage_is_resolved(r)]
    if resolved:
        raise PosShiftError(
            "لا يمكن إعادة المسودة: عجز هذه الجلسة مُعالَج (خصم راتب أو عفو). "
            "أزل المعالجة أولاً إن أردت تصحيح العدّ."
        )
    try:
        from modules.payments.shift_variances import ShiftVariance, ShiftVarianceStatus

        decided = list(
            db.scalars(
                select(ShiftVariance).where(
                    ShiftVariance.pos_shift_id == int(shift_id),
                    ShiftVariance.status != ShiftVarianceStatus.PENDING_REVIEW,
                )
            ).all()
        )
        if decided:
            raise PosShiftError(
                "لا يمكن إعادة المسودة: يوجد عجز/زيادة معتمد على هذه الجلسة."
            )
        for row in db.scalars(
            select(ShiftVariance).where(ShiftVariance.pos_shift_id == int(shift_id))
        ).all():
            db.delete(row)
    except PosShiftError:
        raise
    except Exception:  # noqa: BLE001
        pass
    why = (reason or "").strip() or "تصحيح أرقام المعدود بعد الإقفال"
    if sh.treasury_handoff_at is not None:
        from modules.payments.shift_handoff_service import (
            ShiftHandoffError,
            revoke_shift_handoff,
        )

        try:
            revoke_shift_handoff(
                db,
                shift_id=int(shift_id),
                user_id=user_id,
                admin_username=admin_username,
                reason=why,
            )
        except ShiftHandoffError as exc:
            raise PosShiftError(str(exc)) from exc
    for row in shortage_rows:
        db.delete(row)
    sh.status = PosShiftStatus.OPEN
    sh.closed_at = None
    sh.counted_cash = None
    sh.counted_bank = None
    sh.counted_room = None
    sh.cash_difference = None
    sh.bank_difference = None
    sh.room_difference = None
    sh.expected_cash = None
    sh.expected_bank = None
    sh.expected_room = None
    sh.close_destination = None
    sh.carried_to_employee_id = None
    line = f"[إعادة لمسودة — {admin_username}: {why}]"
    prev = (sh.closing_note or "").strip()
    sh.closing_note = (prev + " | " + line).strip(" | ") if prev else line
    db.flush()
    return sh
