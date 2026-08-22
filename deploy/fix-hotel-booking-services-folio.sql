-- إصلاح: Unknown column 'hotel_booking_services.service_code' (ومجاوراتها)
-- تسجيل مخالفة / توزيع شركة-نزيل على خدمات الحجز
-- آمن للتشغيل أكثر من مرة (يتخطى العمود إن وُجد)

SET @db := DATABASE();

SET @exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'hotel_booking_services' AND COLUMN_NAME = 'service_code'
);
SET @sql := IF(@exists = 0,
  'ALTER TABLE hotel_booking_services ADD COLUMN service_code VARCHAR(40) NULL',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'hotel_booking_services' AND COLUMN_NAME = 'folio_side'
);
SET @sql := IF(@exists = 0,
  'ALTER TABLE hotel_booking_services ADD COLUMN folio_side VARCHAR(16) NULL',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'hotel_booking_services' AND COLUMN_NAME = 'company_amount'
);
SET @sql := IF(@exists = 0,
  'ALTER TABLE hotel_booking_services ADD COLUMN company_amount DECIMAL(14,3) NULL',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'hotel_booking_services' AND COLUMN_NAME = 'guest_amount'
);
SET @sql := IF(@exists = 0,
  'ALTER TABLE hotel_booking_services ADD COLUMN guest_amount DECIMAL(14,3) NULL',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
