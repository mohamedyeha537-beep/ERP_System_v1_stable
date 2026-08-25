-- منح/منع صلاحيات لكل مستخدم (فوق الأدوار)
-- التطبيق ينشئ الجداول أيضاً عبر schema patch عند الإقلاع.

CREATE TABLE IF NOT EXISTS user_permission_grants (
  user_id INT NOT NULL,
  permission_id INT NOT NULL,
  PRIMARY KEY (user_id, permission_id),
  CONSTRAINT fk_upg_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_upg_perm FOREIGN KEY (permission_id) REFERENCES permissions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_permission_denies (
  user_id INT NOT NULL,
  permission_id INT NOT NULL,
  PRIMARY KEY (user_id, permission_id),
  CONSTRAINT fk_upd_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_upd_perm FOREIGN KEY (permission_id) REFERENCES permissions(id) ON DELETE CASCADE
);
