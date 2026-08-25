-- حسابات الشركات: خصم / ائتمان / ربط فرد بشركة / حجز تحت شركة
-- آمن لإعادة التشغيل (يتجاهل العمود إن وُجد)

ALTER TABLE customers ADD COLUMN parent_company_id INT NULL;
ALTER TABLE customers ADD COLUMN company_discount_percent DECIMAL(7,3) NOT NULL DEFAULT 0;
ALTER TABLE customers ADD COLUMN company_credit_limit DECIMAL(14,3) NOT NULL DEFAULT 0;
ALTER TABLE customers ADD COLUMN allow_company_credit TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE hotel_bookings ADD COLUMN company_customer_id INT NULL;
ALTER TABLE hotel_bookings ADD COLUMN booking_payer VARCHAR(20) NULL;
ALTER TABLE hotel_bookings ADD COLUMN is_tourism_agency TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE hotel_bookings ADD COLUMN tourism_commission_percent DECIMAL(7,3) NOT NULL DEFAULT 0;
