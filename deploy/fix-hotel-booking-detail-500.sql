-- إصلاح خطأ 500 بعد إنشاء الحجز (تفاصيل الحجز / الإيصال)
-- السبب الشائع: أعمدة جديدة في الكود غير موجودة في MySQL
-- نفّذ من phpMyAdmin ثم: sudo systemctl restart pos
-- تجاهل Duplicate column إن ظهر.

ALTER TABLE hotel_booking_services
  ADD COLUMN sale_id INT NULL;

CREATE INDEX ix_hotel_booking_services_sale_id
  ON hotel_booking_services (sale_id);

ALTER TABLE hotel_booking_services
  ADD COLUMN charged_to_guest TINYINT(1) NOT NULL DEFAULT 1;

ALTER TABLE hotel_rooms
  ADD COLUMN lock_no VARCHAR(16) NULL;

ALTER TABLE sales
  ADD COLUMN booking_id INT NULL;

CREATE INDEX ix_sales_booking_id ON sales (booking_id);
