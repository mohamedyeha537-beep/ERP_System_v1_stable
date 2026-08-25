"""سياسة التسكين ويوم التشغيل الفندقي (Business Day).

وفق سياسة يوم التشغيل الفندقي:
- لا تربط ليلة الإقامة بمنتصف الليل التقويمي فقط.
- نهاية يوم التشغيل (افتراضياً 12:00 ظهراً): وصول بعد منتصف الليل وقبل هذا الوقت = ليلة الأمس كاملة.
- من وقت الإغلاق إلى نفس الساعة في اليوم التالي = ليلة واحدة.
- يُحفظ وقت الوصول الفعلي، ويُحسب first_chargeable_night منفصلاً.
- عدد الليالي = تاريخ المغادرة − أول ليلة محتسبة (ليس فرق ساعات عابراً للأيام).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy.orm import Session

from app.datetime_local import now_local, store_timezone
from modules.settings.service import get_bool, get_int, get_setting, set_setting

SETTING_ENABLED = "hotel_checkin_stay_policy_enabled"
SETTING_CHECKIN_TIME = "hotel_default_check_in_time"
SETTING_FULL_NIGHT_HOURS = "hotel_full_night_hours"
SETTING_HALF_NIGHT_HOURS = "hotel_half_night_hours"
SETTING_BUSINESS_CUTOFF = "hotel_business_day_cutoff"
SETTING_EARLY_POLICY = "hotel_early_checkin_policy"
SETTING_EARLY_FIXED = "hotel_early_checkin_fixed_amount"
SETTING_EARLY_PERCENT = "hotel_early_checkin_percent"

DEFAULT_CHECKIN_TIME = "14:00"
DEFAULT_FULL_NIGHT_HOURS = 12
DEFAULT_HALF_NIGHT_HOURS = 6
DEFAULT_BUSINESS_CUTOFF = "12:00"

# FREE | FULL_NIGHT | HALF | FIXED | PERCENT | BLOCK
EarlyPolicy = Literal["FREE", "FULL_NIGHT", "HALF", "FIXED", "PERCENT", "BLOCK"]
EARLY_POLICIES: tuple[str, ...] = (
    "FREE",
    "FULL_NIGHT",
    "HALF",
    "FIXED",
    "PERCENT",
    "BLOCK",
)


def _parse_hhmm(raw: str, default: str = DEFAULT_CHECKIN_TIME) -> tuple[int, int]:
    text = (raw or default).strip() or default
    parts = text.split(":")
    try:
        hour = int(parts[0])
    except (TypeError, ValueError):
        hour = 14
    try:
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        minute = 0
    return max(0, min(23, hour)), max(0, min(59, minute))


def normalize_checkin_time(raw: str) -> str:
    h, m = _parse_hhmm(raw, DEFAULT_CHECKIN_TIME)
    return f"{h:02d}:{m:02d}"


def normalize_cutoff_time(raw: str) -> str:
    h, m = _parse_hhmm(raw, DEFAULT_BUSINESS_CUTOFF)
    return f"{h:02d}:{m:02d}"


def normalize_early_policy(raw: str | None) -> str:
    v = (raw or "FREE").strip().upper()
    return v if v in EARLY_POLICIES else "FREE"


@dataclass(frozen=True)
class CheckinStayPolicy:
    enabled: bool
    check_in_time: str
    full_night_hours: int
    half_night_hours: int
    business_day_cutoff: str  # e.g. 12:00
    early_checkin_policy: str
    early_checkin_fixed: Decimal
    early_checkin_percent: Decimal

    def checkin_at(self, d: date) -> datetime:
        h, m = _parse_hhmm(self.check_in_time)
        return datetime(d.year, d.month, d.day, h, m, tzinfo=store_timezone())

    def cutoff_at(self, d: date) -> datetime:
        h, m = _parse_hhmm(self.business_day_cutoff, DEFAULT_BUSINESS_CUTOFF)
        return datetime(d.year, d.month, d.day, h, m, tzinfo=store_timezone())


@dataclass(frozen=True)
class ArrivalChargePlan:
    """نتيجة تحليل وقت الوصول → ليلة محتسبة + سياسة."""

    actual_at: datetime
    first_chargeable_night: date
    billing_check_in: date  # تاريخ بداية الإقامة في التسعير/الحجز
    nights: int
    reason: str  # AFTER_MIDNIGHT | EARLY_CHECKIN | NORMAL | EARLY_DATE
    after_midnight_previous_night: bool
    early_checkin_same_day: bool
    early_fee_amount: Decimal
    early_fee_label: str
    needs_reception_confirm: bool  # عرض نافذة بعد منتصف الليل
    confirm_message: str
    block_checkin: bool
    block_message: str


def checkin_stay_policy(db: Session) -> CheckinStayPolicy:
    full_h = get_int(db, SETTING_FULL_NIGHT_HOURS, DEFAULT_FULL_NIGHT_HOURS)
    half_h = get_int(db, SETTING_HALF_NIGHT_HOURS, DEFAULT_HALF_NIGHT_HOURS)
    full_h = max(1, min(48, full_h))
    half_h = max(0, min(full_h, half_h))
    try:
        fixed = Decimal(
            str(get_setting(db, SETTING_EARLY_FIXED, "0") or "0")
        ).quantize(Decimal("0.001"))
    except Exception:  # noqa: BLE001
        fixed = Decimal("0")
    try:
        pct = Decimal(
            str(get_setting(db, SETTING_EARLY_PERCENT, "50") or "50")
        ).quantize(Decimal("0.001"))
    except Exception:  # noqa: BLE001
        pct = Decimal("50")
    if pct < 0:
        pct = Decimal("0")
    if pct > 100:
        pct = Decimal("100")
    return CheckinStayPolicy(
        enabled=get_bool(db, SETTING_ENABLED, True),
        check_in_time=normalize_checkin_time(
            get_setting(db, SETTING_CHECKIN_TIME, DEFAULT_CHECKIN_TIME)
            or DEFAULT_CHECKIN_TIME
        ),
        full_night_hours=full_h,
        half_night_hours=half_h,
        business_day_cutoff=normalize_cutoff_time(
            get_setting(db, SETTING_BUSINESS_CUTOFF, DEFAULT_BUSINESS_CUTOFF)
            or DEFAULT_BUSINESS_CUTOFF
        ),
        early_checkin_policy=normalize_early_policy(
            get_setting(db, SETTING_EARLY_POLICY, "FREE")
        ),
        early_checkin_fixed=fixed,
        early_checkin_percent=pct,
    )


def to_local(dt: datetime | None) -> datetime:
    if dt is None:
        return now_local()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=store_timezone())
    return dt.astimezone(store_timezone())


def business_date_for_instant(db: Session, when: datetime | None = None) -> date:
    """تاريخ التشغيل الفندقي: قبل إغلاق اليوم (cut-off) = تاريخ اليوم السابق."""
    pol = checkin_stay_policy(db)
    local = to_local(when)
    ch, cm = _parse_hhmm(pol.business_day_cutoff, DEFAULT_BUSINESS_CUTOFF)
    if (local.hour, local.minute) < (ch, cm):
        return (local.date() - timedelta(days=1))
    return local.date()


def booking_billing_start(
    db: Session,
    *,
    check_in: date,
    when: datetime | None = None,
) -> date:
    """أول ليلة محتسبة عند إنشاء الحجز (تواريخ فقط).

    إن كان الوصول اليوم والوقت الحالي بعد منتصف الليل وقبل نهاية يوم التشغيل
    (ظهراً افتراضياً) تُحتسب ليلة الأمس أيضاً — كأن النزيل جاء أمس.
    الحجوزات بتاريخ مستقبلي تبقى على فرق التواريخ فقط.
    """
    if check_in is None:
        return check_in
    pol = checkin_stay_policy(db)
    local = to_local(when)
    if local.date() != check_in:
        return check_in
    ch, cm = _parse_hhmm(pol.business_day_cutoff, DEFAULT_BUSINESS_CUTOFF)
    if (local.hour, local.minute) < (ch, cm):
        return check_in - timedelta(days=1)
    return check_in


def _combine_local(d: date, hhmm: str, default: str) -> datetime:
    h, m = _parse_hhmm(hhmm, default)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=store_timezone())


def booking_stay_units(
    db: Session,
    *,
    check_in: date,
    check_out: date,
    check_in_time: str | None = None,
    check_out_time: str | None = None,
    when: datetime | None = None,
) -> Decimal:
    """وحدات الليالي عند إنشاء الحجز: يوم التشغيل + ساعات كامل/نصف ليلة.

    - وصول قبل نهاية يوم التشغيل → تُحتسب ليلة الأمس (فرق التواريخ من ذلك اليوم).
    - إقامة في نفس يوم الفوترة (بعد الـ cutoff والمغادرة نفس اليوم):
      تُحوَّل الساعات حسب «ساعات ليلة كاملة» و«ساعات نصف ليلة».
    """
    if check_in is None or check_out is None or check_out < check_in:
        return Decimal("0")
    pol = checkin_stay_policy(db)
    from modules.hotel.late_checkout import late_checkout_policy

    out_pol = late_checkout_policy(db)
    local = to_local(when)
    cin_raw = (check_in_time or "").strip()
    if not cin_raw:
        if check_in == local.date():
            cin_raw = f"{local.hour:02d}:{local.minute:02d}"
        else:
            cin_raw = pol.check_in_time
    cout_raw = (check_out_time or "").strip() or out_pol.checkout_time
    arrival = _combine_local(check_in, cin_raw, pol.check_in_time)
    depart = _combine_local(check_out, cout_raw, out_pol.checkout_time)
    if depart <= arrival:
        return Decimal("0")
    billing = booking_billing_start(db, check_in=check_in, when=arrival)
    calendar = (check_out - billing).days
    if calendar >= 1:
        return Decimal(calendar)
    hours = stay_hours_between(arrival, depart)
    return stay_units_from_hours(
        hours,
        full_night_hours=pol.full_night_hours,
        half_night_hours=pol.half_night_hours,
    )


def booking_night_count(
    db: Session,
    *,
    check_in: date,
    check_out: date,
    when: datetime | None = None,
    check_in_time: str | None = None,
    check_out_time: str | None = None,
) -> int:
    """عدد الليالي الصحيح للأعلى (للواجهات التي تحتاج عدداً صحيحاً)."""
    units = booking_stay_units(
        db,
        check_in=check_in,
        check_out=check_out,
        check_in_time=check_in_time,
        check_out_time=check_out_time,
        when=when,
    )
    if units <= 0:
        return 0
    n = int(units)
    if units > n:
        n += 1
    return max(1, n)


def stay_units_from_hours(
    hours: float | Decimal | int,
    *,
    full_night_hours: int,
    half_night_hours: int,
) -> Decimal:
    """يحوّل مدة الإقامة بالساعات إلى وحدات ليالٍ (0 / 0.5 / 1 / 1.5 …)."""
    h = float(hours or 0)
    if h <= 0:
        return Decimal("0")
    full_h = max(1, int(full_night_hours))
    half_h = max(0, min(full_h, int(half_night_hours)))

    full_units = int(h // full_h)
    rem = h - (full_units * full_h)
    units = Decimal(full_units)
    if rem + 1e-9 >= full_h:
        units += Decimal("1")
    elif half_h > 0 and rem + 1e-9 >= half_h:
        units += Decimal("0.5")
    elif rem > 1e-9 and full_units == 0 and half_h == 0:
        units = Decimal("1")
    elif rem > 1e-9 and full_units == 0:
        units = Decimal("0")
    return units.quantize(Decimal("0.001"))


def stay_hours_between(start: datetime, end: datetime) -> float:
    if end.tzinfo is None and start.tzinfo is not None:
        end = end.replace(tzinfo=start.tzinfo)
    if start.tzinfo is None and end.tzinfo is not None:
        start = start.replace(tzinfo=end.tzinfo)
    secs = (end - start).total_seconds()
    if secs <= 0:
        return 0.0
    return secs / 3600.0


def compute_arrival_charge_plan(
    db: Session,
    *,
    actual_at: datetime | None = None,
    scheduled_check_in: date,
    scheduled_check_out: date,
    nightly_rate: Decimal | str | float | None = None,
    confirm_after_midnight: bool = False,
    force_waive_previous_night: bool = False,
) -> ArrivalChargePlan:
    """يحسب أول ليلة محتسبة وسياسة الدخول المبكر/بعد منتصف الليل."""
    pol = checkin_stay_policy(db)
    local = to_local(actual_at)
    actual_date = local.date()
    ch, cm = _parse_hhmm(pol.business_day_cutoff, DEFAULT_BUSINESS_CUTOFF)
    ih, im = _parse_hhmm(pol.check_in_time)
    before_cutoff = (local.hour, local.minute) < (ch, cm)

    after_midnight = before_cutoff  # 00:00 ≤ t < cut-off = ليلة تشغيل سابقة
    first = scheduled_check_in
    reason = "NORMAL"
    early_same = False
    early_fee = Decimal("0")
    early_label = ""
    needs_confirm = False
    confirm_msg = ""
    block = False
    block_msg = ""

    if not pol.enabled:
        # سياسة معطّلة: لا ليلة سابقة ولا رسوم مبكر
        first = scheduled_check_in
        if actual_date < scheduled_check_in:
            first = actual_date
            reason = "EARLY_DATE"
        billing_in = first
        if billing_in >= scheduled_check_out:
            billing_in = scheduled_check_out - timedelta(days=1)
        nights = max(1, (scheduled_check_out - billing_in).days) if scheduled_check_out > billing_in else 0
        return ArrivalChargePlan(
            actual_at=local,
            first_chargeable_night=first,
            billing_check_in=billing_in,
            nights=nights,
            reason=reason,
            after_midnight_previous_night=False,
            early_checkin_same_day=False,
            early_fee_amount=Decimal("0"),
            early_fee_label="",
            needs_reception_confirm=False,
            confirm_message="",
            block_checkin=False,
            block_message="",
        )

    if after_midnight:
        prev = actual_date - timedelta(days=1)
        # ليلة محتسبة = الليلة السابقة (يوم التشغيل)
        first = prev
        reason = "AFTER_MIDNIGHT"
        needs_confirm = not confirm_after_midnight
        confirm_msg = (
            f"وصول بعد منتصف الليل ({local.strftime('%Y-%m-%d %H:%M')}).\n"
            f"سيُحتسب: ليلة {prev.isoformat()}\n"
            f"المغادرة المجدولة: {scheduled_check_out.isoformat()}\n"
            f"عدد الليالي: {max(0, (scheduled_check_out - prev).days)}\n"
            "سيتم الاحتفاظ بوقت الوصول الفعلي دون تغييره."
        )
        # إن كان الحجز مسبقاً لتلك الليلة — لا تغيّر البداية أبعد من prev
        if scheduled_check_in <= prev:
            first = scheduled_check_in
            reason = "AFTER_MIDNIGHT_PREBOOKED"
        if force_waive_previous_night:
            first = actual_date
            reason = "WAIVED_PREVIOUS_NIGHT"
            needs_confirm = False
    elif actual_date < scheduled_check_in:
        # وصول بتاريخ قبل الموعد (عدة أيام)
        first = actual_date
        reason = "EARLY_DATE"
    elif actual_date == scheduled_check_in and (local.hour, local.minute) < (ih, im):
        # دخول مبكر في نفس يوم الموعد قبل ساعة التسكين الرسمية
        early_same = True
        reason = "EARLY_CHECKIN"
        first = scheduled_check_in
        rate = Decimal(str(nightly_rate or 0)).quantize(Decimal("0.001"))
        ep = pol.early_checkin_policy
        if ep == "BLOCK":
            block = True
            block_msg = (
                f"سياسة الدخول المبكر: ممنوع التسكين قبل {pol.check_in_time}. "
                "يلزم صلاحية مدير لتجاوز السياسة."
            )
        elif ep == "FULL_NIGHT":
            # ليلة إضافية قبل الموعد
            first = scheduled_check_in - timedelta(days=1)
            early_label = "دخول مبكر — ليلة كاملة"
        elif ep == "HALF":
            early_fee = (rate * Decimal("0.5")).quantize(Decimal("0.001"))
            early_label = "دخول مبكر — نصف ليلة"
        elif ep == "FIXED":
            early_fee = pol.early_checkin_fixed
            early_label = "دخول مبكر — مبلغ ثابت"
        elif ep == "PERCENT":
            early_fee = (rate * pol.early_checkin_percent / Decimal("100")).quantize(
                Decimal("0.001")
            )
            early_label = f"دخول مبكر — {pol.early_checkin_percent}%"
        # FREE: لا رسوم ولا تغيير

    # billing check_in = min(first, scheduled) لحماية الإشغال، أو first إذا أبكر
    billing_in = first if first <= scheduled_check_in else scheduled_check_in
    if first < scheduled_check_in:
        billing_in = first
    else:
        billing_in = scheduled_check_in
        # after midnight walk-in without prebook on prev night: billing starts at first
        if after_midnight and not force_waive_previous_night:
            if scheduled_check_in > first:
                # حجز مجدول لليوم الحالي لكنه وصل قبل الـ cutoff → اربط بالليلة السابقة
                billing_in = first

    if billing_in >= scheduled_check_out:
        # سلامة
        billing_in = scheduled_check_out - timedelta(days=1)

    nights = max(0, (scheduled_check_out - billing_in).days)
    if nights <= 0 and scheduled_check_out >= billing_in:
        nights = 1

    return ArrivalChargePlan(
        actual_at=local,
        first_chargeable_night=first,
        billing_check_in=billing_in,
        nights=nights,
        reason=reason,
        after_midnight_previous_night=after_midnight and not force_waive_previous_night,
        early_checkin_same_day=early_same,
        early_fee_amount=early_fee,
        early_fee_label=early_label,
        needs_reception_confirm=needs_confirm,
        confirm_message=confirm_msg,
        block_checkin=block,
        block_message=block_msg,
    )


def billable_night_units(
    db: Session,
    *,
    check_in: date,
    check_out: date,
    arrival_at: datetime | None = None,
    departure_at: datetime | None = None,
    policy: CheckinStayPolicy | None = None,
    first_chargeable_night: date | None = None,
) -> Decimal:
    """
    وحدات الليالي القابلة للفوترة.
    - متعدد الأيام: checkout − first_chargeable (أو check_in).
    - نفس اليوم: عتبات الساعات (نصف/كامل).
    """
    pol = policy or checkin_stay_policy(db)
    start_d = first_chargeable_night or check_in
    calendar = max(0, (check_out - start_d).days)
    if not pol.enabled:
        return Decimal(
            max(1, calendar) if check_out > start_d else (1 if check_out == start_d else 0)
        )

    if calendar >= 1:
        return Decimal(calendar)

    start = arrival_at
    if start is None:
        start = pol.checkin_at(start_d)
    end = departure_at
    if end is None:
        from modules.hotel.late_checkout import late_checkout_policy

        cout = late_checkout_policy(db)
        end = cout.checkout_at(check_out if check_out >= start_d else start_d)
        if end <= start:
            end = start + timedelta(hours=pol.full_night_hours)

    hours = stay_hours_between(start, end)
    units = stay_units_from_hours(
        hours,
        full_night_hours=pol.full_night_hours,
        half_night_hours=pol.half_night_hours,
    )
    if units <= 0 and (arrival_at is not None or departure_at is not None):
        return Decimal("0")
    if units <= 0 and check_out >= start_d:
        return Decimal("0.5") if pol.half_night_hours > 0 else Decimal("1")
    return units


def settings_context(db: Session) -> dict:
    pol = checkin_stay_policy(db)
    return {
        "hotel_checkin_stay_policy_enabled": pol.enabled,
        "hotel_default_check_in_time": pol.check_in_time,
        "hotel_full_night_hours": str(pol.full_night_hours),
        "hotel_half_night_hours": str(pol.half_night_hours),
        "hotel_business_day_cutoff": pol.business_day_cutoff,
        "hotel_early_checkin_policy": pol.early_checkin_policy,
        "hotel_early_checkin_fixed_amount": str(pol.early_checkin_fixed),
        "hotel_early_checkin_percent": str(pol.early_checkin_percent),
    }


def save_checkin_stay_settings(
    db: Session,
    *,
    enabled: bool,
    check_in_time: str,
    full_night_hours: int | str,
    half_night_hours: int | str,
    business_day_cutoff: str = DEFAULT_BUSINESS_CUTOFF,
    early_checkin_policy: str = "FREE",
    early_checkin_fixed: str | Decimal = "0",
    early_checkin_percent: str | Decimal = "50",
) -> None:
    from modules.settings.service import invalidate_settings_cache

    try:
        full_h = int(str(full_night_hours or "12").strip())
    except ValueError:
        full_h = 12
    try:
        half_h = int(str(half_night_hours or "6").strip())
    except ValueError:
        half_h = 6
    full_h = max(1, min(48, full_h))
    half_h = max(0, min(full_h, half_h))
    set_setting(db, SETTING_ENABLED, "1" if enabled else "0")
    set_setting(db, SETTING_CHECKIN_TIME, normalize_checkin_time(check_in_time))
    set_setting(db, SETTING_FULL_NIGHT_HOURS, str(full_h))
    set_setting(db, SETTING_HALF_NIGHT_HOURS, str(half_h))
    set_setting(
        db, SETTING_BUSINESS_CUTOFF, normalize_cutoff_time(business_day_cutoff)
    )
    set_setting(db, SETTING_EARLY_POLICY, normalize_early_policy(early_checkin_policy))
    try:
        fixed = Decimal(str(early_checkin_fixed or 0)).quantize(Decimal("0.001"))
    except Exception:  # noqa: BLE001
        fixed = Decimal("0")
    try:
        pct = Decimal(str(early_checkin_percent or 50)).quantize(Decimal("0.001"))
    except Exception:  # noqa: BLE001
        pct = Decimal("50")
    set_setting(db, SETTING_EARLY_FIXED, str(max(Decimal("0"), fixed)))
    set_setting(db, SETTING_EARLY_PERCENT, str(max(Decimal("0"), min(Decimal("100"), pct))))
    invalidate_settings_cache()
