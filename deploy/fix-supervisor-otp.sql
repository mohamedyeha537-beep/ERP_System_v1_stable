-- OTP اعتماد الاسترداد (مشرف واتساب)
-- نفّذ على MySQL ثم: sudo systemctl restart pos
-- تجاهل Duplicate table إن ظهر.

CREATE TABLE IF NOT EXISTS supervisor_otp_challenges (
  id INT AUTO_INCREMENT PRIMARY KEY,
  purpose VARCHAR(40) NOT NULL,
  domain VARCHAR(20) NOT NULL,
  ref_type VARCHAR(20) NOT NULL,
  ref_id INT NOT NULL,
  code_hash VARCHAR(128) NOT NULL,
  expires_at DATETIME NOT NULL,
  consumed_at DATETIME NULL,
  attempts INT NOT NULL DEFAULT 0,
  requested_by_user_id INT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX ix_supervisor_otp_purpose (purpose),
  INDEX ix_supervisor_otp_domain (domain),
  INDEX ix_supervisor_otp_ref (ref_type, ref_id),
  INDEX ix_supervisor_otp_expires (expires_at)
);
