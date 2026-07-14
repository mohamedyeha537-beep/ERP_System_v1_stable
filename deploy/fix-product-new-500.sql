-- إصلاح خطأ 500 في /catalog/products/new على pos.baytak.ly
-- phpMyAdmin: اختر قاعدة البيانات → SQL → الصق الكل → Go
-- تجاهل أي سطر يعطي: Duplicate column name

CREATE TABLE IF NOT EXISTS kitchen_departments (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name_ar VARCHAR(120) NOT NULL,
  venue VARCHAR(20) NOT NULL DEFAULT 'KITCHEN',
  sort_order INT NOT NULL DEFAULT 0,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  routing_mode VARCHAR(20) NOT NULL DEFAULT 'SCREEN',
  routing_target VARCHAR(255) NULL
);

ALTER TABLE product_categories ADD COLUMN color_hex VARCHAR(7) NOT NULL DEFAULT '#3b82f6';
ALTER TABLE product_categories ADD COLUMN delete_protected TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE product_categories ADD COLUMN routing_mode VARCHAR(20) NOT NULL DEFAULT 'NONE';
ALTER TABLE product_categories ADD COLUMN routing_target VARCHAR(255) NULL;
ALTER TABLE product_categories ADD COLUMN show_in_pos TINYINT(1) NOT NULL DEFAULT 1;

ALTER TABLE products ADD COLUMN kitchen_department_id INT NULL;
ALTER TABLE products ADD COLUMN image_filename VARCHAR(255) NULL;
ALTER TABLE products ADD COLUMN show_in_pos TINYINT(1) NOT NULL DEFAULT 1;
ALTER TABLE products ADD COLUMN reference_unit_cost DECIMAL(14,3) NULL;
ALTER TABLE products ADD COLUMN price_linked_to_bom TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE products ADD COLUMN bom_markup_pct DECIMAL(8,2) NULL;
ALTER TABLE products ADD COLUMN line_modifier_presets TEXT NULL;
ALTER TABLE products ADD COLUMN expiry_tracked TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE products ADD COLUMN expiry_production_date DATE NULL;
ALTER TABLE products ADD COLUMN expiry_date DATE NULL;
ALTER TABLE products ADD COLUMN expiry_warn_days INT NOT NULL DEFAULT 7;

-- تحقق (يجب 4 صفوف):
-- SELECT column_name FROM information_schema.columns
-- WHERE table_schema = DATABASE() AND table_name = 'product_categories'
--   AND column_name IN ('color_hex','delete_protected','routing_mode','routing_target');
