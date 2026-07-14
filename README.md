# ERP System

نظام ERP متكامل لإدارة المطاعم، الفنادق، ويمتد لأنشطة تجارية متعددة.

## المتطلبات

- Python 3.10+
- قاعدة بيانات: SQLite (للتطوير) أو MySQL/MariaDB/PostgreSQL (للإنتاج)
- pip

## التثبيت السريع

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# عدّل .env حسب قاعدة البيانات
python run.py
```

افتح المتصفح على `http://127.0.0.1:8011/`

بيانات الدخول الافتراضية:
- المستخدم: `admin`
- كلمة المرور: `admin123`

## البنية

- `app/`: نقطة دخول FastAPI والقوالب والـ static.
- `modules/`: كل موديول مستقل (محاسبة، مخزون، مبيعات، فندق، HR...).
- `infra/`: قاعدة البيانات، الإعدادات، وتصحيح مخطط SQLite.
- `alembic/`: ترقيات قاعدة البيانات.
- `tools/`: سكربتات مساعدة خارجية.

## الموديولات المحاسبية

- `modules/gl`: دليل الحسابات، قيود اليومية، ميزان المراجعة، قائمة الدخل، الميزانية.
- `modules/payments`: محافظ وطرق دفع (كاش، بنك، آجل...).
- `modules/receivables` / `modules/payables`: الذمم المدينة والدائنة.

## ملاحظات الأمان

- غيّر `SECRET_KEY` وكلمات المرور الافتراضية في الإنتاج.
- لا ترفع `.env` أو `*.db` إلى Git (`gitignore` معد مسبقاً).
- استخدم HTTPS مع SameSite/Secure cookies في الإنتاج.

## التطوير

للاختبار السريع:

```bash
python -m pytest
# أو
source .venv/bin/activate && python run.py
```

## الأنشطة التجارية

للنسخة المقترحة لأنشطة متعددة (ملابس، نظارات، عيادة، صيدلية...) انظر فرع `dev/v2-erp-core`.
