"""Permission codes — stable API for RBAC."""

SALES_CREATE = "sales:create"
CATALOG_WRITE = "catalog:write"
INVENTORY_VIEW = "inventory:view"
INVENTORY_ADJUST = "inventory:adjust"
REPORTS_VIEW = "reports:view"
ADMIN_ROLES = "admin:roles"
ADMIN_USERS = "admin:users"
ADMIN_SETTINGS = "admin:settings"
POS_PRINT_CHOOSE_SIZE = "pos:print_choose_size"
PAYMENTS_MANAGE = "payments:manage"
PURCHASES_MANAGE = "purchases:manage"
TABLES_MANAGE = "tables:manage"
KDS_VIEW = "kds:view"
BACKUP_MANAGE = "backup:manage"
HR_VIEW = "hr:view"
HR_MANAGE = "hr:manage"
HR_ATTENDANCE = "hr:attendance"
HOTEL_ROOMS_MANAGE = "hotel:rooms:manage"
HOTEL_CHARGE = "hotel:charge"
HOTEL_SETTLE = "hotel:settle"
CUSTOMERS_VIEW = "customers:view"
CUSTOMERS_MANAGE = "customers:manage"
LOYALTY_REDEEM = "loyalty:redeem"
BRANDING_MANAGE = "branding:manage"
SALES_REFUND = "sales:refund"
SALES_REFUND_OVERRIDE = "sales:refund_override"
DELIVERY_MANAGE = "delivery:manage"
WAREHOUSES_MANAGE = "warehouses:manage"

ALL_PERMISSIONS: list[tuple[str, str]] = [
    (SALES_CREATE, "إنشاء مبيعات (كاشير)"),
    (CATALOG_WRITE, "إدارة الأصناف ووصفات التركيب"),
    (INVENTORY_VIEW, "عرض المخزون"),
    (INVENTORY_ADJUST, "تعديل المخزون والتسويات"),
    (REPORTS_VIEW, "عرض التقارير"),
    (ADMIN_ROLES, "إدارة الأدوار والصلاحيات"),
    (ADMIN_USERS, "إدارة المستخدمين"),
    (ADMIN_SETTINGS, "إدارة إعدادات النظام (الطباعة، اسم المتجر)"),
    (POS_PRINT_CHOOSE_SIZE, "اختيار مقاس الطباعة وقت إصدار الفاتورة"),
    (PAYMENTS_MANAGE, "إدارة أساليب الدفع (مصارف، خدمات، كاش)"),
    (PURCHASES_MANAGE, "تسجيل المشتريات والمصروفات"),
    (TABLES_MANAGE, "إدارة طاولات المطعم/المقهى"),
    (KDS_VIEW, "عرض شاشة المطبخ (KDS) والتعامل مع الطلبات"),
    (BACKUP_MANAGE, "النسخ الاحتياطي والاستعادة وتصفير قاعدة البيانات (خطر)"),
    (HR_VIEW, "عرض الموظفين وسجلات الحضور والرواتب"),
    (HR_MANAGE, "إدارة الموظفين واعتماد ودفع الرواتب"),
    (HR_ATTENDANCE, "تسجيل الحضور والانصراف"),
    (HOTEL_ROOMS_MANAGE, "إدارة غرف الفندق (للأدمن)"),
    (HOTEL_CHARGE, "قيد فاتورة على حساب غرفة فندق (للكاشير)"),
    (HOTEL_SETTLE, "تسوية حسابات غرف الفندق وتسجيل الدفع (للاستقبال)"),
    (CUSTOMERS_VIEW, "عرض قاعدة العملاء وأرصدة نقاط الولاء"),
    (CUSTOMERS_MANAGE, "إدارة العملاء وتعديل بياناتهم وضبط نقاطهم"),
    (LOYALTY_REDEEM, "استخدام نقاط الولاء كخصم على الفاتورة"),
    (BRANDING_MANAGE, "تخصيص الهويّة البصرية (الشعار والألوان)"),
    (SALES_REFUND, "تنفيذ مرتجعات المبيعات"),
    (SALES_REFUND_OVERRIDE, "اعتماد مرتجع بوسيلة رد مختلفة مع قيد تسوية"),
    (DELIVERY_MANAGE, "إدارة مناطق التوصيل ورسومها"),
    (WAREHOUSES_MANAGE, "إدارة المخازن الفرعية والتحويل من الرئيسي"),
]
