"""جلسة وردية الفندق — رقم سري الموظف والتحقق من الوردية المفتوحة."""
from __future__ import annotations

from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.authz.kiosk import requires_hotel_shift_pin
from modules.authz.models import User
from modules.hr.models import Employee, EmployeeStatus
from modules.hr.pos_pin import validate_pos_pin, verify_pos_pin
from modules.hr.service import get_employee_by_user_id
from modules.hotel.shift_models import HotelShift, HotelShiftError
from modules.hotel.shift_service import get_open_shift, open_shift
from modules.platform.business_domain import BusinessDomain


class HotelShiftSessionError(Exception):
    pass


class HotelShiftRedirectNeeded(Exception):
    """يُرفع لإعادة التوجيه إلى PIN أو افتتاح الجلسة (مثل كاشير المطعم)."""

    def __init__(self, location: str):
        self.location = location
        super().__init__(location)


def session_hotel_employee_id(request) -> int | None:
    raw = request.session.get("hotel_employee_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def session_hotel_shift_id(request) -> int | None:
    raw = request.session.get("hotel_shift_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def clear_hotel_operator_session(request) -> None:
    request.session.pop("hotel_employee_id", None)
    request.session.pop("hotel_shift_id", None)


def sync_session_hotel_shift(request, db: Session) -> HotelShift | None:
    open_s = get_open_shift(db)
    if open_s is not None:
        request.session["hotel_shift_id"] = open_s.id
        sess_emp = session_hotel_employee_id(request)
        if open_s.employee_id is not None and sess_emp is None:
            request.session["hotel_employee_id"] = open_s.employee_id
        return open_s
    request.session.pop("hotel_shift_id", None)
    return None


def _employee_is_hotel_front(emp: Employee) -> bool:
    if getattr(emp, "is_hotel_front", False):
        return True
    dom = (emp.business_domain or "").strip().lower()
    return dom == BusinessDomain.HOTEL.value and bool(emp.pos_pin_hash)


def _list_hotel_front_with_pin(db: Session) -> list[Employee]:
    active = list(
        db.scalars(
            select(Employee).where(
                Employee.status == EmployeeStatus.ACTIVE,
                Employee.pos_pin_hash.isnot(None),
            )
        ).all()
    )
    return [e for e in active if _employee_is_hotel_front(e)]


def _matches_pin(employees: list[Employee], pin: str) -> list[Employee]:
    return [e for e in employees if verify_pos_pin(pin, e.pos_pin_hash)]


def authenticate_hotel_employee_pin(db: Session, user: User, pin: str) -> Employee:
    """يتحقق من الرقم السري ويعيد موظف الاستقبال.

    محطة مشتركة: أي رقم سري لموظف استقبال نشط يُقبل،
    مع تفضيل الموظف المرتبط بحساب الدخول إن طابق الرقم.
    """
    try:
        pin = validate_pos_pin(pin)
    except ValueError as e:
        raise HotelShiftSessionError(str(e)) from e

    hotel_staff = _list_hotel_front_with_pin(db)
    matches = _matches_pin(hotel_staff, pin)
    linked = get_employee_by_user_id(db, user.id)

    if linked is not None:
        if linked.status != EmployeeStatus.ACTIVE:
            raise HotelShiftSessionError("حساب الموظف المرتبط غير نشط.")
        # تفضيل المرتبط إن كان رقمُه الصحيح
        if (
            _employee_is_hotel_front(linked)
            and linked.pos_pin_hash
            and verify_pos_pin(pin, linked.pos_pin_hash)
        ):
            return linked
        # محطة مشتركة: رقم موظف استقبال آخر
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise HotelShiftSessionError(
                "رقم سري مكرر بين أكثر من موظف استقبال — راجع المدير."
            )
        if not _employee_is_hotel_front(linked):
            raise HotelShiftSessionError(
                "حسابك غير مفعّل كموظف استقبال فندق. "
                "من «الموظفون» → فعّل «موظف استقبال فندق» + مجال العمل فندق + رقم سري 4 أرقام."
            )
        if not linked.pos_pin_hash:
            raise HotelShiftSessionError(
                "لم يُعيّن رقم سري على سجل الموظف المرتبط بهذا الحساب. "
                "من «الموظفون» → عدّل الموظف → أدخل 4 أرقام (مثل 1234) واحفظ."
            )
        raise HotelShiftSessionError(
            "الرقم السري غير صحيح. "
            "أعد تعيينه من «الموظفون» على نفس الموظف المرتبط بحسابك، ثم احفظ."
        )

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise HotelShiftSessionError(
            "رقم سري مكرر بين أكثر من موظف استقبال — راجع المدير."
        )
    if not hotel_staff:
        raise HotelShiftSessionError(
            "لا يوجد أي موظف استقبال برقم سري. "
            "من «الموظفون» فعّل «موظف استقبال فندق» وعيّن 4 أرقام."
        )
    raise HotelShiftSessionError("الرقم السري غير صحيح.")


def pin_authenticate_for_hotel_shift(
    db: Session,
    request,
    user: User,
    pin: str,
) -> tuple[HotelShift | None, bool]:
    """يعيد (وردية مفتوحة أو None، هل يلزم صفحة فتح وردية)."""
    emp = authenticate_hotel_employee_pin(db, user, pin)
    existing = get_open_shift(db)
    if existing is not None:
        request.session["hotel_employee_id"] = emp.id
        owner_id = getattr(existing, "employee_id", None)
        if owner_id and int(owner_id) != int(emp.id):
            request.session.pop("hotel_shift_id", None)
            return existing, False
        request.session["hotel_shift_id"] = existing.id
        if existing.employee_id is None:
            existing.employee_id = emp.id
            db.flush()
        return existing, False
    request.session["hotel_employee_id"] = emp.id
    request.session.pop("hotel_shift_id", None)
    return None, True


def require_hotel_shift_session(
    request, db: Session, user: User
) -> HotelShift | RedirectResponse | None:
    """None = لا يلزم وردية (مدير). RedirectResponse = إعادة توجيه. HotelShift = جاهز."""
    if not requires_hotel_shift_pin(user):
        return sync_session_hotel_shift(request, db)

    emp_id = session_hotel_employee_id(request)
    if emp_id is None:
        return RedirectResponse("/admin/hotel/pin", status_code=302)

    open_s = sync_session_hotel_shift(request, db)
    if open_s is None:
        return RedirectResponse("/admin/hotel/shift/start", status_code=302)
    owner_id = getattr(open_s, "employee_id", None)
    if owner_id and emp_id and int(owner_id) != int(emp_id):
        return RedirectResponse("/admin/hotel/shift/incoming", status_code=302)
    return open_s


def enforce_hotel_shift_session(
    request, db: Session, user: User
) -> HotelShift | None:
    """مثل require_hotel_shift_session لكن يرفع HotelShiftRedirectNeeded بدل RedirectResponse."""
    result = require_hotel_shift_session(request, db, user)
    if isinstance(result, RedirectResponse):
        location = result.headers.get("location") or "/admin/hotel/pin"
        raise HotelShiftRedirectNeeded(location)
    return result


def guard_hotel_shift_start_page(
    request, db: Session, user: User
) -> RedirectResponse | None:
    """حراسة صفحة افتتاح الجلسة — بدون حلقة إعادة توجيه (مثل نقطة البيع)."""
    if requires_hotel_shift_pin(user) and session_hotel_employee_id(request) is None:
        return RedirectResponse("/admin/hotel/pin", status_code=302)
    if get_open_shift(db) is not None:
        return RedirectResponse("/admin/hotel/dashboard", status_code=302)
    return None


def open_hotel_shift_after_pin(
    db: Session,
    request,
    user: User,
    *,
    shift_number: int | None = None,
    opening_note: str | None = None,
    opening_cash: str | None = None,
    received_cash: str | None = None,
    received_bank: str | None = None,
) -> HotelShift:
    from decimal import Decimal

    if get_open_shift(db) is not None:
        raise HotelShiftError("يوجد جلسة مفتوحة بالفعل.")
    emp_id = session_hotel_employee_id(request)
    if emp_id is None:
        raise HotelShiftError("أدخل الرقم السري أولاً.")
    cash = Decimal(str(opening_cash or "0")).quantize(Decimal("0.001"))
    if cash < 0:
        raise HotelShiftError("رصيد الافتتاح غير صالح.")
    shift = open_shift(
        db,
        user_id=user.id,
        employee_id=emp_id,
        opening_note=opening_note,
        shift_number=shift_number,
        opening_cash=cash,
        received_cash=received_cash,
        received_bank=received_bank,
    )
    request.session["hotel_shift_id"] = shift.id
    return shift
