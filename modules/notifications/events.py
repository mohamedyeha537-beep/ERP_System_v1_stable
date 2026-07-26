from __future__ import annotations

# Phase 1 — wired
ORDER_CREATED = "order.created"
ORDER_DELIVERED = "order.delivered"
LOYALTY_POINTS_EARNED = "loyalty.points_earned"
INVENTORY_LOW_STOCK = "inventory.low_stock"

# Phase 2 — order lifecycle
ORDER_CONFIRMED = "order.confirmed"
ORDER_REJECTED = "order.rejected"
ORDER_PAYMENT_PENDING = "order.payment_pending"
ORDER_PAYMENT_PAID = "order.payment_paid"
ORDER_SENT_TO_KITCHEN = "order.sent_to_kitchen"
ORDER_PREPARING = "order.preparing"
ORDER_READY = "order.ready"
ORDER_DRIVER_ASSIGNED = "order.driver_assigned"
ORDER_DRIVER_ACCEPTED = "order.driver_accepted"
ORDER_DRIVER_REJECTED = "order.driver_rejected"
ORDER_OUT_FOR_DELIVERY = "order.out_for_delivery"
ORDER_CANCELLED = "order.cancelled"
ORDER_FAILED_DELIVERY = "order.failed_delivery"
ORDER_REFUNDED = "order.refunded"

CUSTOMER_CREATED = "customer.created"
LOYALTY_ACCOUNT_CREATED = "loyalty.account_created"
LOYALTY_POINTS_REDEEMED = "loyalty.points_redeemed"
REFERRAL_LINK_CREATED = "referral.link_created"
REFERRAL_PRODUCT_SHARED = "referral.product_shared"
REFERRAL_FIRST_ORDER = "referral.first_order_completed"
REFERRAL_REFERRER_REWARDED = "referral.referrer_rewarded"

POS_SHIFT_OPENED = "pos.shift_opened"
POS_SHIFT_CLOSED = "pos.shift_closed"
POS_CASH_SHORTAGE = "pos.cash_shortage"
POS_CASH_OVERAGE = "pos.cash_overage"
POS_INVOICE_CANCEL_REQUESTED = "pos.invoice_cancel_requested"
POS_INVOICE_CANCEL_APPROVED = "pos.invoice_cancel_approved"
POS_ITEM_VOID_REQUESTED = "pos.item_void_requested"
POS_ITEM_VOID_APPROVED = "pos.item_void_approved"

TREASURY_SHIFT_CLOSED = "treasury.shift_closed"
TREASURY_MOVEMENT = "treasury.movement"
TREASURY_BALANCE_UPDATE = "treasury.balance_update"

HOTEL_BOOKING_CREATED = "hotel.booking_created"
HOTEL_BOOKING_CONFIRMED = "hotel.booking_confirmed"
HOTEL_ONLINE_BOOKING_REQUEST = "hotel.online_booking_request"
HOTEL_CHECKOUT_REMINDER = "hotel.checkout_reminder"
HOTEL_NIGHT_PAYMENT_DUE = "hotel.night_payment_due"
HOTEL_BALANCE_CLAIM = "hotel.balance_claim"
HOTEL_UNPAID_SERVICE_ADDED = "hotel.unpaid_service_added"
HOTEL_PAYMENT_RECEIVED = "hotel.payment_received"
HOTEL_ROOM_MAINTENANCE = "hotel.room_maintenance"
HOTEL_ROOM_CLEANING = "hotel.room_cleaning"
HOTEL_SHIFT_OPENED = "hotel.shift_opened"
HOTEL_SHIFT_CLOSED = "hotel.shift_closed"
HOTEL_SHIFT_OVERDUE = "hotel.shift_overdue"

KITCHEN_TICKET_CREATED = "kitchen.ticket_created"
KITCHEN_ITEM_CANCELLED = "kitchen.item_cancelled"
KITCHEN_ORDER_DELAYED = "kitchen.order_delayed"

# Phase 3 — inventory catalog
INVENTORY_OUT_OF_STOCK = "inventory.out_of_stock"
INVENTORY_NEGATIVE_STOCK = "inventory.negative_stock"
INVENTORY_STOCK_REPLENISHED = "inventory.stock_replenished"
INVENTORY_PURCHASE_NEEDED = "inventory.purchase_needed"
INVENTORY_PURCHASE_RECEIVED = "inventory.purchase_received"
INVENTORY_STOCK_ADJUSTMENT_CREATED = "inventory.stock_adjustment_created"
INVENTORY_STOCK_ADJUSTMENT_APPROVED = "inventory.stock_adjustment_approved"
INVENTORY_STOCK_ADJUSTMENT_REJECTED = "inventory.stock_adjustment_rejected"
INVENTORY_WASTE_RECORDED = "inventory.waste_recorded"
INVENTORY_EXPIRY_WARNING = "inventory.expiry_warning"
INVENTORY_EXPIRED_ITEM = "inventory.expired_item"
INVENTORY_HIGH_USAGE = "inventory.high_usage_detected"
INVENTORY_UNUSUAL_MOVEMENT = "inventory.unusual_stock_movement"
INVENTORY_UNUSUAL_STOCK_MOVEMENT = INVENTORY_UNUSUAL_MOVEMENT
INVENTORY_TRANSFER_REQUESTED = "inventory.transfer_requested"
INVENTORY_TRANSFER_APPROVED = "inventory.transfer_approved"
INVENTORY_TRANSFER_RECEIVED = "inventory.transfer_received"
INVENTORY_RECIPE_COST_CHANGED = "inventory.recipe_cost_changed"
INVENTORY_BOM_MISSING_COST = "inventory.bom_missing_cost"

# HR — حضور، رواتب، سلف، خصومات
HR_ATTENDANCE_CHECK_IN = "hr.attendance.check_in"
HR_ATTENDANCE_CHECK_OUT = "hr.attendance.check_out"
HR_PAYROLL_PAID = "hr.payroll.paid"
HR_PAYROLL_POSTED = "hr.payroll.posted"
HR_ADVANCE_GIVEN = "hr.advance.given"
HR_DEDUCTION_CREATED = "hr.deduction.created"

ALL_EVENT_KEYS: list[tuple[str, str]] = [
    (ORDER_CREATED, "إنشاء طلب"),
    (ORDER_CONFIRMED, "تأكيد طلب"),
    (ORDER_SENT_TO_KITCHEN, "إرسال للمطبخ"),
    (ORDER_DRIVER_ASSIGNED, "تعيين سائق"),
    (ORDER_OUT_FOR_DELIVERY, "خرج للتوصيل"),
    (ORDER_DELIVERED, "تسليم طلب"),
    (ORDER_CANCELLED, "إلغاء طلب"),
    (LOYALTY_POINTS_EARNED, "اكتساب نقاط"),
    (LOYALTY_POINTS_REDEEMED, "خصم نقاط"),
    (LOYALTY_ACCOUNT_CREATED, "حساب ولاء"),
    (CUSTOMER_CREATED, "عميل جديد"),
    (REFERRAL_LINK_CREATED, "كود إحالة جديد"),
    (REFERRAL_PRODUCT_SHARED, "مشاركة منتج + كود إحالة"),
    (REFERRAL_FIRST_ORDER, "أول طلب إحالة"),
    (REFERRAL_REFERRER_REWARDED, "مكافأة المحيل"),
    (POS_SHIFT_OPENED, "فتح جلسة"),
    (POS_SHIFT_CLOSED, "إغلاق جلسة"),
    (POS_CASH_SHORTAGE, "عجز جلسة"),
    (POS_CASH_OVERAGE, "فائض جلسة"),
    (POS_ITEM_VOID_REQUESTED, "طلب مسح صنف"),
    (POS_INVOICE_CANCEL_REQUESTED, "طلب إلغاء فاتورة"),
    (TREASURY_SHIFT_CLOSED, "إغلاق جلسة وخزينة"),
    (TREASURY_MOVEMENT, "حركة خزينة"),
    (TREASURY_BALANCE_UPDATE, "تحديث رصيد خزينة"),
    (HOTEL_BOOKING_CREATED, "إنشاء حجز فندقي"),
    (HOTEL_BOOKING_CONFIRMED, "تأكيد حجز فندقي"),
    (HOTEL_ONLINE_BOOKING_REQUEST, "طلب حجز أونلاين — تنبيه الموظف"),
    (HOTEL_CHECKOUT_REMINDER, "تذكير مغادرة فندقية"),
    (HOTEL_NIGHT_PAYMENT_DUE, "ليلة فندقية مستحقة"),
    (HOTEL_BALANCE_CLAIM, "مطالبة رصيد حجز فندقي"),
    (HOTEL_UNPAID_SERVICE_ADDED, "خدمة فندقية غير مدفوعة"),
    (HOTEL_PAYMENT_RECEIVED, "سداد حجز فندقي"),
    (HOTEL_ROOM_MAINTENANCE, "طلب صيانة شقة"),
    (HOTEL_ROOM_CLEANING, "مهمة تنظيف شقة"),
    (HOTEL_SHIFT_OPENED, "فتح وردية فندق"),
    (HOTEL_SHIFT_CLOSED, "إقفال وردية فندق"),
    (HOTEL_SHIFT_OVERDUE, "تأخر إقفال وردية"),
    (KITCHEN_TICKET_CREATED, "تذكرة مطبخ"),
    (KITCHEN_ITEM_CANCELLED, "إلغاء صنف مطبخ"),
    (INVENTORY_LOW_STOCK, "مخزون منخفض"),
    (INVENTORY_OUT_OF_STOCK, "نفاد مخزون"),
    (INVENTORY_NEGATIVE_STOCK, "مخزون سالب"),
    (INVENTORY_STOCK_REPLENISHED, "تعبئة مخزون"),
    (INVENTORY_PURCHASE_NEEDED, "حاجة شراء"),
    (INVENTORY_PURCHASE_RECEIVED, "استلام شراء"),
    (INVENTORY_STOCK_ADJUSTMENT_CREATED, "تسوية مخزون"),
    (INVENTORY_WASTE_RECORDED, "تسجيل هالك"),
    (INVENTORY_EXPIRY_WARNING, "قرب انتهاء صلاحية"),
    (INVENTORY_EXPIRED_ITEM, "صلاحية منتهية"),
    (INVENTORY_TRANSFER_RECEIVED, "استلام تحويل"),
    (INVENTORY_UNUSUAL_STOCK_MOVEMENT, "حركة مخزون غير عادية"),
    (INVENTORY_RECIPE_COST_CHANGED, "تغيّر تكلفة تركيبة"),
    (INVENTORY_BOM_MISSING_COST, "مكوّن بدون تكلفة"),
    (HR_ATTENDANCE_CHECK_IN, "حضور موظف"),
    (HR_ATTENDANCE_CHECK_OUT, "انصراف موظف"),
    (HR_PAYROLL_PAID, "صرف راتب"),
    (HR_PAYROLL_POSTED, "اعتماد كشف رواتب"),
    (HR_ADVANCE_GIVEN, "صرف سلفة"),
    (HR_DEDUCTION_CREATED, "خصم على موظف"),
]

PHASE1_EVENT_KEYS: frozenset[str] = frozenset(
    {
        ORDER_CREATED,
        ORDER_DELIVERED,
        LOYALTY_POINTS_EARNED,
        INVENTORY_LOW_STOCK,
    }
)

PHASE2_EVENT_KEYS: frozenset[str] = frozenset(
    {
        ORDER_CONFIRMED,
        ORDER_SENT_TO_KITCHEN,
        ORDER_DRIVER_ASSIGNED,
        ORDER_OUT_FOR_DELIVERY,
        LOYALTY_POINTS_REDEEMED,
        CUSTOMER_CREATED,
        POS_SHIFT_CLOSED,
        POS_CASH_SHORTAGE,
        POS_ITEM_VOID_REQUESTED,
        POS_INVOICE_CANCEL_REQUESTED,
    }
)

PHASE3_EVENT_KEYS: frozenset[str] = frozenset(
    {
        INVENTORY_OUT_OF_STOCK,
        INVENTORY_NEGATIVE_STOCK,
        INVENTORY_STOCK_REPLENISHED,
        INVENTORY_PURCHASE_NEEDED,
        INVENTORY_PURCHASE_RECEIVED,
        INVENTORY_STOCK_ADJUSTMENT_CREATED,
        INVENTORY_WASTE_RECORDED,
        INVENTORY_EXPIRY_WARNING,
        INVENTORY_EXPIRED_ITEM,
        INVENTORY_TRANSFER_RECEIVED,
        INVENTORY_UNUSUAL_STOCK_MOVEMENT,
        INVENTORY_RECIPE_COST_CHANGED,
        INVENTORY_BOM_MISSING_COST,
    }
)

PHASE4_EVENT_KEYS: frozenset[str] = frozenset(
    {
        REFERRAL_LINK_CREATED,
        REFERRAL_PRODUCT_SHARED,
        REFERRAL_FIRST_ORDER,
        REFERRAL_REFERRER_REWARDED,
        LOYALTY_ACCOUNT_CREATED,
        ORDER_CANCELLED,
        KITCHEN_TICKET_CREATED,
        KITCHEN_ITEM_CANCELLED,
        POS_SHIFT_OPENED,
        POS_CASH_OVERAGE,
    }
)

PHASE5_EVENT_KEYS: frozenset[str] = frozenset(
    {
        HR_ATTENDANCE_CHECK_IN,
        HR_ATTENDANCE_CHECK_OUT,
        HR_PAYROLL_PAID,
        HR_PAYROLL_POSTED,
        HR_ADVANCE_GIVEN,
        HR_DEDUCTION_CREATED,
    }
)

PHASE6_EVENT_KEYS: frozenset[str] = frozenset(
    {
        TREASURY_SHIFT_CLOSED,
        TREASURY_MOVEMENT,
        TREASURY_BALANCE_UPDATE,
    }
)

PHASE7_EVENT_KEYS: frozenset[str] = frozenset(
    {
        HOTEL_BOOKING_CREATED,
        HOTEL_BOOKING_CONFIRMED,
        HOTEL_ONLINE_BOOKING_REQUEST,
        HOTEL_CHECKOUT_REMINDER,
        HOTEL_NIGHT_PAYMENT_DUE,
        HOTEL_BALANCE_CLAIM,
        HOTEL_UNPAID_SERVICE_ADDED,
        HOTEL_PAYMENT_RECEIVED,
        HOTEL_ROOM_MAINTENANCE,
        HOTEL_ROOM_CLEANING,
    }
)

WIRED_EVENT_KEYS: frozenset[str] = (
    PHASE1_EVENT_KEYS
    | PHASE2_EVENT_KEYS
    | PHASE3_EVENT_KEYS
    | PHASE4_EVENT_KEYS
    | PHASE5_EVENT_KEYS
    | PHASE6_EVENT_KEYS
    | PHASE7_EVENT_KEYS
)

DEFAULT_THROTTLE_MINUTES: dict[str, int] = {
    INVENTORY_LOW_STOCK: 360,
    INVENTORY_OUT_OF_STOCK: 60,
    INVENTORY_NEGATIVE_STOCK: 30,
    INVENTORY_EXPIRY_WARNING: 1440,
    INVENTORY_STOCK_REPLENISHED: 360,
    INVENTORY_WASTE_RECORDED: 60,
    INVENTORY_UNUSUAL_STOCK_MOVEMENT: 120,
    INVENTORY_EXPIRED_ITEM: 1440,
    INVENTORY_BOM_MISSING_COST: 1440,
    INVENTORY_RECIPE_COST_CHANGED: 360,
    REFERRAL_LINK_CREATED: 1440,
    HOTEL_CHECKOUT_REMINDER: 1440,
    HOTEL_NIGHT_PAYMENT_DUE: 1440,
    HOTEL_BALANCE_CLAIM: 1440,
    KITCHEN_TICKET_CREATED: 60,
    HR_ATTENDANCE_CHECK_IN: 60,
    HR_ATTENDANCE_CHECK_OUT: 60,
    HR_PAYROLL_PAID: 1440,
    HR_ADVANCE_GIVEN: 1440,
    HR_DEDUCTION_CREATED: 1440,
}
