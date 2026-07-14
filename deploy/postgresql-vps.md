# PostgreSQL على VPS — دليل النشر

هذا الدليل ينقل المنظومة من **SQLite** (ملف `pos.db`) إلى **PostgreSQL** على سيرفر Linux (VPS).

---

## 1) لماذا PostgreSQL؟

| SQLite | PostgreSQL |
|--------|------------|
| ملف واحد — مناسب للتجربة المحلية | خادم قاعدة بيانات — مناسب للإنتاج |
| بطء مع عدة مستخدمين متزامنين | أداء أفضل مع POS + KDS + تقارير |
| نسخ الملف يدوياً | نسخ احتياطي `pg_dump` + است replication |

**التوصية:** SQLite على جهازك للتطوير، PostgreSQL على VPS للإنتاج (`pos.baytak.ly`).

---

## 2) ما الذي جهّزه المشروع؟

- دعم `DATABASE_URL` لـ PostgreSQL في `.env`
- حزمة `psycopg2-binary` في `requirements.txt`
- تجميع اتصالات (connection pool) تلقائياً على PostgreSQL
- سكربت نقل البيانات: `tools/migrate_sqlite_to_postgres.py`
- نسخ احتياطي عبر `pg_dump` من صفحة الإدارة
- `sqlite_patch.py` يعمل **فقط** مع SQLite — على Postgres يُستخدم `Base.metadata.create_all`

---

## 3) على VPS — تثبيت PostgreSQL

```bash
sudo apt update
sudo apt install -y postgresql postgresql-contrib

sudo -u postgres psql <<'SQL'
CREATE USER pos_user WITH PASSWORD 'STRONG_PASSWORD_HERE';
CREATE DATABASE pos_db OWNER pos_user;
GRANT ALL PRIVILEGES ON DATABASE pos_db TO pos_user;
SQL
```

---

## 4) رفع المشروع وإعداد `.env`

```bash
cd /var/www/pos
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt

cp .env.example .env
nano .env
```

**أهم سطر في `.env` للإنتاج:**

```env
DATABASE_URL=postgresql+psycopg2://pos_user:STRONG_PASSWORD_HERE@127.0.0.1:5432/pos_db
SECRET_KEY=سلسلة-عشوائية-طويلة
DEFAULT_ADMIN_PASSWORD=كلمة-مرور-قوية
SEED_DEMO_USERS=false
POS_RELOAD=0
```

---

## 5) نقل البيانات من SQLite

### أ) على جهازك (قبل الرفع)

1. ثبّت PostgreSQL محلياً أو أنشئ قاعدة على VPS.
2. عدّل `.env` مؤقتاً إلى عنوان Postgres.
3. نفّذ:

```bash
python tools/migrate_sqlite_to_postgres.py --source sqlite:///./pos.db --force
```

4. تحقّق من الدخول والبيانات.
5. ارفع الملفات إلى VPS (لا حاجة لرفع `pos.db` بعد النقل).

### ب) على VPS (بعد رفع `pos.db`)

```bash
# DATABASE_URL في .env يشير إلى Postgres
python tools/migrate_sqlite_to_postgres.py --source sqlite:///./pos.db --force
```

---

## 6) تشغيل التطبيق كخدمة (systemd)

انسخ `deploy/pos.service.example` إلى `/etc/systemd/system/pos.service` وعدّل المسارات:

```bash
sudo cp deploy/pos.service.example /etc/systemd/system/pos.service
sudo nano /etc/systemd/system/pos.service
sudo systemctl daemon-reload
sudo systemctl enable pos
sudo systemctl start pos
sudo systemctl status pos
```

---

## 7) Nginx كـ reverse proxy

```bash
sudo apt install -y nginx
sudo cp deploy/nginx-pos.conf.example /etc/nginx/sites-available/pos
sudo ln -s /etc/nginx/sites-available/pos /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

---

## 8) النسخ الاحتياطي على PostgreSQL

- من الواجهة: **الإعدادات → النسخ الاحتياطي → إنشاء نسخة** (يستخدم `pg_dump`).
- يدوياً على السيرفر:

```bash
pg_dump -h 127.0.0.1 -U pos_user -Fc -f backups/manual.dump pos_db
```

---

## 9) ملاحظات مهمة

1. **OneDrive:** لا تضع `pos.db` على OneDrive في الإنتاج — يسبب بطءاً وتعليقاً.
2. **الملفات المرفوعة** (`app/static/uploads/`) تبقى على القرص — انسخها مع المشروع.
3. **محو الحركات / التصفير** يعمل على PostgreSQL عبر SQL (ليس حذف ملف).
4. **تصفير المصنع** (حذف ملف SQLite) غير متاح على Postgres — أنشئ قاعدة جديدة يدوياً.

---

## 10) التحقق السريع

```bash
source .venv/bin/activate
python -c "from infra.database import database_kind; print(database_kind())"
# يجب أن يطبع: postgresql
```

افتح الموقع وتأكد من: تسجيل الدخول، POS، التقارير، الخزينة.
