"""Permission codes — stable API for RBAC."""

ADMIN_ROLE_NAME_AR = "مدير النظام"
TREASURY_CLERK_ROLE_NAME_AR = "أمين خزينة"
SUPPORT_AGENT_ROLE_NAME_AR = "دعم فني"
SALES_AGENT_ROLE_NAME_AR = "مبيعات"
CATALOG_PURCHASES_ROLE_NAME_AR = "إدخال أصناف ومشتريات"
HOTEL_BOOKINGS_ONLY_ROLE_NAME_AR = "إدارة حجوزات الفندق فقط"

SALES_CREATE = "sales:create"
CATALOG_WRITE = "catalog:write"
INVENTORY_VIEW = "inventory:view"
INVENTORY_ADJUST = "inventory:adjust"
INVENTORY_RECEIVE = "inventory:receive"
REPORTS_VIEW = "reports:view"
ADMIN_ROLES = "admin:roles"
ADMIN_USERS = "admin:users"
ADMIN_SETTINGS = "admin:settings"
POS_PRINT_CHOOSE_SIZE = "pos:print_choose_size"
PAYMENTS_MANAGE = "payments:manage"
TREASURY_HANDOFF_REVOKE = "treasury:handoff_revoke"
PURCHASES_MANAGE = "purchases:manage"
PURCHASE_INVOICES_MANAGE = "purchases:invoices"
TABLES_MANAGE = "tables:manage"
KDS_VIEW = "kds:view"
BACKUP_MANAGE = "backup:manage"
HR_VIEW = "hr:view"
HR_MANAGE = "hr:manage"
HR_ATTENDANCE = "hr:attendance"
HOTEL_ROOMS_MANAGE = "hotel:rooms:manage"
HOTEL_CHARGE = "hotel:charge"
HOTEL_SETTLE = "hotel:settle"
HOTEL_BOOKING_VIEW = "hotel:booking:view"
HOTEL_BOOKING_CREATE = "hotel:booking:create"
HOTEL_BOOKING_MANAGE = "hotel:booking:manage"
HOTEL_BOOKING_CHECKIN = "hotel:booking:checkin"
HOTEL_BOOKING_CHECKOUT = "hotel:booking:checkout"
HOTEL_BOOKING_CHECKOUT_BALANCE = "hotel:booking:checkout_with_balance"
HOTEL_HOUSEKEEPING = "hotel:housekeeping"
HOTEL_FINANCE_CLOSE = "hotel:finance:close_day"
CUSTOMERS_VIEW = "customers:view"
CUSTOMERS_MANAGE = "customers:manage"
LOYALTY_REDEEM = "loyalty:redeem"
BRANDING_MANAGE = "branding:manage"
SALES_REFUND = "sales:refund"
SALES_REFUND_OVERRIDE = "sales:refund_override"
SALES_EDIT_INVOICE = "sales:edit_invoice"
SALES_PRINT_RECEIPT = "sales:print_receipt"
SALES_CORRECT_PAYMENT = "sales:correct_payment"
SALES_VOID_AFTER_KITCHEN = "sales:void_after_kitchen"
SALES_VOID_SUPERVISOR = "sales:void_supervisor"
ASSETS_EDIT_INVOICE = "assets:edit_invoice"
DELIVERY_MANAGE = "delivery:manage"
WAREHOUSES_MANAGE = "warehouses:manage"
MESSAGING_MANAGE = "messaging:manage"
MESSAGING_SEND = "messaging:send"
MESSAGING_VIEW = "messaging:view"
GL_MANAGE = "gl:manage"

ALL_PERMISSIONS: list[tuple[str, str]] = [
    (SALES_CREATE, "إنشاء مبيعات (كاشير)"),
    (CATALOG_WRITE, "إدارة الأصناف ووصفات التركيب"),
    (INVENTORY_VIEW, "عرض المخزون"),
    (INVENTORY_ADJUST, "تعديل المخزون والتسويات"),
    (INVENTORY_RECEIVE, "مصادقة استلام الصرف للمخزن الفرعي"),
    (REPORTS_VIEW, "عرض التقارير"),
    (ADMIN_ROLES, "إدارة الأدوار والصلاحيات"),
    (ADMIN_USERS, "إدارة المستخدمين"),
    (ADMIN_SETTINGS, "إدارة إعدادات النظام (الطباعة، اسم المتجر)"),
    (POS_PRINT_CHOOSE_SIZE, "اختيار مقاس الطباعة وقت إصدار الفاتورة"),
    (PAYMENTS_MANAGE, "إدارة أساليب الدفع (مصارف، خدمات، كاش)"),
    (TREASURY_HANDOFF_REVOKE, "إلغاء اعتماد خزينة الجلسة وإعادة الفتح للتعديل"),
    (PURCHASES_MANAGE, "تسجيل المصروفات وإدارة مالية الشراء"),
    (PURCHASE_INVOICES_MANAGE, "تسجيل فواتير الشراء فقط"),
    (TABLES_MANAGE, "إدارة طاولات المطعم/المقهى"),
    (KDS_VIEW, "عرض شاشة المطبخ (KDS) والتعامل مع الطلبات"),
    (BACKUP_MANAGE, "النسخ الاحتياطي والاستعادة وتصفير قاعدة البيانات (خطر)"),
    (HR_VIEW, "عرض الموظفين وسجلات الحضور والرواتب"),
    (HR_MANAGE, "إدارة الموظفين واعتماد ودفع الرواتب"),
    (HR_ATTENDANCE, "تسجيل الحضور والانصراف"),
    (HOTEL_ROOMS_MANAGE, "إدارة غرف الفندق (للأدمن)"),
    (HOTEL_CHARGE, "قيد فاتورة على حساب غرفة فندق (للكاشير)"),
    (HOTEL_SETTLE, "تسوية حسابات غرف الفندق وتسجيل الدفع (للاستقبال)"),
    (HOTEL_BOOKING_VIEW, "عرض حجوزات الفندق"),
    (HOTEL_BOOKING_CREATE, "إنشاء وتأكيد حجوزات"),
    (HOTEL_BOOKING_MANAGE, "تعديل سعر/خصم/إلغاء حجز"),
    (HOTEL_BOOKING_CHECKIN, "Check-in للنزلاء"),
    (HOTEL_BOOKING_CHECKOUT, "Check-out للنزلاء"),
    (HOTEL_BOOKING_CHECKOUT_BALANCE, "Check-out مع مبلغ متبقٍ (مشرف)"),
    (HOTEL_HOUSEKEEPING, "تنظيف الغرف (Housekeeping)"),
    (HOTEL_FINANCE_CLOSE, "إقفال يومي فندقي"),
    (CUSTOMERS_VIEW, "عرض قاعدة العملاء وأرصدة نقاط الولاء"),
    (CUSTOMERS_MANAGE, "إدارة العملاء وتعديل بياناتهم وضبط نقاطهم"),
    (LOYALTY_REDEEM, "استخدام نقاط الولاء كخصم على الفاتورة"),
    (BRANDING_MANAGE, "تخصيص الهويّة البصرية (الشعار والألوان)"),
    (SALES_REFUND, "تنفيذ مرتجعات المبيعات"),
    (SALES_REFUND_OVERRIDE, "اعتماد مرتجع بوسيلة رد مختلفة مع قيد تسوية"),
    (SALES_EDIT_INVOICE, "تعديل فواتير مكتملة (الكمية والسعر)"),
    (SALES_PRINT_RECEIPT, "طباعة فاتورة مكتملة من التقارير"),
    (SALES_CORRECT_PAYMENT, "تصحيح وسيلة الدفع على فاتورة مكتملة"),
    (SALES_VOID_AFTER_KITCHEN, "مسح أصناف أو إلغاء فاتورة بعد الإرسال للمطبخ"),
    (SALES_VOID_SUPERVISOR, "اعتماد إلغاء/مسح بعد المطبخ (كود المشرف)"),
    (ASSETS_EDIT_INVOICE, "تعديل فواتير الأصول والأدوات"),
    (DELIVERY_MANAGE, "إدارة مناطق التوصيل ورسومها"),
    (WAREHOUSES_MANAGE, "إدارة المخازن الفرعية والتحويل من الرئيسي"),
    (MESSAGING_MANAGE, "إدارة بوت المراسلات والحملات"),
    (MESSAGING_SEND, "ردّ على العملاء وإرسال رسائل من صندوق الوارد"),
    (MESSAGING_VIEW, "عرض صندوق الوارد والمحادثات (بدون إرسال)"),
    (GL_MANAGE, "إدارة دفتر الأستاذ العام والتقارير المالية"),
]
