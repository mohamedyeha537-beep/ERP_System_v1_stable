-- إصلاح أعمدة الكatalog الناقصة على MySQL (شغّل مرة واحدة على VPS)
-- mysql -u pos_user -p pos_db < deploy/mysql-catalog-columns.sql

CREATE TABLE IF NOT EXISTS kitchen_departments (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name_ar VARCHAR(120) NOT NULL,
  venue VARCHAR(20) NOT NULL DEFAULT 'KITCHEN',
  sort_order INT NOT NULL DEFAULT 0,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  routing_mode VARCHAR(20) NOT NULL DEFAULT 'SCREEN',
  routing_target VARCHAR(255) NULL
);

ALTER TABLE product_categories
  ADD COLUMN show_in_pos TINYINT(1) NOT NULL DEFAULT 1;

ALTER TABLE product_categories
  ADD COLUMN delete_protected TINYINT(1) NOT NULL DEFAULT 0;

ALTER TABLE product_categories
  ADD COLUMN routing_mode VARCHAR(20) NOT NULL DEFAULT 'NONE';

ALTER TABLE product_categories
  ADD COLUMN routing_target VARCHAR(255) NULL;

ALTER TABLE products
  ADD COLUMN show_in_pos TINYINT(1) NOT NULL DEFAULT 1;

ALTER TABLE products
  ADD COLUMN kitchen_department_id INT NULL;

ALTER TABLE products
  ADD COLUMN image_filename VARCHAR(255) NULL;

ALTER TABLE products
  ADD COLUMN line_modifier_presets TEXT NULL;

ALTER TABLE products
  ADD COLUMN expiry_tracked TINYINT(1) NOT NULL DEFAULT 0;

ALTER TABLE products
  ADD COLUMN expiry_production_date DATE NULL;

ALTER TABLE products
  ADD COLUMN expiry_date DATE NULL;

ALTER TABLE products
  ADD COLUMN expiry_warn_days INT NOT NULL DEFAULT 7;

ALTER TABLE products
  ADD COLUMN price_linked_to_bom TINYINT(1) NOT NULL DEFAULT 0;

ALTER TABLE products
  ADD COLUMN bom_markup_pct DECIMAL(8,2) NULL;

ALTER TABLE products
  ADD COLUMN reference_unit_cost DECIMAL(14,3) NULL;

UPDATE products SET show_in_pos = 0 WHERE kind = 'STOCK_ONLY';
