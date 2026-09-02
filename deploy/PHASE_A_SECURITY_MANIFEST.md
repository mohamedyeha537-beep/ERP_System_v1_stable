# Phase A / A.5 — Security Manifest

عزل تغييرات الأمان للمراجعة أو commit منفصل لاحقًا.
**لا يشمل** ميزات منتج / SEO / تسويق وظيفي / skills.

تاريخ الإعداد: 2026-09-01

---

## 1) ملفات Security Phase A + A.5 (للـcommit الأمني)

### أساسية — منطق الحماية

| المسار | حالة git | الغرض |
|--------|----------|--------|
| `app/upload_authorization.py` | untracked | ACL مورد لـ `/uploads` (صلاحية + ربط DB) |
| `app/uploads_router.py` | modified | تطبيق ACL على التحميل |
| `app/csrf.py` | modified | CSRF Pure ASGI؛ login معفى؛ machine APIs منفصلة |
| `modules/common/safe_http_url.py` | modified | فلتر SSRF مركزي (private/loopback/metadata/IPv4-mapped) |
| `modules/common/safe_http.py` | untracked | `open_safe_http` + رفض redirect داخلي |
| `modules/authz/pos_wallet_access.py` | modified | Wallet ACL fail-closed عند الخطأ |
| `modules/messaging/web_chat_service.py` | modified | n8n عبر safe HTTP |
| `modules/messaging/providers/textmebot.py` | modified | TextMeBot عبر safe HTTP |
| `modules/messaging/providers/telegram.py` | modified | Telegram عبر safe HTTP |
| `modules/messaging/hermes_ai.py` | modified | Hermes AI عبر safe HTTP |
| `modules/kds/service.py` | modified | KDS WhatsApp webhook عبر safe HTTP |
| `modules/marketing_room/ai_client.py` | modified (A.5) | كل urlopen → open_safe_http |
| `modules/marketing_room/pipeline.py` | modified (A.5) | AI/n8n عبر safe HTTP |
| `modules/marketing_room/campaign_manager.py` | modified (A.5) | Meta graph عبر safe HTTP + host allowlist |
| `modules/hotel/lock_cards.py` | modified (A.5) | استثناء door-lock: loopback + منفذ ضيق + لا redirects |
| `modules/hotel/router_bookings.py` | modified (A.5) | التحقق عند حفظ `hotel_lock_encoder_url` فقط (سطر الإعداد) |

### اختبارات أمنية

| المسار | حالة | الغرض |
|--------|------|--------|
| `tests/test_security_phase_a.py` | untracked | IDOR / SSRF / CSRF / wallet / door-lock / redirect |
| `tests/test_sync.py` | modified (A.5) | عزل DB لمنع flaky (ليس ميزة منتج) |
| `tests/test_seo_agent.py` | modified | إرسال CSRF في طلبات الجلسة — تكيف مع حماية CSRF (ليس SEO feature) |
| `tests/test_login_csrf_body.py` | موجود سابقًا | Regression تسجيل الدخول + CSRF |

### مستند العزل

| المسار | الغرض |
|--------|--------|
| `deploy/PHASE_A_SECURITY_MANIFEST.md` | هذا الملف |

---

## 2) خارج نطاق commit الأمان (لا تضمّنها)

- `.agents/**` و `.cursor/**` و skills
- `tests/test_marketing_room.py` (عدد بطاقات الوكلاء 8→9 — منتج)
- أي تعديلات hotel/POS/GL/booking/backup/product غير المذكورة أعلاه
- تغييرات `.env` (ممنوعة في هذه المرحلة)

> ملاحظة: `modules/hotel/router_bookings.py` فيه diff منتج كبير تاريخيًا؛ لـcommit الأمان خذ **فقط** كتلة التحقق من `hotel_lock_encoder_url` (حوالي الأسطر التي تستدعي `assert_local_encoder_base_url`) عبر `git add -p` لاحقًا.

---

## 3) أوامر مراجعة مقترحة (لا تُنفَّذ تلقائيًا)

```bash
git add app/upload_authorization.py app/uploads_router.py app/csrf.py
git add modules/common/safe_http_url.py modules/common/safe_http.py
git add modules/authz/pos_wallet_access.py
git add modules/messaging/web_chat_service.py modules/messaging/hermes_ai.py
git add modules/messaging/providers/textmebot.py modules/messaging/providers/telegram.py
git add modules/kds/service.py
git add modules/marketing_room/ai_client.py modules/marketing_room/pipeline.py modules/marketing_room/campaign_manager.py
git add modules/hotel/lock_cards.py
git add -p modules/hotel/router_bookings.py   # قبول hunk التحقق من encoder URL فقط
git add tests/test_security_phase_a.py tests/test_sync.py tests/test_seo_agent.py
git add deploy/PHASE_A_SECURITY_MANIFEST.md
```

---

## 4) إعدادات بيئة — توصية فقط (لم تُغيَّر)

| متغير | TEST `pos.baytak.ly` | PRODUCTION `baytak.baytak.ly` | Local HTTP |
|--------|----------------------|-------------------------------|------------|
| `SESSION_HTTPS_ONLY` | `true` | `true` | `false` |
| `ONLINE_SYNC_REQUIRE_HMAC` | لا تفعّل بعد | لا تفعّل بعد | — |

انظر تقرير A.5 في المحادثة لتفاصيل HMAC readiness.
