# MySQL على VPS — دليل النشر

## 1) تثبيت MySQL على السيرفر

```bash
sudo apt update
sudo apt install -y mysql-server mysql-client
sudo mysql <<'SQL'
CREATE DATABASE pos_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'pos_user'@'localhost' IDENTIFIED BY 'STRONG_PASSWORD_HERE';
GRANT ALL PRIVILEGES ON pos_db.* TO 'pos_user'@'localhost';
FLUSH PRIVILEGES;
SQL
```

## 2) إعداد `.env`

```env
DATABASE_URL=mysql+pymysql://pos_user:STRONG_PASSWORD_HERE@127.0.0.1:3306/pos_db?charset=utf8mb4
SECRET_KEY=سلسلة-عشوائية-طويلة
DEFAULT_ADMIN_PASSWORD=كلمة-مرور-قوية
SEED_DEMO_USERS=false
POS_RELOAD=0
```

## 3) تثبيت الحزم

```bash
cd /var/www/pos
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

## 4) نقل البيانات من SQLite

**مهم:** في `.env` على VPS ضع `DATABASE_URL` لـ MySQL **قبل** تشغيل السكربت.

```bash
# معاينة
python tools/migrate_sqlite_to_mysql.py --dry-run

# تنفيذ (لا يحذف pos.db)
python tools/migrate_sqlite_to_mysql.py --force
```

**صور المنتجات:** انقل مجلد الصور مع قاعدة البيانات:

```bash
# من جهازك (Windows) — عدّل المسارات
scp -r app/static/uploads/products/ user@vps:/home/posbaytak/pos_app/app/static/uploads/
```

الروابط في الواجهة تستخدم `/uploads/products/…` (تمر عبر التطبيق) — لا تعتمد على `alias` ثابت في nginx.

## 5) تشغيل التطبيق

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8011
```

للتشغيل الدائم: استخدم `deploy/pos.service.example` (عدّل `After=mysql.service`).

## 6) Nginx

```bash
sudo cp deploy/nginx-pos.conf.example /etc/nginx/sites-available/pos
sudo ln -s /etc/nginx/sites-available/pos /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## 7) النسخ الاحتياطي

- من الواجهة: **الإعدادات → النسخ الاحتياطي** (ملف `.sql` عبر `mysqldump`)
- يدوياً:

```bash
mysqldump -u pos_user -p --single-transaction pos_db > backups/manual.sql
```

## 8) الاستعادة

النسخ الحالية تصل ~90MB. إن بقي nginx على `client_max_body_size 32M` يفشل الرفع بـ 413 قبل أن يصل التطبيق.

على السيرفر (مرة واحدة):

```bash
# في ملف nginx للموقع:
#   client_max_body_size 256M;
#   proxy_read_timeout 1800s;
#   proxy_send_timeout 1800s;
sudo nginx -t && sudo systemctl reload nginx

# عميل MySQL إن لم يكن مثبتاً:
sudo apt update && sudo apt install -y mysql-client
# أو: sudo apt install -y mariadb-client
which mysql && which mysqldump
```

- من الواجهة: ارفع ملف `.sql` / `.sql.gz` — أو اضغط «استعادة» بجانب نسخة موجودة في الجدول (بدون رفع).
- يدوياً:

```bash
mysql -u pos_user -p --default-character-set=utf8mb4 --max-allowed-packet=512M pos_db < backups/pos-backup-....sql
```

## 9) تحقق

```bash
python -c "from infra.database import database_kind; print(database_kind())"
# يطبع: mysql
```

## ملاحظات

- لا ترفع `pos.db` على OneDrive للإنتاج.
- **محو الحركات** و**محو كل البيانات** يعملان على MySQL.
- **تصفير المصنع** (حذف ملف) لـ SQLite فقط — على MySQL أنشئ قاعدة جديدة يدوياً.
- تأكد من وجود `mysqldump` و`mysql` على PATH للسيرفر.
