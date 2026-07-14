# MySQL عبر XAMPP على Windows (بدون Docker)

الطريقة الموصى بها إذا **لا تريد Docker** — XAMPP خفيف وMySQL جاهز.

## 1) تشغيل MySQL في XAMPP

1. افتح **XAMPP Control Panel**
2. اضغط **Start** بجانب **MySQL** (يصبح الخلفية خضراء)
3. (اختياري) **Start** بجانب **Apache** — ليس مطلوباً لتشغيل POS، فقط لـ phpMyAdmin

## 2) نقل البيانات تلقائياً

من مجلد المشروع:

```bat
setup-mysql-xampp.bat
```

يفعل:
- إنشاء قاعدة **`pos_db`**
- إنشاء مستخدم **`pos_user`** / `pos_local_dev` (مثل VPS)
- تحديث **`DATABASE_URL`** في `.env` (نسخة احتياطية `.env.bak-mysql`)
- نقل **`pos.db`** → MySQL (**لا يحذف** SQLite)

## 3) تشغيل التطبيق

```bat
start-server.bat
```

## إعداد `.env` يدوياً (إن أردت)

```env
DATABASE_URL=mysql+pymysql://pos_user:pos_local_dev@127.0.0.1:3306/pos_db?charset=utf8mb4
```

أو بـ **root** بدون كلمة (XAMPP الافتراضي):

```env
DATABASE_URL=mysql+pymysql://root@127.0.0.1:3306/pos_db?charset=utf8mb4
```

## phpMyAdmin

- الرابط: http://localhost/phpmyadmin
- المستخدم: `root`
- كلمة المرور: فارغة (افتراضي XAMPP)

## أوامر يدوية

```bat
python tools/setup_mysql_xampp.py --all
python tools/setup_mysql_xampp.py --all --root-password "your_root_pass"
python tools/setup_mysql_xampp.py --all --use-root
python tools/migrate_sqlite_to_mysql.py --dry-run
python tools/migrate_sqlite_to_mysql.py --force
```

## استيراد نسخة السيرفر الفعلي (بيانات live)

إذا أخذت dump من phpMyAdmin على VPS وتريد نفس البيانات محلياً:

```bat
import-live-backup.bat
```

أو:

```bat
python tools/import_live_mysql_backup.py --all --source "C:\path\to\backup.sql"
```

يفعل:
- تهيئة الملف (إصلاح `physical_status`، استبدال `utf8mb4_0900_ai_ci` لـ XAMPP)
- حفظ نسخة مُهيّأة في `deploy/backups/prepared-live-backup-*.sql`
- إعادة إنشاء `pos_db` واستيراد البيانات
- إضافة أعمدة ناقصة (`sales.booking_id`، …) عبر ترقية التطبيق

**تحذير:** `--all` يحذف بيانات `pos_db` المحلية الحالية ويستبدلها بنسخة السيرفر.

## مشاكل شائعة

| المشكلة | الحل |
|---------|------|
| MySQL لا يبدأ في XAMPP | منفذ **3306** مشغول — أوقف Skype/MySQL آخر أو غيّر منفذ XAMPP |
| Access denied for root | استخدم `--root-password` إذا غيّرت كلمة root |
| لا يوجد pos.db | النقل يحتاج ملف SQLite موجوداً في مجلد المشروع |

## العودة إلى SQLite

```env
DATABASE_URL=sqlite:///./pos.db
```

## مقارنة مع VPS

| | XAMPP محلي | VPS |
|--|------------|-----|
| الرابط | `127.0.0.1:3306` | `127.0.0.1:3306` |
| المستخدم | `pos_user` | `pos_user` |
| الصيغة | `mysql+pymysql://…?charset=utf8mb4` | نفسها |

## Docker

إذا رغبت لاحقاً: `setup-mysql-local.bat` — راجع `deploy/mysql-local-windows.md`
