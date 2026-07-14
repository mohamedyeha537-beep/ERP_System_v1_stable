from __future__ import annotations

LOYALTY_POINTS_EARNED = "loyalty.points_earned"
LOYALTY_POINTS_REDEEMED = "loyalty.points_redeemed"
LOYALTY_POINTS_ADJUSTED = "loyalty.points_adjusted"
STOCK_LOW = "stock.low"
PRODUCT_EXPIRY = "product.expiry"
CUSTOMER_FIRST_LINKED = "customer.first_linked"
CAMPAIGN_BROADCAST = "campaign.broadcast"
CAMPAIGN_MANUAL = "campaign.manual"
MESSAGING_TEST = "messaging.test"
MESSAGING_TEST_BUTTONS = "messaging.test_buttons"

ALL_EVENT_TYPES: list[tuple[str, str]] = [
    (LOYALTY_POINTS_EARNED, "اكتساب نقاط ولاء"),
    (LOYALTY_POINTS_REDEEMED, "خصم نقاط ولاء"),
    (LOYALTY_POINTS_ADJUSTED, "تعديل نقاط يدوي"),
    (STOCK_LOW, "نقص مخزون (إدارة)"),
    (PRODUCT_EXPIRY, "قرب انتهاء صلاحية منتج (إدارة)"),
    (CUSTOMER_FIRST_LINKED, "ربط عميل جديد + موافقة"),
    (CAMPAIGN_BROADCAST, "حملة مجدولة"),
    (CAMPAIGN_MANUAL, "حملة يدوية"),
    (MESSAGING_TEST, "اختبار إرسال"),
    (MESSAGING_TEST_BUTTONS, "اختبار أزرار"),
]
