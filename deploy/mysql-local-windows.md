# MySQL محلياً على Windows (مثل VPS)

**بدون Docker (موصى به):** [mysql-xampp-windows.md](mysql-xampp-windows.md) — `setup-mysql-xampp.bat`

**مع Docker:** [mysql-local-windows.md](mysql-local-windows.md) — `setup-mysql-local.bat`

## المتطلبات (XAMPP)

1. **Docker Desktop** — [تحميل](https://www.docker.com/products/docker-desktop/)
2. **Python 3.13+** — كما في `install-requirements.bat`

## الطريقة السريعة (موصى بها)

1. شغّل **Docker Desktop** وانتظر حتى يصبح جاهزاً.
2. من مجلد المشروع نفّذ:

```bat
setup-mysql-local.bat
```

يفعل تلقائياً:
- تشغيل MySQL في Docker (`docker-compose.yml`)
- تحديث `DATABASE_URL` في `.env` (مع نسخة احتياطية `.env.bak-mysql`)
- نقل بيانات `pos.db` إلى MySQL (**لا يحذف** ملف SQLite)
- اختبار الاتصال

3. شغّل التطبيق:

```bat
start-server.bat
```

## إعدادات MySQL المحلي (Docker)

| البند | القيمة |
|--------|--------|
| Host | `127.0.0.1:3306` |
| Database | `pos_db` |
| User | `pos_user` |
| Password | `pos_local_dev` |
| Root password | `pos_root_local` |

```env
DATABASE_URL=mysql+pymysql://pos_user:pos_local_dev@127.0.0.1:3306/pos_db?charset=utf8mb4
```

## أوامر يدوية

```bat
docker compose up -d
python tools/setup_mysql_local.py --write-env
python tools/migrate_sqlite_to_mysql.py --dry-run
python tools/migrate_sqlite_to_mysql.py --force
python tools/setup_mysql_local.py --verify
```

## العودة إلى SQLite

1. عدّل `.env`:
   ```env
   DATABASE_URL=sqlite:///./pos.db
   ```
2. أعد تشغيل `start-server.bat`
3. (اختياري) أوقف MySQL: `docker compose down`

## MySQL مثبت على Windows بدون Docker

1. ثبّت [MySQL Community Server](https://dev.mysql.com/downloads/installer/)
2. أنشئ قاعدة `pos_db` ومستخدم `pos_user`
3. ضع الرابط في `.env`
4. نفّذ: `python tools/migrate_sqlite_to_mysql.py --force`

## أوامر Docker مفيدة

```bat
docker compose ps
docker compose logs mysql
docker compose down
docker compose down -v
```

`down -v` **يمحو** بيانات MySQL المحلية — استخدمه بحذر.

## ملاحظات

- ملف **`pos.db`** يبقى كنسخة احتياطية بعد النقل.
- على VPS استخدم `deploy/mysql-vps.md` — نفس `DATABASE_URL` بصيغة `mysql+pymysql://…`
- النسخ الاحتياطي من الواجهة يحتاج `mysqldump` على PATH (داخل Docker أو MySQL client).
