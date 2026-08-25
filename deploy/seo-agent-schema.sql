-- ============================================================
-- SEO Agent Integration — جداول وأعمدة MySQL
-- البيئة: Staging (pos.baytak.ly) أو Production (baytak.baytak.ly)
--
-- التنفيذ: phpMyAdmin → اختر قاعدة البيانات → تبويب SQL → الصق → Go
-- ثم: sudo systemctl restart pos
--
-- تجاهل أخطاء: Duplicate table / Duplicate column / already exists
-- ============================================================

-- ---------- جداول SEO الجديدة ----------

CREATE TABLE IF NOT EXISTS seo_sites (
  id INT AUTO_INCREMENT PRIMARY KEY,
  environment VARCHAR(20) NOT NULL,
  base_url VARCHAR(255) NOT NULL,
  name VARCHAR(120) NOT NULL DEFAULT '',
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX ix_seo_sites_environment (environment)
);

CREATE TABLE IF NOT EXISTS seo_pages (
  id INT AUTO_INCREMENT PRIMARY KEY,
  environment VARCHAR(20) NOT NULL,
  page_type VARCHAR(40) NOT NULL,
  entity_type VARCHAR(40) NULL,
  entity_id INT NULL,
  url VARCHAR(500) NOT NULL,
  title VARCHAR(255) NULL,
  meta_description VARCHAR(500) NULL,
  h1 VARCHAR(255) NULL,
  canonical_url VARCHAR(500) NULL,
  status_code INT NULL,
  indexable TINYINT(1) NOT NULL DEFAULT 1,
  language VARCHAR(10) NOT NULL DEFAULT 'ar',
  last_scanned_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX ix_seo_pages_environment (environment),
  INDEX ix_seo_pages_page_type (page_type),
  INDEX ix_seo_pages_entity_type (entity_type),
  INDEX ix_seo_pages_entity_id (entity_id)
);

CREATE TABLE IF NOT EXISTS seo_crawl_runs (
  id INT AUTO_INCREMENT PRIMARY KEY,
  environment VARCHAR(20) NOT NULL,
  agent_name VARCHAR(80) NOT NULL DEFAULT 'technical',
  status VARCHAR(30) NOT NULL DEFAULT 'running',
  started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at DATETIME NULL,
  pages_scanned INT NOT NULL DEFAULT 0,
  issues_found INT NOT NULL DEFAULT 0,
  error_message TEXT NULL,
  raw_summary_json TEXT NULL,
  INDEX ix_seo_crawl_runs_environment (environment),
  INDEX ix_seo_crawl_runs_status (status)
);

CREATE TABLE IF NOT EXISTS seo_issues (
  id INT AUTO_INCREMENT PRIMARY KEY,
  environment VARCHAR(20) NOT NULL,
  crawl_run_id INT NULL,
  page_id INT NULL,
  severity VARCHAR(20) NOT NULL DEFAULT 'medium',
  issue_type VARCHAR(60) NOT NULL,
  title VARCHAR(255) NOT NULL,
  description TEXT NULL,
  current_value TEXT NULL,
  recommended_value TEXT NULL,
  status VARCHAR(30) NOT NULL DEFAULT 'open',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resolved_at DATETIME NULL,
  INDEX ix_seo_issues_environment (environment),
  INDEX ix_seo_issues_crawl_run_id (crawl_run_id),
  INDEX ix_seo_issues_page_id (page_id),
  INDEX ix_seo_issues_severity (severity),
  INDEX ix_seo_issues_issue_type (issue_type),
  INDEX ix_seo_issues_status (status)
);

CREATE TABLE IF NOT EXISTS seo_suggestions (
  id INT AUTO_INCREMENT PRIMARY KEY,
  environment VARCHAR(20) NOT NULL,
  page_id INT NULL,
  agent_name VARCHAR(80) NOT NULL DEFAULT 'content',
  suggestion_type VARCHAR(60) NOT NULL,
  current_value TEXT NULL,
  suggested_value TEXT NULL,
  reason TEXT NULL,
  confidence_score DOUBLE NULL,
  status VARCHAR(30) NOT NULL DEFAULT 'pending',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  reviewed_by_id INT NULL,
  reviewed_at DATETIME NULL,
  INDEX ix_seo_suggestions_environment (environment),
  INDEX ix_seo_suggestions_page_id (page_id),
  INDEX ix_seo_suggestions_suggestion_type (suggestion_type),
  INDEX ix_seo_suggestions_status (status)
);

CREATE TABLE IF NOT EXISTS seo_fix_requests (
  id INT AUTO_INCREMENT PRIMARY KEY,
  environment VARCHAR(20) NOT NULL,
  page_id INT NULL,
  suggestion_id INT NULL,
  fix_type VARCHAR(60) NOT NULL,
  payload_json TEXT NOT NULL,
  rollback_json TEXT NULL,
  status VARCHAR(30) NOT NULL DEFAULT 'pending',
  approved_for_production TINYINT(1) NOT NULL DEFAULT 0,
  approved_by_id INT NULL,
  approved_at DATETIME NULL,
  applied_by_id INT NULL,
  applied_at DATETIME NULL,
  rolled_back_by_id INT NULL,
  rolled_back_at DATETIME NULL,
  error_message TEXT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX ix_seo_fix_requests_environment (environment),
  INDEX ix_seo_fix_requests_page_id (page_id),
  INDEX ix_seo_fix_requests_suggestion_id (suggestion_id),
  INDEX ix_seo_fix_requests_fix_type (fix_type),
  INDEX ix_seo_fix_requests_status (status)
);

CREATE TABLE IF NOT EXISTS seo_reports (
  id INT AUTO_INCREMENT PRIMARY KEY,
  environment VARCHAR(20) NOT NULL,
  report_type VARCHAR(40) NOT NULL,
  period_start DATETIME NULL,
  period_end DATETIME NULL,
  title VARCHAR(255) NOT NULL DEFAULT '',
  summary TEXT NULL,
  data_json TEXT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX ix_seo_reports_environment (environment),
  INDEX ix_seo_reports_report_type (report_type)
);

CREATE TABLE IF NOT EXISTS seo_agent_logs (
  id INT AUTO_INCREMENT PRIMARY KEY,
  environment VARCHAR(20) NOT NULL,
  agent_name VARCHAR(80) NOT NULL DEFAULT '',
  action VARCHAR(80) NOT NULL,
  request_id VARCHAR(80) NULL,
  status VARCHAR(30) NOT NULL DEFAULT 'ok',
  message TEXT NULL,
  payload_json TEXT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX ix_seo_agent_logs_environment (environment),
  INDEX ix_seo_agent_logs_action (action),
  INDEX ix_seo_agent_logs_request_id (request_id),
  INDEX ix_seo_agent_logs_created_at (created_at)
);

-- ---------- أعمدة SEO على الجداول الموجودة ----------
-- نفّذ سطراً سطراً إن لزم. تجاهل Duplicate column name

ALTER TABLE products ADD COLUMN seo_title VARCHAR(255) NULL;
ALTER TABLE products ADD COLUMN seo_description VARCHAR(500) NULL;
ALTER TABLE products ADD COLUMN seo_h1 VARCHAR(255) NULL;
ALTER TABLE products ADD COLUMN seo_slug VARCHAR(255) NULL;
ALTER TABLE products ADD COLUMN seo_keywords VARCHAR(500) NULL;
ALTER TABLE products ADD COLUMN seo_schema_json TEXT NULL;
ALTER TABLE products ADD COLUMN seo_og_title VARCHAR(255) NULL;
ALTER TABLE products ADD COLUMN seo_og_description VARCHAR(500) NULL;
ALTER TABLE products ADD COLUMN seo_og_image VARCHAR(500) NULL;
ALTER TABLE products ADD COLUMN seo_indexable TINYINT(1) NOT NULL DEFAULT 1;
ALTER TABLE products ADD COLUMN seo_canonical_url VARCHAR(500) NULL;
ALTER TABLE products ADD COLUMN seo_updated_at DATETIME NULL;

ALTER TABLE product_categories ADD COLUMN seo_title VARCHAR(255) NULL;
ALTER TABLE product_categories ADD COLUMN seo_description VARCHAR(500) NULL;
ALTER TABLE product_categories ADD COLUMN seo_h1 VARCHAR(255) NULL;
ALTER TABLE product_categories ADD COLUMN seo_slug VARCHAR(255) NULL;
ALTER TABLE product_categories ADD COLUMN seo_keywords VARCHAR(500) NULL;
ALTER TABLE product_categories ADD COLUMN seo_schema_json TEXT NULL;
ALTER TABLE product_categories ADD COLUMN seo_og_title VARCHAR(255) NULL;
ALTER TABLE product_categories ADD COLUMN seo_og_description VARCHAR(500) NULL;
ALTER TABLE product_categories ADD COLUMN seo_og_image VARCHAR(500) NULL;
ALTER TABLE product_categories ADD COLUMN seo_indexable TINYINT(1) NOT NULL DEFAULT 1;
ALTER TABLE product_categories ADD COLUMN seo_canonical_url VARCHAR(500) NULL;
ALTER TABLE product_categories ADD COLUMN seo_updated_at DATETIME NULL;

ALTER TABLE hotel_rooms ADD COLUMN seo_title VARCHAR(255) NULL;
ALTER TABLE hotel_rooms ADD COLUMN seo_description VARCHAR(500) NULL;
ALTER TABLE hotel_rooms ADD COLUMN seo_h1 VARCHAR(255) NULL;
ALTER TABLE hotel_rooms ADD COLUMN seo_slug VARCHAR(255) NULL;
ALTER TABLE hotel_rooms ADD COLUMN seo_keywords VARCHAR(500) NULL;
ALTER TABLE hotel_rooms ADD COLUMN seo_schema_json TEXT NULL;
ALTER TABLE hotel_rooms ADD COLUMN seo_og_title VARCHAR(255) NULL;
ALTER TABLE hotel_rooms ADD COLUMN seo_og_description VARCHAR(500) NULL;
ALTER TABLE hotel_rooms ADD COLUMN seo_og_image VARCHAR(500) NULL;
ALTER TABLE hotel_rooms ADD COLUMN seo_indexable TINYINT(1) NOT NULL DEFAULT 1;
ALTER TABLE hotel_rooms ADD COLUMN seo_canonical_url VARCHAR(500) NULL;
ALTER TABLE hotel_rooms ADD COLUMN seo_updated_at DATETIME NULL;

ALTER TABLE hotel_service_catalog ADD COLUMN seo_title VARCHAR(255) NULL;
ALTER TABLE hotel_service_catalog ADD COLUMN seo_description VARCHAR(500) NULL;
ALTER TABLE hotel_service_catalog ADD COLUMN seo_h1 VARCHAR(255) NULL;
ALTER TABLE hotel_service_catalog ADD COLUMN seo_slug VARCHAR(255) NULL;
ALTER TABLE hotel_service_catalog ADD COLUMN seo_keywords VARCHAR(500) NULL;
ALTER TABLE hotel_service_catalog ADD COLUMN seo_schema_json TEXT NULL;
ALTER TABLE hotel_service_catalog ADD COLUMN seo_og_title VARCHAR(255) NULL;
ALTER TABLE hotel_service_catalog ADD COLUMN seo_og_description VARCHAR(500) NULL;
ALTER TABLE hotel_service_catalog ADD COLUMN seo_og_image VARCHAR(500) NULL;
ALTER TABLE hotel_service_catalog ADD COLUMN seo_indexable TINYINT(1) NOT NULL DEFAULT 1;
ALTER TABLE hotel_service_catalog ADD COLUMN seo_canonical_url VARCHAR(500) NULL;
ALTER TABLE hotel_service_catalog ADD COLUMN seo_updated_at DATETIME NULL;

-- ---------- صلاحيات SEO (تُضاف أيضاً تلقائياً عند إعادة تشغيل التطبيق) ----------
INSERT IGNORE INTO permissions (code, label_ar) VALUES
  ('seo:view', 'عرض مركز السيو (SEO Center)'),
  ('seo:review', 'مراجعة اقتراحات السيو ورفضها'),
  ('seo:approve', 'اعتماد إصلاحات السيو'),
  ('seo:apply', 'تطبيق إصلاحات السيو'),
  ('seo:rollback', 'التراجع عن إصلاحات السيو'),
  ('seo:production_approve', 'اعتماد تطبيق السيو على الإنتاج'),
  ('seo:settings', 'إعدادات وكلاء السيو');

-- ربط صلاحيات SEO بدور الأدمن (عدّل اسم الدور إن لزم)
-- INSERT IGNORE INTO role_permissions (role_id, permission_id)
-- SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
-- WHERE r.name_ar LIKE '%أدمن%' OR r.name_ar LIKE '%Admin%'
--   AND p.code LIKE 'seo:%';
