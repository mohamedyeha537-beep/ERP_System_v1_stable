"""ساعات عمل طلبات المتجر الأونلاين (/shop)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

from sqlalchemy.orm import Session

from app.datetime_local import now_local
from modules.settings.service import get_bool, get_setting


WEEKDAY_AR = (
    "الاثنين",
    "الثلاثاء",
    "الأربعاء",
    "الخميس",
    "الجمعة",
    "السبت",
    "الأحد",
)


@dataclass(frozen=True)
class ShopHoursStatus:
    enforce: bool
    is_open: bool
    open_time: str
    close_time: str
    hours_label: str
    message: str
    closed_reason: str  # "" | "hours" | "day" | "misconfigured"


def _parse_hhmm(raw: str | None, default: str) -> time | None:
    text = (raw or "").strip() or default
    parts = text.replace(".", ":").split(":")
    if len(parts) < 2:
        return None
    try:
        h = int(parts[0])
        m = int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return time(h, m)
    except (TypeError, ValueError):
        return None


def _fmt_time(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def _parse_open_weekdays(raw: str | None) -> set[int]:
    """أيام العمل: 0=الاثنين … 6=الأحد. فارغ = كل الأيام."""
    text = (raw or "").strip()
    if not text:
        return set(range(7))
    out: set[int] = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        try:
            d = int(part)
        except ValueError:
            continue
        if 0 <= d <= 6:
            out.add(d)
    return out or set(range(7))


def _within_window(now_t: time, open_t: time, close_t: time) -> bool:
    """يدعم الفترة الليلية (مثل 22:00 → 02:00)."""
    if open_t == close_t:
        # نفس الوقت = مفتوح 24 ساعة
        return True
    if open_t < close_t:
        return open_t <= now_t < close_t
    return now_t >= open_t or now_t < close_t


def _default_closed_message(*, open_s: str, close_s: str, days_label: str) -> str:
    return (
        f"نعتذر، الطلبات الأونلاين غير متاحة الآن. "
        f"مواعيد العمل الحالية: من {open_s} إلى {close_s}"
        + (f" ({days_label})" if days_label else "")
        + ". نرحّب بكم في الوقت المحدد."
    )


def shop_hours_status(db: Session, *, at: datetime | None = None) -> ShopHoursStatus:
    enforce = get_bool(db, "shop_hours_enforce", False)
    open_raw = (get_setting(db, "shop_hours_open", "10:00") or "10:00").strip()
    close_raw = (get_setting(db, "shop_hours_close", "23:00") or "23:00").strip()
    days_raw = get_setting(db, "shop_hours_open_weekdays", "") or ""
    custom_msg = (get_setting(db, "shop_hours_closed_message", "") or "").strip()

    open_t = _parse_hhmm(open_raw, "10:00")
    close_t = _parse_hhmm(close_raw, "23:00")
    open_s = _fmt_time(open_t) if open_t else open_raw or "—"
    close_s = _fmt_time(close_t) if close_t else close_raw or "—"
    open_days = _parse_open_weekdays(days_raw)

    if open_days == set(range(7)):
        days_label = "يومياً"
    else:
        days_label = "، ".join(WEEKDAY_AR[d] for d in sorted(open_days))

    hours_label = f"{open_s} — {close_s}"
    if days_label:
        hours_label = f"{hours_label} · {days_label}"

    def _msg(reason_hint: str = "") -> str:
        if custom_msg:
            return (
                custom_msg.replace("{open}", open_s)
                .replace("{close}", close_s)
                .replace("{hours}", f"{open_s} — {close_s}")
                .replace("{days}", days_label)
            )
        base = _default_closed_message(
            open_s=open_s, close_s=close_s, days_label=days_label
        )
        if reason_hint:
            return f"{reason_hint} {base}"
        return base

    if not enforce:
        return ShopHoursStatus(
            enforce=False,
            is_open=True,
            open_time=open_s,
            close_time=close_s,
            hours_label=hours_label,
            message="",
            closed_reason="",
        )

    if open_t is None or close_t is None:
        return ShopHoursStatus(
            enforce=True,
            is_open=False,
            open_time=open_s,
            close_time=close_s,
            hours_label=hours_label,
            message=_msg("إعدادات ساعات العمل غير مكتملة."),
            closed_reason="misconfigured",
        )

    now = at or now_local()
    if now.weekday() not in open_days:
        return ShopHoursStatus(
            enforce=True,
            is_open=False,
            open_time=open_s,
            close_time=close_s,
            hours_label=hours_label,
            message=_msg(f"المطعم مغلق يوم {WEEKDAY_AR[now.weekday()]}."),
            closed_reason="day",
        )

    if not _within_window(now.time(), open_t, close_t):
        return ShopHoursStatus(
            enforce=True,
            is_open=False,
            open_time=open_s,
            close_time=close_s,
            hours_label=hours_label,
            message=_msg(),
            closed_reason="hours",
        )

    return ShopHoursStatus(
        enforce=True,
        is_open=True,
        open_time=open_s,
        close_time=close_s,
        hours_label=hours_label,
        message="",
        closed_reason="",
    )


def shop_accepting_orders(db: Session) -> bool:
    return shop_hours_status(db).is_open


def assert_shop_accepting_orders(db: Session) -> None:
    from modules.shop.service import ShopError

    st = shop_hours_status(db)
    if not st.is_open:
        raise ShopError(st.message or "الطلبات الأونلاين غير متاحة الآن.")


def shop_hours_public_dict(db: Session) -> dict:
    st = shop_hours_status(db)
    return {
        "enforce": st.enforce,
        "is_open": st.is_open,
        "open_time": st.open_time,
        "close_time": st.close_time,
        "hours_label": st.hours_label,
        "message": st.message,
        "closed_reason": st.closed_reason,
    }
