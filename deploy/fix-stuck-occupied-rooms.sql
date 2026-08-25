-- إصلاح شقق عالقة بحالة OCCUPIED بلا نزيل مسكّن (CHECKED_IN)
-- نفّذ على MySQL (phpMyAdmin) إن بقيت شقة «مشغولة» بعد مغادرة مسجّلة.
-- آمن: لا يمس شقة عليها حجز مسكّن حالياً.

UPDATE hotel_rooms r
SET
  r.physical_status = 'DIRTY',
  r.guest_name = NULL
WHERE r.physical_status = 'OCCUPIED'
  AND NOT EXISTS (
    SELECT 1
    FROM hotel_bookings b
    WHERE b.room_id = r.id
      AND b.booking_status = 'CHECKED_IN'
  );

-- تحقق سريع بعد التنفيذ:
-- SELECT id, number, physical_status, guest_name FROM hotel_rooms WHERE physical_status IN ('OCCUPIED','DIRTY');
