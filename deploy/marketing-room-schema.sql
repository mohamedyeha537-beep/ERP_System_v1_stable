-- غرفة وكلاء التسويق — جداول مرحلة 1
-- آمن للتشغيل المتكرر (IF NOT EXISTS / INSERT IGNORE)

CREATE TABLE IF NOT EXISTS marketing_runs (
  id INT AUTO_INCREMENT PRIMARY KEY,
  business_domain VARCHAR(20) NOT NULL DEFAULT 'shared',
  status VARCHAR(30) NOT NULL DEFAULT 'pending',
  title VARCHAR(200) NOT NULL DEFAULT '',
  triggered_by_id INT NULL,
  error_message TEXT NULL,
  context_json LONGTEXT NULL,
  chief_summary TEXT NULL,
  created_at DATETIME(6) NULL,
  finished_at DATETIME(6) NULL,
  INDEX ix_marketing_runs_domain (business_domain),
  INDEX ix_marketing_runs_status (status),
  CONSTRAINT fk_marketing_runs_user FOREIGN KEY (triggered_by_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS marketing_artifacts (
  id INT AUTO_INCREMENT PRIMARY KEY,
  run_id INT NOT NULL,
  agent_role VARCHAR(40) NOT NULL,
  kind VARCHAR(40) NOT NULL,
  status VARCHAR(30) NOT NULL DEFAULT 'pending_approval',
  title VARCHAR(240) NOT NULL DEFAULT '',
  body_text MEDIUMTEXT NOT NULL,
  meta_json TEXT NULL,
  media_path VARCHAR(500) NULL,
  reviewed_by_id INT NULL,
  reviewed_at DATETIME(6) NULL,
  review_note TEXT NULL,
  created_at DATETIME(6) NULL,
  INDEX ix_marketing_artifacts_run (run_id),
  INDEX ix_marketing_artifacts_role (agent_role),
  INDEX ix_marketing_artifacts_kind (kind),
  INDEX ix_marketing_artifacts_status (status),
  CONSTRAINT fk_marketing_artifacts_run FOREIGN KEY (run_id) REFERENCES marketing_runs(id) ON DELETE CASCADE,
  CONSTRAINT fk_marketing_artifacts_user FOREIGN KEY (reviewed_by_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS marketing_agent_logs (
  id INT AUTO_INCREMENT PRIMARY KEY,
  run_id INT NULL,
  agent_role VARCHAR(40) NOT NULL DEFAULT '',
  level VARCHAR(20) NOT NULL DEFAULT 'info',
  message TEXT NOT NULL,
  created_at DATETIME(6) NULL,
  INDEX ix_marketing_agent_logs_run (run_id),
  CONSTRAINT fk_marketing_agent_logs_run FOREIGN KEY (run_id) REFERENCES marketing_runs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO permissions (code, name_ar) VALUES
  ('marketing_room:view', 'عرض غرفة وكلاء التسويق'),
  ('marketing_room:run', 'تشغيل خط وكلاء التسويق'),
  ('marketing_room:approve', 'اعتماد مسودات التسويق');
