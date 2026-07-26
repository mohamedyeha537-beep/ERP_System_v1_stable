-- إصلاح جداول فاتورة/دين المغادرة على MySQL (pos.baytak.ly)
-- نفّذ من phpMyAdmin ثم: sudo systemctl restart pos
-- تجاهل أخطاء Duplicate table/column

CREATE TABLE IF NOT EXISTS hotel_invoices (
  id INT AUTO_INCREMENT PRIMARY KEY,
  booking_id INT NOT NULL,
  invoice_number VARCHAR(32) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'DRAFT',
  subtotal DECIMAL(14,3) NOT NULL DEFAULT 0,
  discount DECIMAL(14,3) NOT NULL DEFAULT 0,
  tax DECIMAL(14,3) NOT NULL DEFAULT 0,
  total DECIMAL(14,3) NOT NULL DEFAULT 0,
  paid DECIMAL(14,3) NOT NULL DEFAULT 0,
  issued_at DATETIME NULL,
  issued_by_id INT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX ix_hotel_invoices_booking_id (booking_id),
  INDEX ix_hotel_invoices_invoice_number (invoice_number),
  INDEX ix_hotel_invoices_status (status)
);

CREATE TABLE IF NOT EXISTS hotel_invoice_items (
  id INT AUTO_INCREMENT PRIMARY KEY,
  invoice_id INT NOT NULL,
  description VARCHAR(255) NOT NULL,
  quantity DECIMAL(14,4) NOT NULL DEFAULT 1,
  unit_price DECIMAL(14,3) NOT NULL DEFAULT 0,
  line_total DECIMAL(14,3) NOT NULL DEFAULT 0,
  item_type VARCHAR(40) NOT NULL DEFAULT 'OTHER',
  INDEX ix_hotel_invoice_items_invoice_id (invoice_id)
);

CREATE TABLE IF NOT EXISTS hotel_booking_debts (
  id INT AUTO_INCREMENT PRIMARY KEY,
  booking_id INT NOT NULL,
  amount DECIMAL(14,3) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
  reason TEXT NULL,
  settlement_json TEXT NULL,
  created_by_id INT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  collected_at DATETIME NULL,
  collected_by_id INT NULL,
  collection_payment_id INT NULL,
  written_off_at DATETIME NULL,
  written_off_by_id INT NULL,
  write_off_reason TEXT NULL,
  INDEX ix_hotel_booking_debts_booking_id (booking_id),
  INDEX ix_hotel_booking_debts_status (status)
);

ALTER TABLE hotel_bookings ADD COLUMN final_invoice_number VARCHAR(32) NULL;
ALTER TABLE hotel_bookings ADD COLUMN scheduled_check_out DATE NULL;
ALTER TABLE hotel_booking_payments ADD COLUMN receipt_number VARCHAR(32) NULL;
ALTER TABLE sales ADD COLUMN receipt_number VARCHAR(32) NULL;
ALTER TABLE sales ADD COLUMN final_invoice_number VARCHAR(32) NULL;

-- مطلوب لسجل المدفوعات/الإرجاعات في صفحة إيصال الحجز
CREATE TABLE IF NOT EXISTS hotel_booking_payment_refunds (
  id INT AUTO_INCREMENT PRIMARY KEY,
  payment_id INT NOT NULL,
  amount DECIMAL(14,3) NOT NULL,
  payment_method_id INT NULL,
  reason TEXT NULL,
  approved_by_id INT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX ix_hotel_booking_payment_refunds_payment_id (payment_id),
  INDEX ix_hotel_booking_payment_refunds_payment_method_id (payment_method_id)
);
