# دليل التنفيذ المتبقي — ERP_System_v1_stable v1.1.0
## للمطور/الـ Cursor لتنفيذ الإصلاحات المحاسبية المتبقية

> **تحديث 2026-08-25 — اقرأ قبل التنفيذ**
>
> | المرجع | ماذا يحتوي |
> |--------|------------|
> | **tag v1.0.0** | snapshot قبل إصلاحات الفندق |
> | **tag v1.1.0** (`5ae0de9` على `bayatak_hotel_roof_caffee`) | فندق/فوليو/مغادرة — **ليس** كل النقاط أدناه |
> | **PR #2** (`review/v1.1.0-accounting`) | v1.1.0 + GL idempotency، FEFO، مرتجع lots، ليالي الحجز |
> | **هذا الدليل** | backlog محاسبي متبقٍ — نفّذه **بعد** v1.1.0 وPR #2 |
>
> مراجعة تفصيلية: `deploy/HANDOFF_V1.1.0_REVIEW.md` في المستودع.
>
> **نُفّذ محلياً (2026-08-25):** نقاط COGS **1–4** و**19** (تكلفة احتياطية + منع ازدواج GL للمصروف).

---

## 1. نقطة البداية

1. استنسخ المستودع:
   ```bash
   git clone https://github.com/mohamedyeha537-beep/ERP_System_v1_stable.git
   cd ERP_System_v1_stable
   ```
2. للإصلاحات المحاسبية في PR #2 (موصى به قبل باقي النقاط):
   ```bash
   git fetch origin
   git checkout bayatak_hotel_roof_caffee
   git merge origin/review/v1.1.0-accounting
   ```
3. تثبيت التبعيات:
   ```bash
   python -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
4. اختبار أساسي قبل البدء:
   ```bash
   rm -f tests/test.db tests/test.db-shm tests/test.db-wal
   .venv/bin/python -m pytest tests/test_smoke.py -v
   # المتوقع: 6/6 ناجحة
   ```
5. بعد كل تعديل:
   ```bash
   .venv/bin/ruff check <الملفات_المعدلة>
   .venv/bin/python -m pytest tests/test_smoke.py -v
   ```

---

## 2. أهم قاعدة: السماح بالمخزون السالب يبقى

المستخدم يصرّح أن workflow التشغيلي يسمح ببيع كمية قبل استلامها؛ عند لاحق استلام الشحنة يُصحّح المخزون تلقائيًا.

- **لا تُضف** حظرًا عامًا على `StockMovementType.SALE` عند `new_qty < 0` في `modules/inventory/service.py:apply_movement`.
- **المطلوب:** عندما لا تغطي الدفعات الموجودة كمية البيع، يجب احتساب COGS للكمية غير المغطاة بتكلفة احتياطية (`reference_unit_cost` أو متوسط التكلفة الحالي) حتى لا يظهر COGS صفرًا أو مُبالغًا في انخفاضه.

---

## 3. قائمة الإصلاحات المتبقية حسب الأولوية

### أ. المخزون / COGS / الدفعات

| # | الملف | المشكلة | الإصلاح المطلوب |
|---|-------|---------|-----------------|
| 1 | `modules/inventory/lots.py` `allocate_lots_fifo` | ~~عند نقص الدفعات~~ **مُعالَج في `costing.py`**: `fallback_unit_cost` + `sale_fifo_cogs` للكمية غير المغطاة (بدون `lot_id=None` — يتطلب migration) |
| 2 | `modules/inventory/costing.py` `sale_fifo_cogs` | ~~لا تحسب~~ **✅ نُفّذ** — `fallback_unit_cost` + uncovered qty |
| 3 | `modules/gl/posting.py` `sale_cogs_amount` | ~~إكمال النقص~~ **✅ نُفّذ** — يستخدم `sale_fifo_cogs` الكامل |
| 4 | `modules/reporting/queries.py` `_unit_cost_via_bom` | ~~لا يستخدم reference~~ **✅ نُفّذ** — يتحقق من `reference_unit_cost` |
| 5 | `modules/catalog/bom_pricing.py` + `bom_explosion.py` | استراتيجية التقريب مختلفة بين `_unit_cost_via_bom` و `bom_total_cost` | استخدم نفس التقريب السطر-بناءً-سطر في كليهما. |
| 6 | `modules/inventory/transfer_service.py` | المستودع الوجهة يستلم `StockMovement` موجبًا فقط دون دفعات `InventoryLot` | عند `approve_transfer_line` أنسخ دفعات المصدر المستهلكة إلى الوجهة مع `unit_cost` و `expiry_date`. (إذا كان `purchase_line_id` non-nullable/unique، أنشئ دفعات جديدة بربط `transfer_id` وعدّل قيود `InventoryLot` لتسمح بذلك أو استخدم `source_lot_id` جديد). |
| 7 | `modules/inventory/product_ledger.py` `apply_waste` + `apply_stock_count` | لا توجد قيود GL للهدر والعجز/الزيادة | أضف `post_waste_shadow` (Dr مصروف هدر / Cr مخزون) و `post_adjustment_shadow` (Dr/Cr حساب تسوية). |
| 8 | `modules/inventory/service.py` `apply_movement` | حركات `WASTE`, `ADJUSTMENT`, `SHORTAGE` لا ترحّل | استدعِ دوال GL المناسبة عند هذه الأنواع. |
| 9 | `modules/payments/service.py` `record_inventory_purchase` | يقبل مورد "مرجع تكلفة" وينشئ مخزونًا بدون قيد GL | ارفض أي مورد/أسلوب "مرجع تكلفة" أو افصله إلى مسار `record_component_cost_entry` خاص. |
| 10 | `modules/reporting/queries.py` `_weighted_avg_unit_cost_per_product` | يتضمن فواتير مرجع التكلفة في المتوسط | استبعد `Purchase` التي `is_cost_reference_purchase(p) is True`. |
| 11 | `modules/reporting/queries.py` `inventory_value` | **الوصف الصحيح:** `StockBalance × avg_unit_cost_per_product` (ليس «أقدم دفعة»). المشكلة: الأرصدة السالبة تُنقص المجموع. | أعد الحساب إلى `sum(InventoryLot.qty_remaining × unit_cost)` لكل مستودع. |
| 12 | `modules/gl/posting.py` `sale_return_cogs_amount` | تستخدم متوسط/BOM بدل تكلفة الدفعة الأصلية | استخدم `InventoryLotConsumption` المرتبطة بـ `sale_id` الأصلي لكل بند مرتجع واعكس `line_total` الفعلي. |
| 13 | `modules/refunds/service.py` `_return_components` | الترجيع الجزئي قد يوزع على دفعات غير مطابقة | عند `restore_lots_for_sale_return` وزّع الترجيع على الاستهلاكات الأصلية تناسليًا (`qty_returned / total_sold`) وإذا تبقّى شيء استخدم `fallback_cost`. |

### ب. نقاط البيع والمدفوعات

| # | الملف | المشكلة | الإصلاح المطلوب |
|---|-------|---------|-----------------|
| 14 | `modules/refunds/service.py` `create_sale_return` | قد يرد نقدًا أكثر مما دُفع | احسب `cash_refund = total_return * (paid_total / sale.total)` مطروحًا منه الخصومات/النقاط. اجعل `record_refund_payment` بهذا المبلغ، وعالج الفرق كعكس خصم/نقاط. |
| 15 | `modules/delivery/service.py` `record_delivery_cash_settlement` | أجور التوصيل لا تُرحّل | أضف `delivery_fee` إلى `sale.total` كإيراد أو نشر قيد `Dr مصروف توصيل / Cr نقد` لكل `DeliveryCashSettlement`. |
| 16 | `modules/sales/service.py` + `packaging.py` + `order_policy.py` | مواد التغليف والمنتج المجاني يُستهلك من المخزون دون COGS | أضف `_packaging_unit_cost_via_bom` + تكلفة المنتج المجاني إلى `sale_cogs_amount` قبل `post_sale_cogs_shadow`. |
| 17 | `modules/customers/service.py` + `modules/pos_shifts/loyalty_settlement.py` | خصومات الولاء لا تنعكس في GL | عند استبدال النقاط، انشر `Dr خصم مبيعات / Cr AR` بنفس مبلغ الخصم، أو قلّل `sale.total` قبل `post_sale_completed_shadow`. |
| 18 | `modules/hr/meal_allowance.py` + `record_accrual_expense` | يستخدم أول محفظة نقدية للاستحقاق | استخدم حساب `ذمم دائنة/استحقاق` حقيقي (وسيلة دفع `can_pay=false` وربط GL بـ `2100/2200`) واستدعِ `post_expense_shadow_safe`. |
| 19 | `modules/payments/service.py` `record_expense` | يرحّل النقد والموردين مرتين | إذا كانت طريقة الدفع نقدية، لا تنشئ `PurchasePayment` أو لا تنشر `post_purchase_payment_shadow` للمصروف. يكفي `post_expense_shadow` (Dr مصروف / Cr نقد). |
| 20 | `modules/payments/service.py` `record_sale_payment` + `record_refund_payment` | لا يتحققان من تجاوز المبالغ | أضف `assert sum(payments) <= sale.total` و `sum(refunds) <= sale_return.total` و `<= paid_total`. |
| 21 | `modules/sales/models.py` + `payments/router_web.py` | لا توجد حقول ضريبة/إكرامية/رسوم خدمة | أضف `tax_rate`, `tax_amount`, `tip`, `service_charge` إلى `Sale`/`SaleLine` ونشر قيود GL (إيراد + ضريبة مستحقة + إكرامية مستحقة). |

### ج. الفندق

| # | الملف | المشكلة | الإصلاح المطلوب |
|---|-------|---------|-----------------|
| 22 | `modules/hotel/booking_service.py` `assert_booking_prepayment_met` + `validate_booking_prepayment` | نسبة الدفع المسبق غير مُفعّلة | استبدل `required = due` بـ `required = required_prepayment_amount(db, due=due)`، مع الصفر للشركات المُفعّل حسابها. |
| 23 | `modules/hotel/booking_service.py` `apply_customer_wallet_to_booking` + `refund_booking_credit` | الدفع من المحفظة لا ينشئ `HotelBookingPayment` ولا يُسترد | أنشئ `HotelBookingPayment` نوع "wallet" عند خصم المحفظة، وفي `refund_booking_credit` أعد الرصيد إلى المحفظة. |
| 24 | `modules/hotel/folio.py` `build_folio` | ذاكرة التخزين المؤقت قديمة ولا تعالج `orphan_sales` | أضف الخدمات والمرتجعات إلى `cache_key`، واستخدم `pos_lines` المعادة من `_pos_charges_total` في الفوليو. |
| 25 | `modules/hotel/folio.py` + `company_agreement_service.py` | قواعد الشركات لا تحترم `limit_amount`/`limit_quantity` | عدّل `allocate_charge` لـ `SHARED` بدون حد إلى 50/50، وطبّق `limit_quantity`/`limit_amount` في `build_folio`. |
| 26 | `modules/hotel/finance_service.py` + `revenue_stats.py` | العربون يُحسب ضمن الإيراد | عزل العربون في حساب مسؤولية منفصل حتى المغادرة؛ لا تضفه إلى `revenue_total`. |
| 27 | `modules/gl/posting.py` `post_hotel_booking_payment_shadow` + `try_post_booking_gl` | الإيراد يُعترف به عند القبض | عند القبض: `Dr نقد / Cr عربون مكتسب غير محقق`. عند `check-out`: `Dr عربون/ذمم / Cr إيراد إقامة`. |

### د. GL / الخزينة

| # | الملف | المشكلة | الإصلاح المطلوب |
|---|-------|---------|-----------------|
| 28 | `modules/inventory/transfer_service.py` | لا توجد قيود GL للتحويلات | عند `approve_transfer_line` انشر `Dr مخزون الوجهة / Cr مخزون المصدر` (أو تجاوز إذا كانت نفس القيمة). |
| 29 | `modules/pos_shifts/shift_close_gl.py` | `gl_gap` يمزج أسس النقد والاستحقاق | أما نشر المرتجعات كخصم من `4100` (contra-revenue) أو أضف `4200` إلى صافي الإيراد؛ افصل مقارنة التحصيل النقدي عن مقارنة الإيراد الاستحقاقي. |
| 30 | `modules/pos_shifts/shift_close_gl.py` + `modules/gl/backfill.py` | مرتجعات/مدفوعات بعد الإقفال لا تُرحّل | استدعِ `backfill_shift_gl` أو انشر مباشرةً عند أي عملية بعد إقفال الوردية. |
| 31 | `modules/gl/posting.py` `_safe` | وضع shadow يخفي الأخطاء | حوّل إلى تنبيهات مرئية (UI/Sentry) أو انتقل إلى `live` بعد اختبار شامل. |
| 32 | `modules/payments/service.py` `record_accrual_expense` + `settle_loyalty_on_shift_close` | تكلفة الولاء لا تصل إلى GL | استدعِ `post_expense_shadow_safe` فورًا بعد `record_accrual_expense` أو عند إغلاق الوردية. |

---

## 4. سيناريوهات الاختبار التي يجب تمريرها

1. **البيع بالسالب ثم الاستلام:**
   - أنشئ منتج بدون مخزون.
   - بع 45 قطعة → `StockBalance = -45`، `COGS` يُحسب بتكلفة المرجع/المتوسط.
   - ادخل فاتورة شراء بـ 100 قطعة → `StockBalance = 55`، دفعات FIFO تتكون.
   - تأكد من أن قيمة COGS للـ 45 تطابق التكلفة الاحتياطية.

2. **مرتجع جزئي:**
   - بيع 10 بسعر 10 = 100، دفع 80 بعد خصم 20 نقاط.
   - استرجع 5 قطع → يجب أن يكون النقد المسترد = 40 (نسبة الدفع) وليس 50، والـ 10 المتبقية عكس نقاط.
   - `InventoryLot.qty_remaining` يزيد بـ 5 عبر `restore_lots_for_sale_return`.

3. **تحويل بين مستودعين:**
   - انقل 20 قطعة من A إلى B.
   - تحقق أن B لديه `InventoryLot` جديدة بنفس `unit_cost` و `expiry_date`.
   - بيع من B يستهلك FIFO/FEFO صحيح.

4. **مصروف نقدي:**
   - سجّل مصروف 500 بوسيلة نقدية.
   - تحقق أن `1110` انخفض 500 فقط مرة واحدة، وأن `2100` لم يزداد.

5. **فندق مع عربون:**
   - احجز غرفة بدفع عربون 30%.
   - تحقق أن `revenue_total` لا يشمل العربون قبل المغادرة.
   - عند `check-out` يُعترف بالإيراد كاملًا ويُخصم العربون.

6. **مطابقة GL:**
   - بعد السيناريوهات، قارن أرصدة `1110` (نقد)، `1200` (ذمم عملاء)، `1300` (مخزون)، `2100` (موردين)، `4100` (إيراد)، `5100` (COGS) مع التقارير التشغيلية.

---

## 5. خطة العمل المقترحة

1. ابدأ بالمخزون/COGS (نقاط 1–13) لأنها الأساس لكل شيء.
2. ثم POS/المدفوعات (نقاط 14–21).
3. ثم الفندق (نقاط 22–27).
4. أخيرًا GL/الخزينة (نقاط 28–32).
5. بعد كل مرحلة شغّل `pytest` و `ruff` وافحص أرصدة GL يدويًا.

---

**ملاحظة:** لا تغيّر سلوك السماح بالبيع بالسالب (`SALE` مع `new_qty < 0`)؛ فقط أضف تكلفة احتياطية للجزء غير المغطى بدفعات.
