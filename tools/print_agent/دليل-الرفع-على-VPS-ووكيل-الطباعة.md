# دليل الرفع على VPS وتشغيل وكيل الطباعة

هذا الملف يشرح كيف يعمل النظام بعد نقل **نقطة البيع (POS)** إلى سيرفر VPS، وكيف تُشغَّل **الطباعة** كما كانت على جهازك المحلي.

---

## 1) الفكرة المهمة

**وكيل الطباعة لا يُشغَّل عادةً على VPS السحابي** إذا كانت الطابعات على شبكة المطعm (`192.168.1.100` …).

| المكوّن | أين يعمل؟ |
|---------|-----------|
| **نظام POS (السيرفر)** | VPS (Hostinger / السحابة) |
| **وكيل الطباعة** | **كمبيوتر داخل المطعm** (كاشير أو مطبخ) |
| **الطابعات** | شبكة المطعm المحلية |

السيرفر ينشئ مهام الطباعة → الوكيل **يسحبها عبر الإنترنت** → يطبع على الطابعات المحلية.

```
[VPS] POS  ←── إنترنت ──→  [PC المطعm] وكيل الطباعة  ←── LAN ──→  الطابعات
```

هذا **نفس ما كنت تفعله محلياً**، لكن `server_url` في `config.json` يصبح رابط VPS بدلاً من `http://127.0.0.1:8011`.

---

## 2) بعد رفع POS على VPS

### على VPS — شغّل POS فقط

```bash
cd /path/to/pos
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python run.py
```

للإنتاج يُفضَّل **systemd** أو **uvicorn** خلف **nginx** — وليس نافذة طرفية مفتوحة.

### في المطعm — شغّل الوكيل (Windows كما الآن)

على **نفس كمبيوتر الكاشير** (أو أي PC على شبكة الطابعات):

1. انسخ مجلد `tools/print_agent/` (أو المشروع كاملاً).
2. عدّل `config.json`:

```json
{
  "server_url": "https://your-domain.com",
  "agent_token": "الرمز_من_/admin/printing/agents",
  "poll_interval_seconds": 2,
  "printers": {
    "cashier": { "host": "192.168.1.100", "port": 9100 },
    "grill":   { "host": "192.168.1.101", "port": 9100 },
    "drinks":  { "host": "192.168.1.103", "port": 9100 }
  }
}
```

3. شغّل كما على جهازك:

```bat
start-print-agent.bat
```

أو دبل كليك على الملف — **اترك النافذة مفتوحة** أثناء دوام المطعm.

---

## 3) إعداد لوحة الإدارة (على VPS)

1. **`/admin/printing/agents`** — أنشئ وكيلاً وانسخ `agent_token` إلى `config.json`.
2. **`/admin/printing/printers`** — نوع الاتصال: `local_agent`، و`local_printer_key` يطابق مفاتيح `printers` في `config.json`.
3. **`/admin/printing/jobs`** — راقب المهام (`pending` → `printed` أو `failed`).

تفاصيل أكثر: `tools/print_agent/دليل-وكيل-الطباعة-المحلي.md`

---

## 4) التشغيل التلقائي على Windows (بدون دبل كليك كل يوم)

### الطريقة السريعة — مجلد Startup

1. اضغط `Win + R` → اكتب `shell:startup`
2. ضع اختصاراً لـ `start-print-agent.bat`

أو أنشئ ملف `تشغيل-وكيل-الطباعة.bat`:

```bat
@echo off
cd /d "C:\path\to\pos\tools\print_agent"
python print_agent.py
```

### الطريقة الأفضل — خدمة Windows (NSSM)

1. حمّل [NSSM](https://nssm.cc/download)
2. ثبّت الخدمة (عدّل المسارات):

```bat
nssm install POS-PrintAgent "C:\Users\...\Python313\python.exe" "C:\...\pos\tools\print_agent\print_agent.py"
nssm set POS-PrintAgent AppDirectory "C:\...\pos\tools\print_agent"
nssm start POS-PrintAgent
```

تبدأ تلقائياً مع Windows.

---

## 5) متى يُشغَّل الوكيل على VPS نفسه؟

**فقط** إذا كان VPS **داخل المطعm** ويرى الطابعات (نفس شبكة `192.168.x.x`) — نادر مع VPS سحابي.

على Linux (systemd):

```ini
# /etc/systemd/system/pos-print-agent.service
[Unit]
Description=POS Print Agent
After=network.target

[Service]
WorkingDirectory=/opt/pos/tools/print_agent
ExecStart=/opt/pos/venv/bin/python print_agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pos-print-agent
sudo systemctl status pos-print-agent
```

---

## 6) VPS سحابي + طابعات محلية — لا تضع الوكيل على VPS

السيرفر السحابي **لا يستطيع** الوصول مباشرة إلى `192.168.1.x` داخل المطعm.

| ❌ خطأ شائع | ✅ الصحيح |
|-------------|-----------|
| تشغيل الوكيل على VPS السحابي | تشغيل الوكيل على PC داخل المطعm |
| توقع أن VPS يطبع على IP محلي | PC المطعm يسحب المهام ويطبع محلياً |

**حل متقدّم (اختياري):** VPN مثل Tailscale أو WireGuard يربط VPS بشبكة المطعm — غير ضروري في أغلب الحالات.

---

## 7) قائمة تحقق بعد الرفع

```
□ POS يعمل على VPS (https://your-domain.com)
□ وكيل من /admin/printing/agents + حفظ agent_token
□ config.json على PC المطعm → server_url = رابط VPS
□ الطابعات في /admin/printing/printers (local_agent + local_printer_key)
□ start-print-agent.bat يعمل على PC المطعm
□ اختبار طباعة → الحالة printed في /admin/printing/jobs
□ إرسال طلب للمطبخ → طباعة على الطابعة الصحيحة
```

---

## 8) استكشاف الأخطاء

| المشكلة | السبب | الحل |
|---------|--------|------|
| المهام `pending` | الوكيل متوقف | شغّل `start-print-agent.bat` على PC المطعm |
| `401` | token خطأ | أنشئ وكيلاً جديداً وحدّث `config.json` |
| `failed` | IP خطأ أو طابعة مطفأة | `Test-NetConnection 192.168.1.100 -Port 9100` |
| لا يطبع من VPS | VPS بعيد عن LAN | انقل الوكيل إلى PC المطعm |
| `server_url` خطأ | رابط قديم محلي | غيّر إلى `https://your-domain.com` |

السجل: `tools/print_agent/print_agent.log`

---

## 9) الخلاصة

| على VPS | في المطعm (Windows) |
|---------|---------------------|
| POS + قاعدة البيانات | `start-print-agent.bat` |
| `/admin/printing/*` | `config.json` + IP الطابعات |

**لا تنقل الوكيل إلى VPS السحابي** ما لم تربط VPS بشبكة المطعm عبر VPN.

---

## روابط ذات صلة

| الملف / الصفحة | الغرض |
|----------------|--------|
| `tools/print_agent/دليل-وكيل-الطباعة-المحلي.md` | إعداد تفصيلي للوكيل |
| `tools/print_agent/دليل-ثلاث-طابعات-وتوزيع-الطلب.md` | توزيع أقسام المطبخ |
| `start-print-agent.bat` | تشغيل سريع على Windows |
| `/admin/printing` | مركز الطباعة |

---

*آخر تحديث: دليل الرفع على VPS — وكيل الطباعة يبقى داخل المطعm.*
