-- خزائن نقطة البيع الظاهرة لكل مستخدم (كاش / مصرف)
-- إن ظهر خطأ «Duplicate column» فالأعمدة موجودة مسبقاً — تجاهله.
-- التطبيق يضيف الأعمدة تلقائياً أيضاً عبر server_schema_patch / sqlite_patch.

ALTER TABLE users ADD COLUMN pos_show_cash TINYINT(1) NOT NULL DEFAULT 1;
ALTER TABLE users ADD COLUMN pos_show_bank TINYINT(1) NOT NULL DEFAULT 1;
