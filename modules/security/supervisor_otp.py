"""OTP واتساب لاعتماد الاسترداد — مشرف المطعم للمطعم، مشرف الفندق للفندق فقط."""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy import DateTime, Integer, String

from infra.db import Base
from modules.hr.models import Employee, EmployeeStatus
from modules.messaging.models import MessageChannel
from modules.messaging.outbox import enqueue_message, send_outbox_item_now
from modules.messaging.phone_utils import normalize_whatsapp_phone
from modules.platform.business_domain import BusinessDomain, parse_employee_domain

log = logging.getLogger("security.supervisor_otp")

PURPOSE_POS_REFUND = "pos_refund"
PURPOSE_HOTEL_REFUND = "hotel_refund"
PURPOSE_HOTEL_CANCEL = "hotel_cancel"

# مفتاح الإعداد → (افتراضي مفعّل؟, تسمية عربية)
OTP_POLICY: dict[str, tuple[str, bool, str]] = {
    PURPOSE_HOTEL_REFUND: (
        "otp_require_hotel_refund",
        False,
        "استرداد أموال الفندق للنزيل — بدون رمز مشرف",
    ),
    PURPOSE_POS_REFUND: (
        "otp_require_pos_refund",
        True,
        "ترجيع وجبات المطعم (كاشير / مرتجعات)",
    ),
    PURPOSE_HOTEL_CANCEL: (
        "otp_require_hotel_cancel",
        True,
        "إلغاء حجز شقة أو No Show",
    ),
}

OTP_TTL_SECONDS = 5 * 60
OTP_MAX_ATTEMPTS = 5
OTP_LENGTH = 6

SESSION_POS_EXP = "pos_refund_auth_exp"
SESSION_POS_SALE = "pos_refund_auth_sale_id"
SESSION_HOTEL_EXP = "hotel_refund_auth_exp"
SESSION_HOTEL_BOOKING = "hotel_refund_auth_booking_id"
SESSION_AUTH_TTL_SEC = 900


class SupervisorOtpError(ValueError):
    pass


class SupervisorOtpChallenge(Base):
    __tablename__ = "supervisor_otp_challenges"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    purpose: Mapped[str] = mapped_column(String(40), index=True)
    domain: Mapped[str] = mapped_column(String(20), index=True)
    ref_type: Mapped[str] = mapped_column(String(20), index=True)
    ref_id: Mapped[int] = mapped_column(Integer, index=True)
    code_hash: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    requested_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


def _hash_code(code: str) -> str:
    return hashlib.sha256((code or "").strip().encode("utf-8")).hexdigest()


def _gen_code() -> str:
    # 6 أرقام بدون بادئة أصفار مربكة — يبقى دائماً 6 خانات
    n = secrets.randbelow(1_000_000)
    return f"{n:06d}"


def normalize_supervisor_phone(raw: str | None) -> str:
    phone = normalize_whatsapp_phone((raw or "").strip(), country_code="218")
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 9:
        raise SupervisorOtpError("رقم واتساب المشرف غير صالح.")
    return phone


def require_supervisor_whatsapp(*, is_supervisor: bool, phone: str | None) -> None:
    if not is_supervisor:
        return
    try:
        normalize_supervisor_phone(phone)
    except SupervisorOtpError as exc:
        raise SupervisorOtpError(
            "عند تفعيل «مشرف» يجب إدخال رقم واتساب صالح — يُرسل عليه رمز اعتماد الاسترداد."
        ) from exc


def list_refund_supervisors(
    db: Session, domain: BusinessDomain | str
) -> list[Employee]:
    """مشرفو النطاق فقط: مطعم←مطعم، فندق←فندق."""
    dom = parse_employee_domain(
        domain.value if isinstance(domain, BusinessDomain) else str(domain)
    )
    rows = list(
        db.scalars(
            select(Employee).where(
                Employee.is_pos_supervisor.is_(True),
                Employee.status == EmployeeStatus.ACTIVE,
                Employee.business_domain == dom.value,
            )
        ).all()
    )
    out: list[Employee] = []
    for emp in rows:
        try:
            normalize_supervisor_phone(emp.phone)
        except SupervisorOtpError:
            continue
        out.append(emp)
    return out


def refund_supervisors_configured(db: Session, domain: BusinessDomain | str) -> bool:
    return bool(list_refund_supervisors(db, domain))


def _domain_label(domain: BusinessDomain) -> str:
    return "الفندق" if domain == BusinessDomain.HOTEL else "المطعم"


def _send_otp_whatsapp(
    db: Session,
    *,
    supervisors: list[Employee],
    code: str,
    domain: BusinessDomain,
    purpose: str,
    ref_label: str,
) -> int:
    body = (
        f"رمز اعتماد ({_domain_label(domain)})\n"
        f"{ref_label}\n"
        f"الرمز: {code}\n"
        f"صالح لمدة {OTP_TTL_SECONDS // 60} دقائق.\n"
        f"لا تشارك الرمز مع أحد."
    )
    sent = 0
    for emp in supervisors:
        try:
            phone = normalize_supervisor_phone(emp.phone)
        except SupervisorOtpError:
            continue
        row = enqueue_message(
            db,
            body=body,
            channel=MessageChannel.WHATSAPP.value,
            phone=phone,
            event_type=f"supervisor_otp.{purpose}",
            meta={
                "purpose": purpose,
                "domain": domain.value,
                "employee_id": emp.id,
            },
        )
        try:
            send_outbox_item_now(db, row)
            sent += 1
        except Exception:  # noqa: BLE001
            log.exception("failed sending refund OTP to employee=%s", emp.id)
    return sent


def request_refund_otp(
    db: Session,
    *,
    purpose: str,
    domain: BusinessDomain | str,
    ref_type: str,
    ref_id: int,
    ref_label: str,
    requested_by_user_id: int | None = None,
) -> dict:
    """ينشئ تحدياً ويرسل OTP لمشرفي نفس النطاق فقط."""
    dom = parse_employee_domain(
        domain.value if isinstance(domain, BusinessDomain) else str(domain)
    )
    if purpose == PURPOSE_POS_REFUND and dom != BusinessDomain.RESTAURANT:
        raise SupervisorOtpError("استرداد المطعم يُرسل لمشرفي المطعم فقط.")
    if purpose in (PURPOSE_HOTEL_REFUND, PURPOSE_HOTEL_CANCEL) and dom != BusinessDomain.HOTEL:
        raise SupervisorOtpError("رمز الفندق يُرسل لمشرفي الفندق فقط.")
    if not otp_purpose_required(db, purpose):
        raise SupervisorOtpError(
            "طلب الرمز غير مطلوب لهذه العملية — عطّل الأدمن اعتماد OTP لهذا الإجراء."
        )

    supervisors = list_refund_supervisors(db, dom)
    if not supervisors:
        raise SupervisorOtpError(
            f"لا يوجد مشرف نشط لنطاق {_domain_label(dom)} برقم واتساب. "
            "عيّن موظفاً كمشرف مع رقم واتساب من بطاقة الموظف."
        )

    code = _gen_code()
    now = datetime.now(timezone.utc)
    challenge = SupervisorOtpChallenge(
        purpose=purpose,
        domain=dom.value,
        ref_type=ref_type,
        ref_id=int(ref_id),
        code_hash=_hash_code(code),
        expires_at=now + timedelta(seconds=OTP_TTL_SECONDS),
        attempts=0,
        requested_by_user_id=requested_by_user_id,
    )
    db.add(challenge)
    db.flush()

    sent = _send_otp_whatsapp(
        db,
        supervisors=supervisors,
        code=code,
        domain=dom,
        purpose=purpose,
        ref_label=ref_label,
    )
    if sent <= 0:
        raise SupervisorOtpError(
            "تعذّر إرسال رمز واتساب للمشرف. تحقق من إعدادات المراسلة ورقم المشرف."
        )
    db.commit()
    return {
        "challenge_id": int(challenge.id),
        "expires_in": OTP_TTL_SECONDS,
        "sent_to": sent,
        "domain": dom.value,
    }


def verify_refund_otp(
    db: Session,
    *,
    purpose: str,
    domain: BusinessDomain | str,
    ref_type: str,
    ref_id: int,
    code: str,
) -> SupervisorOtpChallenge:
    dom = parse_employee_domain(
        domain.value if isinstance(domain, BusinessDomain) else str(domain)
    )
    plain = (code or "").strip()
    if len(plain) != OTP_LENGTH or not plain.isdigit():
        raise SupervisorOtpError("أدخل رمز الاعتماد المكوّن من 6 أرقام.")

    now = datetime.now(timezone.utc)
    challenge = db.scalar(
        select(SupervisorOtpChallenge)
        .where(
            SupervisorOtpChallenge.purpose == purpose,
            SupervisorOtpChallenge.domain == dom.value,
            SupervisorOtpChallenge.ref_type == ref_type,
            SupervisorOtpChallenge.ref_id == int(ref_id),
            SupervisorOtpChallenge.consumed_at.is_(None),
            SupervisorOtpChallenge.expires_at > now,
        )
        .order_by(SupervisorOtpChallenge.id.desc())
        .limit(1)
    )
    if challenge is None:
        raise SupervisorOtpError("لا يوجد رمز ساري — اطلب إرسال رمز جديد للمشرف.")

    if int(challenge.attempts or 0) >= OTP_MAX_ATTEMPTS:
        raise SupervisorOtpError("تجاوزت محاولات إدخال الرمز. اطلب رمزاً جديداً.")

    challenge.attempts = int(challenge.attempts or 0) + 1
    if _hash_code(plain) != challenge.code_hash:
        db.commit()
        raise SupervisorOtpError("رمز الاعتماد غير صحيح.")

    challenge.consumed_at = now
    db.commit()
    return challenge


def grant_pos_refund_session(session: dict, sale_id: int) -> None:
    import time

    session[SESSION_POS_EXP] = time.time() + SESSION_AUTH_TTL_SEC
    session[SESSION_POS_SALE] = int(sale_id)


def pos_refund_session_ok(session: dict, sale_id: int | None = None) -> bool:
    import time

    raw = session.get(SESSION_POS_EXP)
    try:
        exp = float(raw)
    except (TypeError, ValueError):
        return False
    if exp <= time.time():
        return False
    if sale_id is not None:
        try:
            if int(session.get(SESSION_POS_SALE) or 0) != int(sale_id):
                return False
        except (TypeError, ValueError):
            return False
    return True


def save_otp_policy(
    db: Session,
    *,
    hotel_refund: bool,
    pos_refund: bool,
    hotel_cancel: bool,
) -> None:
    """يحفظ مفاتيح OTP. ترجيع مبلغ النزيل لا يُطلب له رمز أبداً."""
    from modules.settings.service import invalidate_settings_cache, set_setting

    del hotel_refund  # مغادرة / إيصال صرف للنزيل: بدون OTP
    set_setting(db, "otp_require_hotel_refund", "0")
    set_setting(db, "otp_require_pos_refund", "1" if pos_refund else "0")
    set_setting(db, "otp_require_hotel_cancel", "1" if hotel_cancel else "0")
    invalidate_settings_cache()


def ensure_hotel_guest_refund_otp_off(db: Session) -> None:
    """يُطفئ رمز المشرف لترجيع مبلغ النزيل إن كان مفعّلاً من إعداد قديم."""
    from modules.settings.service import get_setting, invalidate_settings_cache, set_setting

    if (get_setting(db, "otp_require_hotel_refund", "0") or "0").strip() != "0":
        set_setting(db, "otp_require_hotel_refund", "0")
        invalidate_settings_cache()


def otp_purpose_required(db: Session, purpose: str) -> bool:
    """هل يُطلب OTP لهذا الغرض حسب إعدادات الأدمن؟"""
    from modules.settings.service import get_bool

    if purpose == PURPOSE_HOTEL_REFUND:
        return False
    meta = OTP_POLICY.get(purpose)
    if meta is None:
        # أغراض غير معروفة: لا تُعطّل بالخطأ
        return True
    key, default_on, _label = meta
    return get_bool(db, key, default_on)


def otp_policy_context(db: Session) -> dict[str, bool | str]:
    """قيم الواجهة للإعدادات + اختصارات القوالب."""
    from modules.settings.service import get_bool

    ctx: dict[str, bool | str] = {}
    for purpose, (key, default_on, label) in OTP_POLICY.items():
        enabled = get_bool(db, key, default_on)
        ctx[key] = enabled
        ctx[f"{key}_label"] = label
        if purpose == PURPOSE_HOTEL_REFUND:
            ctx["otp_require_hotel_refund"] = False
            ctx["hotel_refund_otp_required"] = False
        elif purpose == PURPOSE_POS_REFUND:
            ctx["otp_require_pos_refund"] = enabled
            ctx["pos_refund_otp_required"] = enabled
        elif purpose == PURPOSE_HOTEL_CANCEL:
            ctx["otp_require_hotel_cancel"] = enabled
            ctx["hotel_cancel_otp_required"] = enabled
    return ctx


def require_otp_or_skip(
    db: Session,
    *,
    purpose: str,
    domain: BusinessDomain | str,
    ref_type: str,
    ref_id: int,
    code: str,
    session: dict | None = None,
    booking_id: int | None = None,
    sale_id: int | None = None,
    is_admin: bool = False,
) -> bool:
    """يتحقق من OTP إن كان مطلوباً. يعيد True إذا مُرّر / غير مطلوب.

    للأدمن: لا OTP مطلقاً (كما كان سلوك الاسترداد).
    """
    if is_admin:
        return True
    if not otp_purpose_required(db, purpose):
        return True
    if purpose == PURPOSE_HOTEL_REFUND and session is not None:
        if hotel_refund_session_ok(session, booking_id if booking_id is not None else ref_id):
            return True
    if purpose == PURPOSE_POS_REFUND and session is not None and sale_id is not None:
        if pos_refund_session_ok(session, sale_id):
            return True
    verify_refund_otp(
        db,
        purpose=purpose,
        domain=domain,
        ref_type=ref_type,
        ref_id=ref_id,
        code=code,
    )
    if purpose == PURPOSE_HOTEL_REFUND and session is not None:
        grant_hotel_refund_session(session, booking_id if booking_id is not None else ref_id)
    if purpose == PURPOSE_POS_REFUND and session is not None:
        grant_pos_refund_session(session, sale_id if sale_id is not None else ref_id)
    return True


def grant_hotel_refund_session(session: dict, booking_id: int) -> None:
    import time

    session[SESSION_HOTEL_EXP] = time.time() + SESSION_AUTH_TTL_SEC
    session[SESSION_HOTEL_BOOKING] = int(booking_id)


def hotel_refund_session_ok(session: dict, booking_id: int | None = None) -> bool:
    import time

    raw = session.get(SESSION_HOTEL_EXP)
    try:
        exp = float(raw)
    except (TypeError, ValueError):
        return False
    if exp <= time.time():
        return False
    if booking_id is not None:
        try:
            if int(session.get(SESSION_HOTEL_BOOKING) or 0) != int(booking_id):
                return False
        except (TypeError, ValueError):
            return False
    return True
