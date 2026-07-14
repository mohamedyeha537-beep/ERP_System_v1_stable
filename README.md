# ERP System

نظام ERP متكامل لإدارة المطاعم والفنادق.

## المتطلبات

- Python 3.10+
- **MySQL / MariaDB** (قاعدة التشغيل المعتمدة محلياً وفي الإنتاج)
- pip

> SQLite ليس مسار التشغيل المعتمد. ملفات مثل `infra/sqlite_patch.py` للتوافق القديم فقط ولا تُبنى عليها تعديلات جديدة.

## التثبيت السريع

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# تأكد أن DATABASE_URL يشير إلى MySQL
python run.py
```

افتح المتصفح على `http://127.0.0.1:8011/`

بيانات الدخول الافتراضية (إن وُجدت في `.env`):
- المستخدم: `admin`
- كلمة المرور: حسب `DEFAULT_ADMIN_PASSWORD` في `.env`

## البنية

- `app/`: نقطة دخول FastAPI والقوالب والـ static
- `modules/`: الموديولات (محاسبة، مخزون، مبيعات، فندق، HR...)
- `infra/`: الإعدادات وقاعدة البيانات وتصحيح مخطط **MySQL** (`server_schema_patch.py`)
- `alembic/`: ترقيات قاعدة البيانات
- `tools/`: سكربتات مساعدة

## الموديولات المحاسبية

- `modules/gl`: دليل الحسابات، قيود اليومية، ميزان المراجعة، قائمة الدخل، الميزانية
- `modules/payments`: محافظ وطرق دفع
- `modules/receivables` / `modules/payables`: الذمم

## ملاحظات الأمان

- غيّر `SECRET_KEY` وكلمات المرور الافتراضية في الإنتاج
- لا ترفع `.env` إلى Git
- استخدم HTTPS مع Secure cookies في الإنتاج

## التطوير

```bash
python -m pytest
python run.py
```

## أنشطة متعددة (مقترح)

لفرع معماري مختلف (عيادة، صيدلية...) انظر `dev/v2-erp-core` — منفصل عن مسار MySQL الحالي.
