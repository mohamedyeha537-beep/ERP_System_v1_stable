# إجابات ربط Hermes بموقع pos.baytak.ly

> **ملف جاهز للإرسال لفريق Hermes** — يجيب على أسئلة التكامل ويوضّح ما هو موجود وما يُبنى.

---

## 1. تقنية الموقع — إيه؟

| البند | الجواب |
|--------|--------|
| **الإطار** | **Python 3** + **FastAPI** |
| **الواجهات** | **Jinja2** (HTML server-side) — ليست React منفصلة |
| **ORM** | SQLAlchemy |
| **السيرفر** | Uvicorn (تشغيل عبر `run.py`) |
| **النطاق** | `https://pos.baytak.ly` |

**ليست** PHP ولا Laravel ولا Django — **FastAPI** (Python حديث، سريع، مناسب لـ API).

---

## 2. قاعدة البيانات — إيه؟

| البند | الجواب |
|--------|--------|
| **الافتراضي** | **SQLite** — ملف `pos.db` على السيرفر |
| **الإنتاج** | يمكن PostgreSQL عبر متغير `DATABASE_URL` في `.env` |
| **ليست** MySQL ولا MongoDB في الإعداد الحالي |

### الجداول ذات الصلة بالعروض والمنتجات

| الجدول | المحتوى |
|--------|---------|
| `products` | المنتجات: الاسم، السعر (`sell_price`)، نشط/موقوف (`is_active`)، صورة، فئة |
| `product_categories` | فئات المنتجات (قائمة POS) |
| `message_campaigns` + `message_templates` | **حملات رسائل/عروض** للبث (واتساب/تيليجرام) — ليست «صفحة عروض» عامة |
| `web_chat_sessions` + `web_chat_messages` | محادثات صفحة `/chat` |
| `message_conversations` | صندوق الوارد (محادثات العملاء) |

> **ملاحظة:** لا يوجد جدول اسمه `offers` منفصل.  
> **العروض = إما منتجات نشطة في الكatalog، أو حملات مراسلات من لوحة الأدمن.**

---

## 3. هل يوجد API الآن؟

**نعم — جزئياً.** هذه نقاط النهاية الموجودة اليوم:

### أ) طلبات أونلاين (جاهز)

```
POST https://pos.baytak.ly/api/integration/orders
Header: X-API-Key: {INTEGRATION_API_KEY}
Content-Type: application/json
```

```json
{
  "external_order_id": "TG-12345",
  "lines": [
    { "product_id": 12, "quantity": 2 }
  ]
}
```

- يُنشئ فاتورة مكتملة في POS.
- المفتاح يُضبط في `.env` → `INTEGRATION_API_KEY`.

---

### ب) استقبال رسائل العملاء — صندوق الوارد (جاهز)

```
POST https://pos.baytak.ly/api/messaging/inbound
Header: X-Messaging-Secret: {SECRET}
```

```json
{
  "phone": "0912345678",
  "text": "نص رسالة العميل",
  "channel": "telegram",
  "name": "اسم العميل"
}
```

- يُفعَّل من `/admin/messaging`.
- مناسب لربط **Telegram/WhatsApp → POS**.

---

### ج) محادثة الويب `/chat` (جاهز)

| المسار | الوظيفة |
|--------|---------|
| `GET /chat` | صفحة محادثة عامة (بدون تسجيل دخول) |
| `POST /api/chat/session` | إنشاء جلسة |
| `POST /api/chat/send` | رسالة الزائر |
| `GET /api/chat/messages?token=&since_id=` | جلب الردود (polling) |
| `POST /api/chat/reply` | Hermes/AI يرسل الرد للزائر |

**Webhook من POS → Hermes** (عند رسالة زائر):

```json
{
  "source": "web_chat",
  "session_token": "…",
  "session_id": 12,
  "message_id": 45,
  "text": "نص الزائر",
  "name": "",
  "phone": "",
  "department": "support"
}
```

**رد Hermes → POS:**

```
POST https://pos.baytak.ly/api/chat/reply
Header: X-Messaging-Secret: {SECRET}
{"session_token": "…", "text": "نص الرد"}
```

---

### د) API عام للمنتجات/العروض JSON — **غير موجود بعد (يُبنى)**

Hermes اقترح:

```php
GET api/offers.php → JSON
```

**الم equivalent المناسب لـ POS:**

```
GET https://pos.baytak.ly/api/public/catalog
أو
GET https://pos.baytak.ly/api/hermes/catalog
Header: X-API-Key: {HERMES_API_KEY}
```

**مقترح شكل الاستجابة:**

```json
{
  "store_name": "بيتك",
  "updated_at": "2026-05-24T12:00:00Z",
  "categories": [
    {
      "id": 1,
      "name_ar": "مشروبات",
      "products": [
        {
          "id": 12,
          "name_ar": "قهوة تركية",
          "sell_price": 5.0,
          "unit": "كوب",
          "is_active": true,
          "image_url": "/static/uploads/products/abc.jpg"
        }
      ]
    }
  ],
  "campaigns": [
    {
      "id": 3,
      "name": "خصم نهاية الأسبوع",
      "message": "خصم 20% على الحلويات",
      "starts_at": "2026-05-24",
      "ends_at": "2026-05-26",
      "is_active": true
    }
  ]
}
```

> **عند إيقاف منتج (`is_active=0`) أو انتهاء حملة → يختفي من JSON → Hermes لا يعرضه في تيليجرام.**

---

## 4. الصفحة التي أُخذت منها صورة — عادية أم لوحة تحكم؟

| الصفحة | النوع |
|--------|--------|
| `/chat` | **صفحة عامة** — محادثة للزوار بدون login |
| `/admin/*` | **لوحة تحكم** — تتطلب تسجيل دخول |
| `/admin/catalog/products` | إدارة المنتجات والأسعار |
| `/admin/messaging` | إعدادات البوت + حملات العروض |
| `/admin/messaging/inbox` | رد الموظفين على العملاء |
| `/pos` | شاشة الكاشير (موظفين فقط) |

**الإدارة من لوحة التحكم — العرض للزبون من `/chat` أو Telegram عبر Hermes.**

---

## 5. الطريقة المناسبة — توصيتنا

### ✅ الطريقة 1 — API من POS (الأفضل)

```
Hermes Bot
    ↓ GET كل 5–15 دقيقة (أو عند الطلب)
https://pos.baytak.ly/api/hermes/catalog
    ↓
JSON: منتجات + حملات نشطة فقط
    ↓
ينشر في جروب Telegram / يرد في الخاص
```

**المزايا:**
- مصدر واحد للحقيقة (POS).
- إيقاف منتج من الأدمن → يختفي من البوت تلقائياً.
- لا حاجة لنسخ يدوي للأسعار.

**المطلوب من POS:** endpoint واحد read-only (سهل الإضافة — FastAPI).

---

### ✅ الطريقة 2 — Webhook ثنائي الاتجاه (مكمّل)

**POS → Hermes** (عند إضافة حملة أو تغيير سعر):

```
POST https://hermes-server/webhook/pos-update
{"event": "campaign.created", "payload": {...}}
```

**Hermes → POS** (رسالة زائر /chat أو تيليجرام):

```
POST https://pos.baytak.ly/api/chat/reply
POST https://pos.baytak.ly/api/messaging/inbound
```

**المزايا:** تحديث فوري للجروب بدون polling.

---

### ⚠️ الطريقة 3 — لوحة عروض منفصلة

**غير مطلوب** — POS فيه بالفعل:
- `/admin/catalog/products` للمنتجات
- `/admin/messaging/campaigns` للحملات

---

## 6. ما الذي يمكن لـ Hermes فعله **اليوم** بدون انتظار؟

| الميزة | جاهز؟ | كيف |
|--------|--------|-----|
| استقبال رسائل تيليجرام → POS inbox | ✅ | POST `/api/messaging/inbound` |
| رد AI على `/chat` | ✅ | webhook من POS + POST `/api/chat/reply` |
| FAQ + قائمة 1/2/3 (دعم/مبيعات/موظف) | ✅ | في `/chat` محلياً أو عبر Hermes |
| جلب المنتجات/الأسعار JSON | ❌ | يحتاج endpoint جديد (~ساعة عمل) |
| إنشاء طلب من Telegram | ⚠️ | POST `/api/integration/orders` موجود — يحتاج `product_id` |
| نشر عروض في جروب تلقائياً | ⚠️ | يحتاج API catalog + منطق Hermes |

---

## 7. أسئلة Hermes — إجابات مختصرة

| # | السؤال | الإجابة |
|---|--------|---------|
| 1 | تقنية الموقع؟ | **Python + FastAPI** |
| 2 | قاعدة البيانات؟ | **SQLite** (أو PostgreSQL في الإنتاج) |
| 3 | API موجود؟ | **نعم جزئياً** — طلبات، مراسلات، chat. **API catalog للعروض: يُبنى.** |
| 4 | الصفحة صورة منها؟ | **`/chat` عامة** + **`/admin` لوحة تحكم** |

---

## 8. مثال كود Hermes (Python) — جلب catalog (بعد إضافة API)

```python
import requests

POS_URL = "https://pos.baytak.ly"
API_KEY = "your-hermes-api-key"

def fetch_catalog():
    r = requests.get(
        f"{POS_URL}/api/hermes/catalog",
        headers={"X-API-Key": API_KEY},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()

def format_offers_for_telegram(data):
    lines = [f"🛒 {data['store_name']}", ""]
    for cat in data.get("categories", []):
        lines.append(f"📂 {cat['name_ar']}")
        for p in cat.get("products", []):
            if p.get("is_active"):
                lines.append(f"  • {p['name_ar']} — {p['sell_price']} د.ل")
        lines.append("")
    for camp in data.get("campaigns", []):
        if camp.get("is_active"):
            lines.append(f"🎁 {camp['name']}: {camp['message']}")
    return "\n".join(lines)
```

---

## 9. مثال كود Hermes — رد على `/chat`

```python
def reply_to_web_chat(session_token: str, text: str):
    requests.post(
        f"{POS_URL}/api/chat/reply",
        headers={
            "X-Messaging-Secret": MESSAGING_SECRET,
            "Content-Type": "application/json",
        },
        json={"session_token": session_token, "text": text},
        timeout=15,
    )
```

---

## 10. الخطوة التالية المقترحة

1. **POS:** إضافة `GET /api/hermes/catalog` (read-only، مفتاح API).
2. **Hermes:** webhook listener لرسائل `/chat` + استدعاء OpenAI + `/api/chat/reply`.
3. **Hermes:** cron كل 10 دقائق لـ `GET /api/hermes/catalog` → نشر في الجروب.
4. **(اختياري)** webhook من POS عند تغيير منتج/حملة → تحديث فوري.

---

## 11. جهات الاتصال التقنية

| الإعداد | أين |
|---------|-----|
| مفتاح API للتكامل | `.env` → `INTEGRATION_API_KEY` |
| مفتاح مراسلات / chat | `/admin/messaging` → Secret |
| تفعيل `/chat` | `/admin/messaging` → محادثة الويب |
| webhook Hermes | `/admin/messaging` → Webhook n8n (أي URL) |

---

*آخر تحديث: مايو 2026 — مشروع POS FastAPI — pos.baytak.ly*
