-- إصلاح: Unknown column 'hotel_room_charges.service_code' (ومجاوراتها)
-- صفحة ذمم الحجوزات /admin/hotel/debts وعمليات الفوليو
-- آمن للتشغيل أكثر من مرة (يتخطى العمود إن وُجد)

SET @db := DATABASE();

-- service_code
SET @exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'hotel_room_charges' AND COLUMN_NAME = 'service_code'
);
SET @sql := IF(@exists = 0,
  'ALTER TABLE hotel_room_charges ADD COLUMN service_code VARCHAR(40) NULL',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- folio_side
SET @exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'hotel_room_charges' AND COLUMN_NAME = 'folio_side'
);
SET @sql := IF(@exists = 0,
  'ALTER TABLE hotel_room_charges ADD COLUMN folio_side VARCHAR(16) NULL',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- company_amount
SET @exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'hotel_room_charges' AND COLUMN_NAME = 'company_amount'
);
SET @sql := IF(@exists = 0,
  'ALTER TABLE hotel_room_charges ADD COLUMN company_amount DECIMAL(14,3) NULL',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- guest_amount
SET @exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'hotel_room_charges' AND COLUMN_NAME = 'guest_amount'
);
SET @sql := IF(@exists = 0,
  'ALTER TABLE hotel_room_charges ADD COLUMN guest_amount DECIMAL(14,3) NULL',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
