"""مسار التنقل — زر الرجوع والرئيسية حسب عنوان URL."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TrailItem:
    url: str
    label: str


@dataclass(frozen=True)
class PageTrail:
    show: bool
    back_url: str
    back_label: str
    breadcrumbs: tuple[TrailItem, ...]


_HOME = TrailItem("/", "الرئيسية")

# (regex on path without query, back_url, back_label, optional intermediate crumbs after home)
_RULES: list[tuple[re.Pattern[str], str, str, tuple[TrailItem, ...]]] = [
    # ── كatalog ──
    (re.compile(r"^/catalog/products/\d+/bom$"), "/catalog/products", "الأصناف", ()),
    (re.compile(r"^/catalog/products/\d+/edit$"), "/catalog/products", "الأصناف", ()),
    (re.compile(r"^/catalog/products/new$"), "/catalog/products", "الأصناف", ()),
    (re.compile(r"^/catalog/products$"), "/", "الرئيسية", (TrailItem("/catalog/products", "الأصناف"),)),
    (re.compile(r"^/catalog/categories$"), "/", "الرئيسية", (TrailItem("/catalog/categories", "الفئات"),)),
    (re.compile(r"^/catalog/import-export$"), "/catalog/products", "الأصناف", (TrailItem("/catalog/import-export", "استيراد / تصدير"),)),
    (re.compile(r"^/catalog/units$"), "/catalog/products", "الأصناف", (TrailItem("/catalog/units", "الوحدات"),)),
    # ── مخزون ──
    (re.compile(r"^/inventory/product/\d+/ledger$"), "/inventory", "المخزون", ()),
    (re.compile(r"^/inventory/low-stock/print$"), "/inventory", "المخزون", ()),
    (re.compile(r"^/inventory$"), "/", "الرئيسية", (TrailItem("/inventory", "المخزون"),)),
    # ── فندق ──
    (re.compile(r"^/admin/hotel/bookings/\d+$"), "/admin/hotel/bookings", "الحجوزات", (TrailItem("/admin/hotel/dashboard", "الشقق"),)),
    (re.compile(r"^/admin/hotel/bookings/new$"), "/admin/hotel/bookings", "الحجوزات", (TrailItem("/admin/hotel/dashboard", "الشقق"),)),
    (re.compile(r"^/admin/hotel/bookings/calendar$"), "/admin/hotel/bookings", "الحجوزات", (TrailItem("/admin/hotel/dashboard", "الشقق"),)),
    (re.compile(r"^/admin/hotel/bookings$"), "/admin/hotel/dashboard", "لوحة الشقق", (TrailItem("/admin/hotel/bookings", "الحجوزات"),)),
    (re.compile(r"^/admin/hotel/rooms$"), "/admin/hotel/dashboard", "لوحة الشقق", (TrailItem("/admin/hotel/rooms", "إعداد الشقق"),)),
    (re.compile(r"^/admin/hotel/room-types$"), "/admin/hotel/dashboard", "لوحة الشقق", (TrailItem("/admin/hotel/room-types", "أنواع الغرف"),)),
    (re.compile(r"^/admin/hotel/housekeeping$"), "/admin/hotel/dashboard", "لوحة الشقق", (TrailItem("/admin/hotel/housekeeping", "التنظيف والصيانة"),)),
    (re.compile(r"^/admin/hotel/reports$"), "/admin/hotel/dashboard", "لوحة الشقق", (TrailItem("/admin/hotel/reports", "تقارير الفندق"),)),
    (re.compile(r"^/admin/hotel/debts$"), "/admin/hotel/dashboard", "لوحة الشقق", (TrailItem("/admin/hotel/debts", "ذمم الحجوزات"),)),
    (re.compile(r"^/admin/hotel/dashboard$"), "/", "الرئيسية", (TrailItem("/admin/hotel/dashboard", "لوحة الشقق"),)),
    (re.compile(r"^/hotel/settle/room/\d+$"), "/hotel/settle", "تسوية الشقق", ()),
    (re.compile(r"^/hotel/settle$"), "/", "الرئيسية", (TrailItem("/hotel/settle", "تسوية الشقق"),)),
    # ── POS فرعي ──
    (re.compile(r"^/pos/shift/\d+/report$"), "/pos/shift", "إدارة الجلسة", (TrailItem("/pos", "نقطة البيع"),)),
    (re.compile(r"^/pos/shift/shortages$"), "/pos/shift", "إدارة الجلسة", (TrailItem("/pos", "نقطة البيع"),)),
    (re.compile(r"^/pos/shift/start$"), "/pos", "نقطة البيع", ()),
    (re.compile(r"^/pos/shift$"), "/pos", "نقطة البيع", (TrailItem("/pos/shift", "إدارة الجلسة"),)),
    (re.compile(r"^/pos/treasury/transfer$"), "/pos/treasury", "السابق", (TrailItem("/pos/treasury", "الخزينة"),)),
    (re.compile(r"^/pos/treasury$"), "/", "السابق", (TrailItem("/pos/treasury", "الخزينة"),)),
    (re.compile(r"^/pos/checkout$"), "/pos", "نقطة البيع", ()),
    (re.compile(r"^/pos/refund/\d+$"), "/pos?orders=1", "جميع الطلبات", (TrailItem("/pos", "نقطة البيع"),)),
    (re.compile(r"^/pos/refund$"), "/pos", "نقطة البيع", ()),
    (re.compile(r"^/pos/pin$"), "/pos", "نقطة البيع", ()),
    # ── تقارير ──
    (re.compile(r"^/admin/shifts$"), "/", "الرئيسية", (TrailItem("/admin/shifts", "الورديات"),)),
    (re.compile(r"^/reports/shifts/\d+$"), "/admin/shifts", "الورديات", (TrailItem("/reports/shifts", "تفاصيل الجلسة"),)),
    (re.compile(r"^/reports/shifts$"), "/reports", "التقارير", (TrailItem("/reports/shifts", "جلسات الكاشير"),)),
    (re.compile(r"^/reports/receivables/debtor/\d+$"), "/reports/receivables", "ذمم العملاء", (TrailItem("/reports", "التقارير"),)),
    (re.compile(r"^/reports/receivables/invoice/\d+$"), "/reports/receivables", "ذمم العملاء", (TrailItem("/reports", "التقارير"),)),
    (re.compile(r"^/reports/transactions$"), "/reports", "التقارير", (TrailItem("/reports/transactions", "سجل العمليات"),)),
    (re.compile(r"^/reports/hotel-shifts$"), "/reports", "التقارير", (TrailItem("/reports/hotel-shifts", "جلسات الفندق"),)),
    (re.compile(r"^/reports/hotel-collections$"), "/reports", "التقارير", (TrailItem("/reports/hotel-collections", "تحصيلات الفندق"),)),
    (re.compile(r"^/reports/hotel-bookings$"), "/reports", "التقارير", (TrailItem("/reports/hotel-bookings", "حجوزات الفندق"),)),
    (re.compile(r"^/reports/hotel-balances$"), "/reports", "التقارير", (TrailItem("/reports/hotel-balances", "ذمم الحجز"),)),
    (re.compile(r"^/reports/hotel-daily-close$"), "/reports", "التقارير", (TrailItem("/reports/hotel-daily-close", "الإقفال اليومي"),)),
    (re.compile(r"^/reports/gl-reconciliation$"), "/reports", "التقارير", (TrailItem("/reports/gl-reconciliation", "مطابقة GL"),)),
    (re.compile(r"^/reports/pos-daily-close$"), "/reports", "التقارير", (TrailItem("/reports/pos-daily-close", "إقفال المطعم"),)),
    (re.compile(r"^/reports/.+$"), "/reports", "التقارير", ()),
    (re.compile(r"^/reports$"), "/", "الرئيسية", (TrailItem("/reports", "التقارير"),)),
    # ── GL ──
    (re.compile(r"^/admin/gl/accounts/\d+/ledger$"), "/admin/gl/accounts", "شجرة الحسابات", (TrailItem("/admin/gl", "دفتر الأستاذ"),)),
    (re.compile(r"^/admin/gl/accounts/\d+$"), "/admin/gl/accounts", "شجرة الحسابات", (TrailItem("/admin/gl", "دفتر الأستاذ"),)),
    (re.compile(r"^/admin/gl/journal/manual$"), "/admin/gl/journal", "قيود اليومية", (TrailItem("/admin/gl", "دفتر الأستاذ"),)),
    (re.compile(r"^/admin/gl/.+$"), "/admin/gl", "دفتر الأستاذ", ()),
    (re.compile(r"^/admin/gl$"), "/", "الرئيسية", (TrailItem("/admin/gl", "دفتر الأستاذ"),)),
    # ── HR ──
    (re.compile(r"^/admin/employees/\d+/edit$"), "/admin/employees", "الموظفون", ()),
    (re.compile(r"^/admin/employees/new$"), "/admin/employees", "الموظفون", ()),
    (re.compile(r"^/admin/payroll/\d+$"), "/admin/payroll", "الرواتب", (TrailItem("/admin/employees", "الموظفون"),)),
    (re.compile(r"^/admin/employees$"), "/", "الرئيسية", (TrailItem("/admin/employees", "الموظفون"),)),
    (re.compile(r"^/admin/attendance$"), "/admin/employees", "الموظفون", (TrailItem("/admin/attendance", "الحضور"),)),
    (re.compile(r"^/admin/payroll$"), "/admin/employees", "الموظفون", (TrailItem("/admin/payroll", "الرواتب"),)),
    (re.compile(r"^/admin/advances$"), "/admin/employees", "الموظفون", (TrailItem("/admin/advances", "السلف"),)),
    (re.compile(r"^/admin/departments$"), "/admin/employees", "الموظفون", (TrailItem("/admin/departments", "الأقسام"),)),
    (re.compile(r"^/admin/work-shifts$"), "/admin/employees", "الموظفون", (TrailItem("/admin/work-shifts", "ورديات العمل"),)),
    # ── مشتريات / مصروفات / أصول ──
    (re.compile(r"^/admin/purchases/\d+$"), "/admin/purchases", "المشتريات", ()),
    (re.compile(r"^/admin/purchases/new$"), "/admin/purchases", "المشتريات", ()),
    (re.compile(r"^/admin/purchases$"), "/", "الرئيسية", (TrailItem("/admin/purchases", "المشتريات"),)),
    (re.compile(r"^/admin/expenses$"), "/", "الرئيسية", (TrailItem("/admin/expenses", "المصروفات"),)),
    (re.compile(r"^/admin/assets/\d+$"), "/admin/assets", "الأصول", ()),
    (re.compile(r"^/admin/assets$"), "/", "الرئيسية", (TrailItem("/admin/assets", "الأصول"),)),
    (re.compile(r"^/admin/consumables$"), "/", "الرئيسية", (TrailItem("/admin/consumables", "الاستهلاكات"),)),
    # ── عملاء / ولاء ──
    (re.compile(r"^/admin/customers/\d+$"), "/admin/customers", "العملاء", ()),
    (re.compile(r"^/admin/customers$"), "/", "الرئيسية", (TrailItem("/admin/customers", "العملاء"),)),
    (re.compile(r"^/admin/loyalty$"), "/admin/customers", "العملاء", (TrailItem("/admin/loyalty", "نقاط الولاء"),)),
    # ── مراسلات / إشعارات ──
    (re.compile(r"^/admin/messaging/inbox/\d+$"), "/admin/messaging/inbox", "صندوق الوارد", (TrailItem("/admin/notifications", "محرك الإشعارات"),)),
    (re.compile(r"^/admin/messaging/.+$"), "/admin/notifications/delivery", "الإرسال", (TrailItem("/admin/notifications", "محرك الإشعارات"),)),
    (re.compile(r"^/admin/messaging$"), "/", "الرئيسية", (TrailItem("/admin/notifications", "محرك الإشعارات"),)),
    (re.compile(r"^/admin/notifications/.+$"), "/admin/notifications", "الإشعارات", ()),
    (re.compile(r"^/admin/notifications$"), "/", "الرئيسية", (TrailItem("/admin/notifications", "الإشعارات"),)),
    # ── إعدادات / إدارة ──
    (re.compile(r"^/admin/payment-methods$"), "/", "الرئيسية", (TrailItem("/admin/payment-methods", "الحسابات المالية"),)),
    (re.compile(r"^/admin/settings$"), "/", "الرئيسية", (TrailItem("/admin/settings", "الإعدادات"),)),
    (re.compile(r"^/admin/branding$"), "/admin/settings", "الإعدادات", (TrailItem("/admin/branding", "الهوية البصرية"),)),
    (re.compile(r"^/admin/roles/\d+$"), "/admin/roles", "الأدوار", (TrailItem("/admin/settings", "الإعدادات"),)),
    (re.compile(r"^/admin/roles$"), "/admin/settings", "الإعدادات", (TrailItem("/admin/roles", "الأدوار"),)),
    (re.compile(r"^/admin/users$"), "/admin/settings", "الإعدادات", (TrailItem("/admin/users", "المستخدمون"),)),
    (re.compile(r"^/admin/tables$"), "/", "الرئيسية", (TrailItem("/admin/tables", "الطاولات"),)),
    (re.compile(r"^/admin/kds$"), "/", "الرئيسية", (TrailItem("/admin/kds", "شاشة المطبخ"),)),
    (re.compile(r"^/admin/kitchen-departments$"), "/admin/kds", "شاشة المطبخ", ()),
    (re.compile(r"^/admin/backup$"), "/admin/settings", "الإعدادات", (TrailItem("/admin/backup", "النسخ الاحتياطي"),)),
    (re.compile(r"^/admin/printing$"), "/admin/settings", "الإعدادات", (TrailItem("/admin/printing", "الطباعة"),)),
    (re.compile(r"^/admin/warehouses$"), "/inventory", "المخزون", (TrailItem("/admin/warehouses", "المخازن"),)),
    (re.compile(r"^/admin/delivery-zones$"), "/reports/delivery", "تقرير التوصيل", ()),
    # ── مبيعات / استرداد ──
    (re.compile(r"^/refunds/sale/\d+$"), "/refunds", "الاسترداد", ()),
    (re.compile(r"^/refunds$"), "/", "الرئيسية", (TrailItem("/refunds", "استرداد المبيعات"),)),
    (re.compile(r"^/sales/invoices/\d+/edit$"), "/sales/invoices", "تعديل الفواتير", ()),
    (re.compile(r"^/sales/invoices$"), "/", "الرئيسية", (TrailItem("/sales/invoices", "تعديل الفواتير"),)),
    # ── بوابة النزلاء ──
    (re.compile(r"^/stay/book$"), "/stay", "بحث الإقامة", ()),
    (re.compile(r"^/stay/my/\d+$"), "/stay", "بحث الإقامة", ()),
    (re.compile(r"^/stay$"), "/", "الرئيسية", (TrailItem("/stay", "بوابة الإقامة"),)),
]

_HIDE_PREFIXES = (
    "/static/",
    "/uploads/",
    "/auth/login",
    "/receipt",
    "/kitchen",
    "/api/",
)

_HIDE_EXACT = frozenset(
    {
        "/",
        "/pos",
        "/shop",
        "/guest-chat",
    }
)


def _normalize_path(raw: str) -> str:
    path = (raw or "/").split("?")[0].split("#")[0].rstrip("/")
    return path or "/"


def build_page_trail(
    path: str,
    *,
    back_url: str | None = None,
    back_label: str | None = None,
    hide: bool = False,
) -> PageTrail:
    if hide:
        return PageTrail(show=False, back_url="/", back_label="الرئيسية", breadcrumbs=(_HOME,))

    p = _normalize_path(path)
    if p in _HIDE_EXACT:
        return PageTrail(show=False, back_url="/", back_label="الرئيسية", breadcrumbs=(_HOME,))
    if any(p.startswith(prefix) for prefix in _HIDE_PREFIXES):
        return PageTrail(show=False, back_url="/", back_label="الرئيسية", breadcrumbs=(_HOME,))

    for pattern, default_back_url, default_back_label, extras in _RULES:
        if pattern.match(p):
            crumbs: list[TrailItem] = [_HOME, *extras]
            bu = back_url or default_back_url
            bl = back_label or default_back_label
            return PageTrail(
                show=True,
                back_url=bu,
                back_label=bl,
                breadcrumbs=tuple(crumbs),
            )

    # fallback: any /admin/* → settings or home; depth-based parent
    if p.startswith("/admin/"):
        section = p.split("/")[2] if len(p.split("/")) > 2 else "admin"
        label_map = {
            "hotel": ("لوحة الشقق", "/admin/hotel/dashboard"),
            "settings": ("الإعدادات", "/admin/settings"),
        }
        lbl, url = label_map.get(section, ("الإعدادات", "/admin/settings"))
        return PageTrail(
            show=True,
            back_url=back_url or url,
            back_label=back_label or lbl,
            breadcrumbs=(_HOME, TrailItem(url, lbl)),
        )

    if p.count("/") >= 2:
        parent = p.rsplit("/", 1)[0] or "/"
        return PageTrail(
            show=True,
            back_url=back_url or parent,
            back_label=back_label or "← السابق",
            breadcrumbs=(_HOME,),
        )

    if p != "/":
        return PageTrail(
            show=True,
            back_url=back_url or "/",
            back_label=back_label or "الرئيسية",
            breadcrumbs=(_HOME,),
        )

    return PageTrail(show=False, back_url="/", back_label="الرئيسية", breadcrumbs=(_HOME,))
