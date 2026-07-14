"""إشعارات لوحة التحكم — عدّ التغييرات غير المقروءة على كروت الروابط السريعة."""

from modules.dashboard_notify.service import (
    badge_counts,
    record_activity,
    resolve_activity,
)

__all__ = ["record_activity", "resolve_activity", "badge_counts"]
