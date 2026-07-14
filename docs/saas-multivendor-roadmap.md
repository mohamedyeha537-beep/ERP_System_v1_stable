# خارطة SaaS والسوق متعدد التجار

> **متى تُستخدم هذه الوثيقة:** عند ذكر «SaaS»، «متعدد التجار»، «multi-tenant»، «marketplace»، أو «تسجيل تجار وتجربة مجانية» — ارجع لهذا الملف، طوّر عليه، أو أضف ملاحظات جديدة في قسم «سجل التحديثات» في الأسفل.

**آخر مراجعة:** 2026-06-10  
**القرار الحالي:** تأجيل SaaS — التركيز أولاً على **موقع تجارة إلكترونية واحد** مرتبط بنقطة البيع الحالية (تاجر واحد = baytak / pos.baytak.ly).

---

## 1. السؤال الاستراتيجي

هل ننسخ المشروع ونحوّله multi-vendor، أم نبدأ مشروعاً جديداً؟

### القرار الموصى به

| الخيار | التقييم |
|--------|---------|
| **مشروع جديد من الصفر** | ❌ غير مناسب — ستُفقد سنوات عمل (GL، HR، ZKBio، KDS، فندق، shifts، BOM، تقارير…) |
| **Fork منفصل** | ❌ يضاعف الصيانة دون تقليل الجهد |
| **نفس المشروع + refactor تدريجي** | ✅ الأفضل على المدى الطويل |
| **نفس المشروع + instance لكل تاجر (بدون multi-tenant في DB)** | ✅ الأسرع للبيع |

**الخلاصة:** استمر على **نفس الكود**، وسّعه على مراحل. لا fork ولا rewrite.

---

## 2. الوضع الحالي للمشروع (2026-06)

- **Monolith** FastAPI + SQLAlchemy + Jinja2
- **تاجر واحد** لكل نشر (instance + قاعدة بيانات)
- **لا يوجد** `tenant_id` / `merchant_id` / `organization_id` في الجداول
- **قيود عالمية** تمنع DB مشتركة: `users.username`، `products.name_ar`، `AppSetting.key` (PK واحد)…
- **إعدادات** عبر `AppSetting` (key/value) — تعليقات في `modules/branding/service.py` تخطط لصف لكل tenant لاحقاً
- **أونلاين جزئي موجود:** `/chat` (guest_chat)، Integration API، POS order hub، delivery/pickup

### ملفات مرجعية

| الموضوع | المسار |
|---------|--------|
| الإعدادات | `infra/config.py` |
| Bootstrap DB | `infra/schema_bootstrap.py` |
| Auth / RBAC | `modules/authz/` |
| إعدادات المتجر | `modules/settings/` |
| طلبات ويب | `modules/messaging/router_web_chat.py`, `chat_order_service.py` |
| API تكامل | `modules/integration/` |
| Branding (مستقبل tenant) | `modules/branding/service.py` |

---

## 3. مساران للتجار المتعددين

### المسار 1 — SaaS مُدار (instance لكل تاجر) — **الأسرع**

```
posbayt.ly              → موقع تسويق + تسجيل + فوترة
merchant1.posbayt.ly    → VPS/DB/.env خاص
merchant2.posbayt.ly    → VPS/DB/.env خاص
```

**المطلوب برمجياً (طبقة فوق POS الحالي):**

- Landing page + signup
- تجربة 14 يوم / اشتراك
- Script provisioning: DB + `.env` + nginx subdomain + `alembic upgrade head`
- لوحة super-admin (إنشاء / إيقاف / تجديد tenants)
- **بدون** تغيير core DB schema في البداية

**مناسب إذا:** 10–100 تاجر، فريق صغير، البيع خلال 2–3 أmonths.

---

### المسار 2 — Multi-tenant حقيقي (DB واحدة، many merchants)

**المراحل:**

| # | العمل |
|---|--------|
| 1 | جدول `tenants` + تسجيل + trial + billing |
| 2 | Middleware: تحديد tenant من subdomain أو slug |
| 3 | `AppSetting` → `(tenant_id, key)` composite PK |
| 4 | `tenant_id` + composite unique على: users, products, sales, customers… |
| 5 | API key و uploads و branding لكل tenant |
| 6 | GL, HR, hotel — module بmodule حسب الحاجة |
| 7 | Marketplace (اكتشاف متاجر، عمولة) |

**مناسب إذا:** مئات/آلاف التجار، تكلفة VPS لكل تاجر عالية.

**الجهد:** كبير — refactor تدريجي وليس rewrite.

---

## 4. خارطة زمنية مقترحة (عند العودة لـ SaaS)

### المرحلة 0 — قبل multi-tenant (الآن)

- [ ] **موقع تجارة إلكترونية واحد** مرتبط بـ POS (انظر `docs/ecommerce-single-store-roadmap.md` إن وُجد)
- [ ] استقرار baytak / pos.baytak.ly على MySQL + migrations

### المرحلة A — SaaS مُدار (3–6 أشهر بعد إطلاق المتجر)

- [ ] Landing + signup + trial
- [ ] Provisioning automation
- [ ] Super-admin panel
- [ ] فوترة (يدوية أو Stripe/…)

### المرحلة B — Multi-tenant في DB (6–12 شهر)

- [ ] `tenants` + `tenant_id` على الجداول الأساسية
- [ ] Subdomain routing
- [ ] عزل settings / branding / API keys

### المرحلة C — Marketplace

- [ ] دليل متاجر عام
- [ ] عمولات / payouts
- [ ] SEO متعدد

---

## 5. البيع أونلاين — ما موجود vs ما ينقص

| موجود | ينقص (للمرحلة الحالية — متجر واحد) |
|--------|-------------------------------------|
| `/chat` طلبات تفاعلية | واجهة متجر catalog تقليدية (غير chat-only) |
| Integration API | بوابة دفع أونلاين |
| POS hub للطلبات | SEO، صفحات منتج، slug |
| delivery / pickup zones | حساب عميل + تتبع طلب |
| guest checkout flow | تحسين UX mobile + branding |

---

## 6. قرارات سريعة (مرجع)

| السؤال | الجawab |
|--------|---------|
| أبيع خلال 2–3 أشهر؟ | المسار 1 — instance لكل تاجر |
| marketplace ضخم لاحقاً؟ | المسار 2 تدريجياً على نفس الكود |
| أبدأ من الصفر؟ | لا — إلا إذا النطاق = ordering-only بدون back-office |

---

## 7. مخاطر يجب تذكرها

1. **Global unique constraints** — أي tenant_id يتطلب migrations واسعة
2. **Background workers** — messaging outbox، print jobs: يحتاجون tenant context
3. **File uploads** — `app/static/uploads/` مشترك اليوم
4. **INTEGRATION_API_KEY** — مفتاح واحد في `.env`؛ لاحقاً per-tenant
5. **Alembic vs sqlite_patch** — MySQL prod يعتمد migrations رسمية

---

## 8. سجل التحديثات

| التاريخ | الملاحظة |
|---------|----------|
| 2026-06-10 | إنشاء الوثيقة بعد نقاش: fork vs new vs incremental. القرار: متجر واحد أولاً ثم SaaS. |
| | *(أضف هنا ملاحظاتك عند العودة)* |

---

## 9. كلمات مفتاحية للبحث في المحادثة

`SaaS` · `multi-tenant` · `multi-vendor` · `marketplace` · `tenant_id` · `provisioning` · `تجربة مجانية` · `تسجيل تاجر`
