# موديول حجز الشقق الفندقية

## الوحدات (خطط الاشتراك)

| المفتاح | الوصف |
|---------|--------|
| `hotel.core` | غرف + حساب شقة POS (موجود سابقاً) |
| `hotel.booking` | حجز، Check-in/out، تقويم |
| `hotel.portal` | بوابة `/stay` للزبون |
| `hotel.finance` | فواتير إقامة + إقفال يومي |

التخزين: `AppSetting.enabled_modules` (JSON).

## المسارات

- `/admin/hotel/bookings` — إدارة الحجوزات
- `/admin/hotel/bookings/calendar` — تقويم
- `/admin/hotel/room-types` — أنواع الغرف
- `/admin/hotel/housekeeping` — غرف Dirty
- `/admin/hotel/reports` — إشغال + إقفال يومي
- `/stay` — بوابة الزبون
- `/hotel/settle` — تسوية فواتير POS على الشقة (موجود)

## تدفق الوجبات والغسيل

طلبات أثناء الإقامة → `Sale` بسياق `ROOM` → `RoomCharge(booking_id)` → Folio + `/hotel/settle`.

## الملفات

- `modules/hotel/booking_models.py` — نماذج الحجز
- `modules/hotel/booking_service.py` — دورة الحياة
- `modules/hotel/router_bookings.py` — استقبال
- `modules/hotel/router_portal.py` — `/stay`
- `modules/platform/module_registry.py` — تفعيل الوحدات
