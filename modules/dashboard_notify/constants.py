"""مفاتيح أقسام لوحة التحكم وربطها بمسارات الواجهة."""

from __future__ import annotations

# مفاتيح الأقسام — تُستخدم في السجل وعند مسح الإشعار
POS = "pos"
REPORTS = "reports"
INVENTORY = "inventory"
CATALOG_PRODUCTS = "catalog_products"
CATALOG_CATEGORIES = "catalog_categories"
TABLES = "tables"
KDS = "kds"
HR_EMPLOYEES = "hr_employees"
HR_ATTENDANCE = "hr_attendance"
HR_PAYROLL = "hr_payroll"
HR_ADVANCES = "hr_advances"
HOTEL_ROOMS = "hotel_rooms"
HOTEL_SETTLE = "hotel_settle"
CUSTOMERS = "customers"
LOYALTY = "loyalty"
PURCHASES = "purchases"
EXPENSES = "expenses"
ASSETS = "assets"
RECURRING_COSTS = "recurring_costs"
PAYMENT_METHODS = "payment_methods"
UNITS = "units"
USERS = "users"
ROLES = "roles"
SETTINGS = "settings"
ALERTS = "alerts"
BRANDING = "branding"
BACKUP = "backup"
REFUNDS = "refunds"
INVOICE_EDIT = "invoice_edit"
RECEIVABLES = "receivables"
PAYABLES = "payables"
PRINTING = "printing"
WAREHOUSES = "warehouses"
DELIVERY = "delivery"
MESSAGING_INBOX = "messaging_inbox"
MESSAGING_CHAT_ORDERS = "messaging_chat_orders"

# إشعار إعلامي — يُمسح عند فتح القسم (لكل مستخدم على حدة)
INFO_ONLY_SECTIONS: frozenset[str] = frozenset(
    {
        INVENTORY,
        REFUNDS,
        INVOICE_EDIT,
        REPORTS,
        CATALOG_PRODUCTS,
        CATALOG_CATEGORIES,
        TABLES,
        HR_EMPLOYEES,
        HR_ATTENDANCE,
        HR_PAYROLL,
        HR_ADVANCES,
        HOTEL_ROOMS,
        CUSTOMERS,
        LOYALTY,
        EXPENSES,
        RECURRING_COSTS,
        PAYMENT_METHODS,
        UNITS,
        USERS,
        ROLES,
        SETTINGS,
        ALERTS,
        BRANDING,
        BACKUP,
        WAREHOUSES,
        DELIVERY,
        RECEIVABLES,
        PAYABLES,
        POS,
    }
)

# أطول بادئة أولاً عند المطابقة
PATH_TO_SECTION: list[tuple[str, str]] = [
    ("/sales/invoices", INVOICE_EDIT),
    ("/refunds", REFUNDS),
    ("/reports/receivables", RECEIVABLES),
    ("/reports/payables", PAYABLES),
    ("/reports/delivery", DELIVERY),
    ("/reports", REPORTS),
    ("/hotel/settle", HOTEL_SETTLE),
    ("/admin/hotel/bookings", HOTEL_ROOMS),
    ("/admin/hotel/dashboard", HOTEL_ROOMS),
    ("/admin/hotel/rooms", HOTEL_ROOMS),
    ("/admin/hotel", HOTEL_ROOMS),
    ("/admin/kds", KDS),
    ("/admin/kitchen", KDS),
    ("/admin/printing", PRINTING),
    ("/admin/employees", HR_EMPLOYEES),
    ("/admin/attendance", HR_ATTENDANCE),
    ("/admin/payroll", HR_PAYROLL),
    ("/admin/advances", HR_ADVANCES),
    ("/admin/customers", CUSTOMERS),
    ("/admin/loyalty", LOYALTY),
    ("/admin/purchases", PURCHASES),
    ("/admin/expenses", EXPENSES),
    ("/admin/assets", ASSETS),
    ("/admin/recurring-costs", RECURRING_COSTS),
    ("/admin/payment-methods", PAYMENT_METHODS),
    ("/admin/users", USERS),
    ("/admin/roles", ROLES),
    ("/admin/settings", SETTINGS),
    ("/admin/alerts", ALERTS),
    ("/admin/messaging/chat-orders", MESSAGING_CHAT_ORDERS),
    ("/admin/messaging/inbox", MESSAGING_INBOX),
    ("/admin/notifications", MESSAGING_INBOX),
    ("/admin/branding", BRANDING),
    ("/admin/backup", BACKUP),
    ("/admin/tables", TABLES),
    ("/admin/warehouses", WAREHOUSES),
    ("/catalog/import", CATALOG_PRODUCTS),
    ("/catalog/products", CATALOG_PRODUCTS),
    ("/catalog/categories", CATALOG_CATEGORIES),
    ("/catalog/units", UNITS),
    ("/inventory", INVENTORY),
    ("/pos/treasury", PAYMENT_METHODS),
    ("/pos/shift", POS),
    ("/pos", POS),
]
