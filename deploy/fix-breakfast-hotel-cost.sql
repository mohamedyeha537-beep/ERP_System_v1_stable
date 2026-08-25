-- إفطار مشمول: تكلفة فندق لا تُضاف على حساب النزيل
-- نفّذ على قاعدة بيانات المنصة ثم: sudo systemctl restart pos
-- تجاهل Duplicate column

ALTER TABLE hotel_booking_services
  ADD COLUMN charged_to_guest TINYINT(1) NOT NULL DEFAULT 1;

ALTER TABLE products
  ADD COLUMN is_hotel_breakfast TINYINT(1) NOT NULL DEFAULT 0;

-- تعليم أصناف الإفطار تلقائياً بالاسم (اختياري — راجع النتائج بعد التنفيذ)
UPDATE products
SET is_hotel_breakfast = 1
WHERE name_ar LIKE '%افطار%'
   OR name_ar LIKE '%إفطار%'
   OR name_ar LIKE '%فطور%'
   OR LOWER(name_ar) LIKE '%breakfast%';
