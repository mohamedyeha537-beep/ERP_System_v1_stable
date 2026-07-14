# موقع التجارة الإلكترونية — متجر واحد مرتبط بنقطة البيع

> **النطاق الحالي:** تاجر واحد (مثلاً baytak) — **ليس** multi-vendor بعد.  
> للعودة لخطة SaaS لاحقاً: `docs/saas-multivendor-roadmap.md`

**آخر مراجعة:** 2026-06-10  
**الحالة:** 🟡 قيد التخطيط — جاهز للبدء بالتطوير

---

## 1. الهدف

موقع تجارة إلكترونية **واحد** يعرض منتجات نقطة البيع، يستقبل الطلبات، ويربطها مباشرة بـ:

- POS (order hub)
- المخزون / BOM
- التوصيل والاستلام
- (اختياري) الدفع أونلاين

---

## 2. ما هو موجود اليوم (لا نعيد بناءه)

| المكوّن | المسار / الوصف |
|---------|----------------|
| Guest chat storefront | `/chat` — `app/templates/guest_chat.html` |
| Chat API | `modules/messaging/router_web_chat.py` |
| إنشاء طلب من الويب | `modules/messaging/chat_order_service.py` |
| POS hub | `modules/sales/pos_orders_hub.py` + قوالب `pos_order_hub.html` |
| Integration API | `POST /api/integration/orders` — `modules/integration/` |
| Delivery / pickup | `modules/delivery/` |
| Catalog | `modules/catalog/` — منتجات، فئات، أسعار |
| Branding | `modules/branding/` — شعار، ألوان |

---

## 3. الفجوة — ما نبنيه

### أولوية 1 (MVP متجر)

- [ ] صفحة رئيسية `/shop` أو `/` — catalog grid (فئات + منتجات نشطة للعرض)
- [ ] صفحة منتج / modal تفاصيل
- [ ] سلة + checkout (delivery / pickup / dine-in إن لزم)
- [ ] ربط الطلب بـ `Sale` بنفس منطق web_chat (مصدر `ONLINE` / `WEB_STORE`)
- [ ] إظهار الطلب في POS hub فوراً
- [ ] mobile-first UI (منفصل عن guest_chat أو تطوير chat)

### أولوية 2

- [ ] إعدادات admin: تفعيل المتجر، slug، منتجات تظهر في المتجر (`show_in_pos` / flag جديد)
- [ ] بوابة دفع (تحويل بنكي + رفع إثبات — موجود جزئياً في chat)
- [ ] SEO أساسي (title، description من settings)

### أولوية 3

- [ ] حساب عميل (اختياري — loyalty موجود)
- [ ] تتبع حالة الطلب برابط
- [ ] Domain مخصص (shop.baytak.ly)

---

## 4. قرارات تقنية مقترحة

| القرار | التوصية |
|--------|---------|
| Framework | نفس FastAPI + Jinja2 (اتساق مع POS) |
| مصدر الطلب | `Sale.source = ONLINE` + `external_order_id = webstore:…` |
| إعادة استخدام الكود | `chat_order_service` / `create_online_sale_completed` |
| CSS | ملف منفصل `shop.css` — لا نكسر POS |
| Auth للعملاء | guest أولاً؛ loyalty لاحقاً |

---

## 5. ملفات متوقعة (عند البدء)

```
modules/shop/           # router + service جديد
app/templates/shop/     # catalog, cart, checkout
app/static/shop.css
app/static/shop.js
app/main.py             # include_router
modules/settings/       # shop_enabled, shop_title, …
```

---

## 6. سجل التحديثات

| التاريخ | الملاحظة |
|---------|----------|
| 2026-06-10 | **MVP مُنفَّذ:** `/shop` + API — نفس منتجات POS، وسائل الدفع، طلبات → POS hub (`webstore:`) |
