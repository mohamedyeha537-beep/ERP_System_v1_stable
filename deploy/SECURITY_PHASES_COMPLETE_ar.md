# إغلاق المراحل الأمنية — v1.1.0+

**التاريخ:** 2026-08-25  
**الحالة:** المراحل 1 + 2 + 3 + بنود الـ backlog التنفيذية **مكتملة في الكود المحلي**.

---

## ما أُغلق

### المرحلة 1 (حرج)
- إزالة حساب `treasury` الثابت
- CSP / HSTS / Permissions-Policy
- حماية `/uploads`
- XSS housekeeping + schema error page
- Rate limiting + تصليب الجلسة

### المرحلة 2 (عالٍ)
- SSRF في marketing / messaging
- حماية مسارات GET المشبوهة (HR / KDS / treasury / hotel)
- قفل login بعد محاولات فاشلة
- OTP بـ `compare_digest` بدون تجاوز أدمن
- shop lookup بدون كشف الاسم

### المرحلة 3 (متوسط)
- CSRF middleware + حقن تلقائي في النماذج
- فحص magic bytes لرفع الملفات

### Backlog تنفيذي (هذا الإكمال)
| البند | التنفيذ |
|--------|---------|
| HMAC + IP allowlist لـ Sync API | `ONLINE_SYNC_REQUIRE_HMAC` + `ONLINE_SYNC_IP_ALLOWLIST` |
| IP allowlist لـ Integration API | `INTEGRATION_API_IP_ALLOWLIST` |
| X3 `pos_shift_shortages` | `tojson` بدل `\| safe` |
| إبطال cache المستخدم | `app/user_cache.py` عند تغيير أدوار/كلمات مرور |
| إعادة إدخال كلمة الأدمن | حقل `admin_password` عند تغيير كلمة مستخدم |
| توقيع النسخ الاحتياطية | ملف `.sig` عند الإنشاء + تحقق عند الاستعادة |
| مسارات GET المتبقية من المراجعة | محمية بـ Depends |

---

## إعدادات الإنتاج المطلوبة

```env
APP_ENV=production
SESSION_HTTPS_ONLY=true
SESSION_SAMESITE=strict
SECRET_KEY=<openssl rand -hex 32>
INTEGRATION_API_KEY=<secrets.token_urlsafe(32)>
INTEGRATION_API_IP_ALLOWLIST=<IP أو CIDR للتكامل>
ONLINE_SYNC_API_KEY=<secrets.token_urlsafe(32)>
ONLINE_SYNC_IP_ALLOWLIST=<IP للمواقع المحلية>
ONLINE_SYNC_REQUIRE_HMAC=true
BACKUP_REQUIRE_SIGNATURE=true
SEED_DEMO_USERS=false
```

---

## ما يبقى خارج نطاق الكود (تشغيل/QA)

1. **اختبار اختراق خارجي** قبل الإنتاج الحي.
2. **نشر + اختبار يدوي** على `pos.baytak.ly`.
3. **commit / tag / push** عند الموافقة.
4. مستخدم DB بأقل صلاحيات لعمليات الاستعادة (ضبط خادم MySQL/Postgres — ليس كود تطبيق).

هذه ليست «مرحلة 4» ناقصة في الدليل؛ هي إجراءات تشغيل بعد اكتمال الترقيع.
