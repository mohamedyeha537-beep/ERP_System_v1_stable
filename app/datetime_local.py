"""تحويل عرض التواريخ من UTC (التخزين) إلى التوقيت المحلي للمتجر."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_STORE_TIMEZONE = "Africa/Tripoli"
# ليبيا UTC+2 بدون توقيت صيفي — احتياطي إن لم تتوفر قاعدة المناطق الزمنية (Windows بدون tzdata)
_FALLBACK_OFFSET = timezone(timedelta(hours=2))


@lru_cache(maxsize=1)
def store_timezone() -> ZoneInfo | timezone:
    try:
        return ZoneInfo(DEFAULT_STORE_TIMEZONE)
    except ZoneInfoNotFoundError:
        return _FALLBACK_OFFSET


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_local(value: datetime | date | None) -> datetime | date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return ensure_utc(value).astimezone(store_timezone())


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone(store_timezone())


def format_local_dt(value: datetime | date | str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if value is None:
        return ""
    if isinstance(value, date) and not isinstance(value, datetime):
        try:
            return value.strftime(fmt)
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text[:16] if len(text) >= 16 else text
        value = parsed
    local = to_local(value)
    if local is None:
        return ""
    try:
        return local.strftime(fmt)
    except (TypeError, ValueError):
        text = str(value).strip()
        return text[:16] if len(text) >= 16 else text


def local_day_start_utc(d: date | None = None) -> datetime:
    """بداية يوم تقويمي محلي (00:00) مُعبَّأة كـ UTC للاستعلام."""
    if d is None:
        d = now_local().date()
    local_start = datetime(d.year, d.month, d.day, tzinfo=store_timezone())
    return local_start.astimezone(timezone.utc)


def local_period_bounds(period: str) -> tuple[datetime, datetime]:
    """حدود الفترة (اليوم/الأسبوع/…) حسب التقويم المحلي."""
    now = now_local()
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif period == "week":
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end = start + timedelta(days=7)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
    elif period == "year":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1)
    else:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def parse_local_date_range(
    start_str: str | None, end_str: str | None
) -> tuple[datetime, datetime] | None:
    """يحلّل YYYY-MM-DD كتواريخ محلية؛ end حصري (+1 يوم إن كان تاريخاً فقط)."""
    if not start_str or not end_str:
        return None
    try:
        s_date = date.fromisoformat(start_str[:10])
        e_date = date.fromisoformat(end_str[:10])
    except ValueError:
        return None
    s = local_day_start_utc(s_date)
    if len(end_str.strip()) == 10:
        e = local_day_start_utc(e_date + timedelta(days=1))
    else:
        try:
            e_raw = datetime.fromisoformat(end_str)
        except ValueError:
            return None
        if e_raw.tzinfo is None:
            e_raw = e_raw.replace(tzinfo=store_timezone())
        e = e_raw.astimezone(timezone.utc)
    if e <= s:
        return None
    return s, e
