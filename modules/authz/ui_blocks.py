"""تعريف أجزاء الواجهة القابلة للإظهار/الإخفاء لكل مستخدم."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable

from modules.authz.models import User


@dataclass(frozen=True)
class UiBlock:
    id: str
    label_ar: str
    group: str
    hint: str = ""


UI_BLOCK_GROUPS: tuple[tuple[str, str], ...] = (
    ("dashboard", "أقسام لوحة التحكم"),
    ("quick", "روابط سريعة"),
    ("nav", "القائمة الجانبية والترويسة"),
)

UI_BLOCKS: tuple[UiBlock, ...] = (
    # —— لوحة التحكم: الأقسام القابلة للطي ——
    UiBlock("ui_dash_revenue", "إيرادات / تحصيلات", "dashboard", "اليوم والشهر"),
    UiBlock("ui_dash_treasury", "الخزينة والذمم", "dashboard"),
    UiBlock("ui_dash_inventory", "المخزون والكتالوج", "dashboard"),
    UiBlock("ui_dash_activity", "آخر الفواتير والأكثر مبيعاً", "dashboard"),
    UiBlock("ui_dash_wallets", "رصيد المحافظ", "dashboard"),
    UiBlock("ui_dash_low_stock", "أصناف قاربت على النفاد", "dashboard"),
    UiBlock("ui_dash_break_even", "تحليل التعادل اليومي", "dashboard"),
    UiBlock("ui_dash_income", "بيان الدخل الشهري", "dashboard"),
    UiBlock("ui_dash_cashflow", "حركة الشهر النقدية", "dashboard"),
    # —— روابط سريعة ——
    UiBlock("ui_ql_bar", "شريط الروابط السريعة بالكامل", "quick"),
    UiBlock("ui_pos", "نقطة البيع", "quick"),
    UiBlock("ui_reports", "مركز التقارير", "quick"),
    UiBlock("ui_inventory", "المخزون", "quick"),
    UiBlock("ui_catalog", "الأصناف", "quick"),
    UiBlock("ui_categories", "الفئات", "quick"),
    UiBlock("ui_tables", "الطاولات", "quick"),
    UiBlock("ui_kds", "شاشة المطبخ", "quick"),
    UiBlock("ui_hr", "الموظفون والحضور والرواتب", "quick"),
    UiBlock("ui_hotel", "حجوزات الشقق", "quick"),
    UiBlock("ui_hotel_settle", "تسوية حسابات الغرف", "quick"),
    UiBlock("ui_customers", "العملاء", "quick"),
    UiBlock("ui_loyalty", "إعدادات الولاء", "quick"),
    UiBlock("ui_purchases", "فواتير الشراء", "quick"),
    UiBlock("ui_expenses", "المصروفات", "quick"),
    UiBlock("ui_assets", "الأصول/الأدوات", "quick"),
    UiBlock("ui_recurring", "التكاليف الشهرية", "quick"),
    UiBlock("ui_payment_methods", "أساليب الدفع", "quick"),
    UiBlock("ui_gl", "شجرة الحسابات / GL", "quick"),
    UiBlock("ui_finance_hub", "مركز الربح", "quick"),
    UiBlock("ui_messaging", "الإشعارات والمراسلات", "quick"),
    UiBlock("ui_settings", "الإعدادات", "quick"),
    UiBlock("ui_users", "المستخدمون", "quick"),
    UiBlock("ui_roles", "الأدوار", "quick"),
    UiBlock("ui_branding", "الهوية البصرية", "quick"),
    UiBlock("ui_backup", "نسخ احتياطي", "quick"),
    # —— القائمة الجانبية ——
    UiBlock("ui_nav_home", "الرئيسية", "nav"),
    UiBlock("ui_nav_hotel_group", "مجموعة الشقق والفندق", "nav"),
    UiBlock("ui_nav_sales_group", "مجموعة المبيعات والمطعم", "nav"),
    UiBlock("ui_nav_stock_group", "مجموعة المخزون والأصناف", "nav"),
    UiBlock("ui_nav_hr_group", "مجموعة الموظفين", "nav"),
    UiBlock("ui_nav_customers_group", "مجموعة العملاء", "nav"),
    UiBlock("ui_nav_finance_group", "مجموعة المالية", "nav"),
    UiBlock("ui_nav_messaging_group", "مجموعة الإشعارات", "nav"),
    UiBlock("ui_nav_system_group", "مجموعة النظام والإعدادات", "nav"),
    # —— شريط تنقل الفندق ——
    UiBlock("ui_hotel_nav_dashboard", "لوحة الشقق", "nav", "شريط الفندق العلوي"),
    UiBlock("ui_hotel_nav_bookings", "الحجوزات", "nav", "شريط الفندق"),
    UiBlock("ui_hotel_nav_quotations", "عروض الشركات", "nav", "شريط الفندق"),
    UiBlock("ui_hotel_nav_calendar", "تقويم الحجوزات", "nav", "شريط الفندق"),
    UiBlock("ui_hotel_nav_rooms", "إعداد الشقق", "nav", "شريط الفندق"),
    UiBlock("ui_hotel_nav_room_types", "أنواع الغرف", "nav", "شريط الفندق"),
    UiBlock("ui_hotel_nav_housekeeping", "التنظيف والصيانة", "nav", "شريط الفندق"),
    UiBlock("ui_hotel_nav_shift", "جلسة الفندق", "nav", "شريط الفندق"),
    UiBlock("ui_hotel_nav_reports", "تقارير الفندق", "nav", "شريط الفندق"),
    UiBlock("ui_hotel_nav_settings", "إعدادات الحجز", "nav", "شريط الفندق"),
    UiBlock("ui_hotel_nav_settle", "تسوية حسابات الغرف", "nav", "شريط الفندق"),
    UiBlock("ui_hotel_nav_debts", "ذمم الحجوزات", "nav", "شريط الفندق"),
)

_UI_BLOCK_MAP = {b.id: b for b in UI_BLOCKS}
_ALL_BLOCK_IDS = frozenset(_UI_BLOCK_MAP)


def all_ui_block_ids() -> frozenset[str]:
    return _ALL_BLOCK_IDS


def ui_blocks_by_group() -> dict[str, list[UiBlock]]:
    groups = {g: [] for g, _ in UI_BLOCK_GROUPS}
    for block in UI_BLOCKS:
        groups.setdefault(block.group, []).append(block)
    return groups


def parse_ui_hidden(raw: str | None) -> set[str]:
    if not raw or not str(raw).strip():
        return set()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(x) for x in data if str(x) in _ALL_BLOCK_IDS}


def serialize_ui_hidden(hidden: Iterable[str]) -> str:
    clean = sorted({str(x) for x in hidden if str(x) in _ALL_BLOCK_IDS})
    return json.dumps(clean, ensure_ascii=False)


def compute_ui_hidden_from_shown(shown_ids: Iterable[str]) -> str:
    shown = {str(x) for x in shown_ids if str(x) in _ALL_BLOCK_IDS}
    return serialize_ui_hidden(_ALL_BLOCK_IDS - shown)


def user_shows_ui_block(user: User | None, block_id: str) -> bool:
    if user is None or block_id not in _ALL_BLOCK_IDS:
        return True
    hidden = parse_ui_hidden(getattr(user, "ui_hidden", None))
    return block_id not in hidden


def user_ui_hidden_set(user: User | None) -> set[str]:
    if user is None:
        return set()
    return parse_ui_hidden(getattr(user, "ui_hidden", None))
