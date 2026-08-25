-- تخصيص واتساب لإشعار معيّن (أرقام متعددة + نص الرسالة)
-- نفّذ على MySQL عبر phpMyAdmin ثم أعد تشغيل الخدمة
-- تجاهل: Table already exists

CREATE TABLE IF NOT EXISTS activity_hub_wa_overrides (
  event_key VARCHAR(64) NOT NULL PRIMARY KEY,
  phones VARCHAR(255) NOT NULL DEFAULT '',
  message_body TEXT NULL,
  updated_at DATETIME(6) NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
