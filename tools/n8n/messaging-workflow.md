# دليل n8n — بوت مراسلات نقطة البيع

يرسل النظام رسائل عبر **Webhook** (JSON موحّد) أو **Telegram Bot API** مباشرة.
الإعداد من: `/admin/messaging`

## 1. Webhook JSON (n8n — اختياري)

فعّل **Webhook / n8n** من `/admin/messaging` → مزود WhatsApp = `webhook`.

```json
{
  "text": "نص الرسالة جاهز للإرسال",
  "phone": "0912345678",
  "channel": "whatsapp",
  "event": "loyalty.points.earned",
  "customer_id": 42,
  "meta": {}
}
```

| الحقل | الوصف |
|--------|--------|
| `text` | النص بعد استبدال المتغيرات |
| `phone` | رقم العميل أو رقم الإدارة |
| `channel` | `whatsapp` أو `telegram` |
| `event` | نوع الحدث (انظر القواعد في الأدمن) |
| `customer_id` | معرّف العميل أو `null` للإدارة |

### مثال workflow في n8n

1. **Webhook** — Method: POST، Path: `/pos-messaging`
2. **Switch** على `{{ $json.channel }}`
   - `whatsapp` → **HTTP Request** إلى CallMeBot أو واتساب Business API
   - `telegram` → **Telegram** node (أو HTTP إلى Bot API)
3. (اختياري) **Respond to Webhook** — `{ "ok": true }`

### CallMeBot / TextMeBot (WhatsApp)

**الموصى به حالياً:** `/admin/messaging` → مزود WhatsApp = **TextMeBot مباشر** (بدون n8n).

لـ n8n + CallMeBot:

```
GET https://api.callmebot.com/whatsapp.php?phone=218912345678&text=URL_ENCODED_TEXT&apikey=YOUR_KEY
```

في n8n: حوّل `phone` إلى `218` + الرقم بدون الصفر، ومرّر `text` من `$json.text`.

## 2. Telegram مباشرة من POS

إذا أدخلت **Bot Token** في `/admin/messaging`، يرسل النظام رسائل `channel=telegram` عبر:

```
POST https://api.telegram.org/bot{TOKEN}/sendMessage
```

يتطلب `telegram_chat_id` في ملف العميل (يُضاف لاحقاً من ربط البوت).

## 3. أحداث جاهزة

| الحدث | متى يُطلق |
|--------|-----------|
| `loyalty.points_earned` | بعد التحصيل — اكتساب نقاط |
| `loyalty.points_redeemed` | خصم نقاط عند الدفع |
| `loyalty.points_adjusted` | تعديل يدوي من الأدmin |
| `stock.low` | نقص مخزون → رقم الإدارة |
| `customer.first_linked` | أول موافقة عميل عند الدفع |
| `campaign.manual` | إرسال حملة من الأدmin |

## 4. اختبار

1. فعّل البوت من `/admin/messaging`
2. أدخل Webhook URL (n8n)
3. أدخل **رقم واتساب الإدارة** للاختبار
4. اضغط **إرسال اختبار**
5. راجع **سجل الإرسال** — حالة `sent` أو `failed` مع سبب الخطأ

## 5. الحملات

من `/admin/messaging/campaigns`: أنشئ حملة، اختر قالب `campaign.generic`، اكتب نص العرض في `{message}`، ثم **إرسال الآن**.
يُرسل فقط للعملاء **الموافقين** (checkbox عند الدفع).

## 6. ملاحظات

- لا يُستخدم ChatGPT في مسار الإرسال — قوالب ثابتة فقط.
- تنبيه المخزون يعمل أيضاً عبر `/admin/alerts` القديم؛ عند تفعيل البوت يُرسل `stock.low` إضافياً.
- أعد تشغيل السيرفر بعد التحديث لإنشاء الجداول والقوالب الافتراضية.

## 7. استقبال رسائل العملاء (صندوق الوارد — تفاعلي)

من `/admin/messaging` فعّل **استقبال الرسائل الواردة** واحفظ **مفتاح الأمان**.

### رابط POS

```
POST https://YOUR-POS-DOMAIN/api/messaging/inbound
Header: X-Messaging-Secret: YOUR_SECRET
Content-Type: application/json
```

```json
{
  "phone": "0912345678",
  "text": "نص رسالة العميل",
  "channel": "whatsapp",
  "external_id": "optional-unique-id-from-provider",
  "name": "اسم العميل إن وُجد"
}
```

### workflow n8n للوارد (مثال)

1. **Webhook** من CallMeBot / WhatsApp Business / وسيط آخر عند وصول رسالة
2. **HTTP Request** → POST إلى `/api/messaging/inbound` بالـ JSON أعلاه + الهيدر `X-Messaging-Secret`
3. الموظف يرد من `/admin/messaging/inbox` داخل POS

### الأدوار

| الدور | الصلاحيات |
|--------|-----------|
| **دعم فني** | عرض الصندوق + الرد |
| **مبيعات** | الرد على العملاء |
| **مدير النظام** | كل شيء + إعدادات البوت |

> **ملاحظة:** TextMeBot للإرسال فقط — للاستقبال تحتاج وسيطاً (n8n + WABA أو CallMeBot inbound إن توفر).

## 8. محادثة الزوار (صفحة /chat)

صفحة عامة بدون تسجيل دخول: `https://YOUR-POS-DOMAIN/chat`

من `/admin/messaging`:
1. فعّل **محادثة الويب**
2. احفظ **مفتاح الأمان** (نفس مفتاح استقبال الوارد)
3. (اختياري) Webhook n8n لرسائل الزوار

### تدفق الرسائل

```
زائر → POST /api/chat/send → POS
POS → POST webhook n8n (إن وُجد)
n8n + OpenAI → POST /api/chat/reply
الزائر يرى الرد عبر polling (/api/chat/messages)
```

### webhook n8n (من POS عند رسالة زائر)

```json
{
  "source": "web_chat",
  "session_token": "…",
  "session_id": 12,
  "message_id": 45,
  "text": "نص رسالة الزائر",
  "name": "",
  "phone": "",
  "department": "support"
}
```

| `department` | المعنى |
|--------------|--------|
| `support` | اختار 1 — دعم |
| `sales` | اختار 2 — مبيعات |
| `human` | اختار 3 — موظف |
| `""` | لم يختر بعد |

### رد البوت / الذكاء الاصطناعي → POS

```
POST https://YOUR-POS-DOMAIN/api/chat/reply
Header: X-Messaging-Secret: YOUR_SECRET
```

```json
{
  "session_token": "…",
  "text": "نص الرد للزائر",
  "external_id": "optional-unique-id"
}
```

### صندوق الوارد

رسائل `/chat` تظهر في `/admin/messaging/inbox` برقم `webchat:{session_id}`.
رد الموظف من الصندوق يصل للزائر مباشرة على صفحة المحادثة.

### workflow n8n (مختصر)

1. **Webhook** — استقبال payload من POS (`source=web_chat`)
2. **Switch** على `department` أو تحليل `text`
3. **OpenAI** — توليد الرد (أو تمرير لموظف إذا `human`)
4. **HTTP Request** → POST `/api/chat/reply`
5. (اختياري) إذا `human` → POST `/api/messaging/inbound` لتنبيه الفريق
