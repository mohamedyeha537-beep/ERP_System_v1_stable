"""جلسة وردية الفندق — رقم سري الموظف والتحقق من الوردية المفتوحة."""
from __future__ import annotations

from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.authz.kiosk import requires_hotel_shift_pin
from modules.authz.models import User
from modules.hr.models import Employee, EmployeeStatus
from modules.hr.pos_pin import verify_pos_pin
from modules.hr.service import get_employee_by_user_id
from modules.hotel.shift_models import HotelShift, HotelShiftError, HotelShiftStatus
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
        if open_s.employee_id is not None:
            request.session["hotel_employee_id"] = open_s.employee_id
        return open_s
    request.session.pop("hotel_shift_id", None)
    return None


def _employee_is_hotel_front(emp: Employee) -> bool:
    if getattr(emp, "is_hotel_front", False):
        return True
    dom = (emp.business_domain or "").strip().lower()
    return dom == BusinessDomain.HOTEL.value and bool(emp.pos_pin_hash)


def authenticate_hotel_employee_pin(db: Session, user: User, pin: str) -> Employee:
    pin = (pin or "").strip()
    if len(pin) != 4 or not pin.isdigit():
        raise HotelShiftSessionError("الرقم السري يجب أن يكون 4 أرقام.")

    linked = get_employee_by_user_id(db, user.id)
    if linked is not None:
        if not _employee_is_hotel_front(linked):
            raise HotelShiftSessionError("حسابك غير مفعّل كموظف استقبال فندق.")
        if linked.status != EmployeeStatus.ACTIVE:
            raise HotelShiftSessionError("حساب الموظف غير نشط.")
        if not linked.pos_pin_hash or not verify_pos_pin(pin, linked.pos_pin_hash):
            raise HotelShiftSessionError("الرقم السري غير صحيح.")
        return linked

    active = list(
        db.scalars(
            select(Employee).where(
                Employee.status == EmployeeStatus.ACTIVE,
                Employee.pos_pin_hash.isnot(None),
            )
        ).all()
    )
    hotel_staff = [e for e in active if _employee_is_hotel_front(e)]
    matches = [e for e in hotel_staff if verify_pos_pin(pin, e.pos_pin_hash)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise HotelShiftSessionError("رقم سري مكرر — راجع المدير.")
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
    )
    request.session["hotel_shift_id"] = shift.id
    return shift
