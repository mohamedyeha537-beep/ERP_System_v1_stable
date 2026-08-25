-- ترجيع مبلغ النزيل (مغادرة / إيصال صرف) بدون رمز مشرف
-- نفّذ على MySQL ثم: sudo systemctl restart pos

INSERT INTO app_settings (`key`, value)
VALUES ('otp_require_hotel_refund', '0')
ON DUPLICATE KEY UPDATE value = '0';
