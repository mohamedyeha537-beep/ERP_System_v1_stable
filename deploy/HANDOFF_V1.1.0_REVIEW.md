# مراجعة handoff v1.1.0 — مقارنة الإصدارات والاختبار

تاريخ المراجعة: 2026-08-25

## 1. العلاقة بين الإصدارات

| المرجع | Commit | الفرع | المحتوى |
|--------|--------|-------|---------|
| **v1.0.0** | `664582e` | `bayatak_hotel_roof_caffee` | قبل إصلاحات الفندق/الفوليو |
| **v1.1.0** | `5ae0de9` | `bayatak_hotel_roof_caffee` | فندق/فوليو/مغادرة + treasury/HR/authz |
| **PR #2** | `a1cdec6` | `review/v1.1.0-accounting` | v1.1.0 + 2 commits محاسبية إضافية |

PR #2 **يبني فوق v1.1.0** — ليس بديلاً عنه.

### ما في PR #2 فقط (7 ملفات، +90 −39 سطر)

| الملف | الإصلاح |
|-------|---------|
| `modules/gl/posting.py` | تحديث قيود GL عند تصادم idempotency (بدل إرجاع قيد قديم) |
| `modules/payments/service.py` | عكس GL قبل `delete_purchase` |
| `modules/inventory/lots.py` | FEFO + استبعاد الدفعات المنتهية |
| `modules/refunds/service.py` | تمرير `sale_id` الأصلي لاستعادة الدفعات عند المرتجع |
| `modules/hotel/booking_service.py` | `booking.nights` عند التمديد/تغيير المغadرة؛ `_recalc_payment_status` بعد الرد |
| `modules/hotel/availability.py` | `PENDING` يحجب الغرفة |
| `modules/hotel/finance_service.py` | lint فقط |

### ما في v1.1.0 وليس في PR #2 (27 ملفاً — إصلاحات الفندق)

- `modules/hotel/folio.py` — توزيع المدفوعات hotel/restaurant، `clear_folio_cache`
- `modules/hotel/departure_settlement.py` — تسعير مغادرة مبكرة
- `app/templates/hotel/booking_detail.html` — واجهة الرصيد الموحّد
- treasury, HR, authz, reporting — تحسينات منفصلة

### ما **لم يُنفَّذ** في v1.1.0 ولا PR #2 (من دليل handoff)

نقاط COGS 1–4، مصروف مزدوج GL (19)، ومعظم نقاط 5–32 — **تُنفَّذ في commit منفصل** (انظر §3).

---

## 2. اختبار v1.1.0 على pos.baytak.ly

| الفحص | النتيجة | ملاحظة |
|-------|---------|--------|
| HTTPS `/` | **302** | إعادة توجيه — السيرفر يعمل |
| `/login` | **404** | مسار الدخول قد يكون مختلفاً (جرّب `/admin/login` أو `/pos`) |
| folio / checkout / disbursement | **يتطلب جلسة** | لا يمكن اختبار منطق الحجز بدون credentials |

**توصية:** بعد نشر v1.1.0 على السيرفر، اختبر يدوياً:

1. حجز مسكّن + وجبة مطعm → الرصيد الموحّd = خالص
2. مغادرة مبكرة → بدون طلب استرداد وهمي
3. إيصال صرف → يظهر فقط عند `amount_credit > 0` حقيقي

---

## 3. أولوية التنفيذ (COGS + مصروف)

**تم تنفيذها في هذا المستودع:**

| # | الملف | الإصلاح |
|---|-------|---------|
| 1–3 | `costing.py`, `posting.py` | COGS للكمية غير المغطاة بدفعات (`fallback_unit_cost`) |
| 4 | `queries.py` | `_unit_cost_via_bom` يتحقق من `reference_unit_cost` |
| 19 | `posting.py` | منع ازدواج GL لمصروف نقدي (`post_purchase_payment_shadow` يتخطى EXPENSE) |

**المرحلة التالية (قبل merge PR #2 أو v1.2.0):**

1. دمج PR #2 (FEFO، GL idempotency، مرتجع lots)
2. نقاط 5–13 (تحويلات، هدر، مرجع تكلفة في المتوسط)
3. نقاط 14–21 (POS، مرتجعات، ضريبة)
4. نقاط 22–32 (فندق GL، إقفال وردية)

---

## 4. تصحيح وصف handoff — نقطة 11

**الوصف القديم في الدليل:** «`inventory_value` = StockBalance × أقدم دفعة»

**الكود الفعلي:** `StockBalance × avg_unit_cost_per_product` ([`queries.py`](../modules/reporting/queries.py) ~768)

**المشكلة الحقيقية:** الأرصدة السالبة تُضاف سالبة للمجموع — وليس خطأ «أقدم دفعة».

**الحل المقترح (لم يُنفَّذ بعد):** `sum(InventoryLot.qty_remaining × unit_cost)` لكل مخزن.

---

## 5. خلاصة

- دليل handoff **صحيح محاسبياً** كـ backlog.
- **v1.1.0 ≠ PR #2 ≠ handoff كامل** — ثلاث طبقات منفصلة.
- للسيرفر: انشر **v1.1.0** ثم اختبر الفندق، ثم ادمج **PR #2**، ثم **COGS fixes** (§3).
