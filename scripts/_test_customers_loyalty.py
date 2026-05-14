"""اختبارات شاملة لوحدة العملاء + الولاء + سياق POS الجديد.

يغطي:
- تطبيع رقم الهاتف
- إنشاء/جلب-أو-إنشاء/تحديث/حذف العميل
- منع تكرار الهاتف
- إعدادات الولاء وحساب النقاط
- التعديل اليدوي للنقاط (موجب/سالب)
- حماية من جعل الرصيد سالباً
- ربط الفاتورة بعميل ومنح النقاط آلياً عند الدفع (تكامل)
- سياق Sale: TABLE/ROOM/EXTERNAL
- POS: تبديل السياق (HTTP)
- POS checkout للعميل ومنح النقاط (HTTP)
"""

import os
import sys
from decimal import Decimal

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import modules.authz.models  # noqa: F401, E402
import modules.catalog.models  # noqa: F401, E402
import modules.customers.models  # noqa: F401, E402
import modules.hotel.models  # noqa: F401, E402
import modules.hr.models  # noqa: F401, E402
import modules.inventory.models  # noqa: F401, E402
import modules.payments.models  # noqa: F401, E402
import modules.sales.models  # noqa: F401, E402
import modules.settings.models  # noqa: F401, E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from infra.db import get_session_factory  # noqa: E402
from modules.catalog.models import Product, ProductCategory, ProductKind  # noqa: E402
from modules.customers.models import Customer  # noqa: E402
from modules.customers.service import (  # noqa: E402
    CustomersError,
    adjust_points,
    create_customer,
    delete_customer,
    get_by_phone,
    get_customer,
    get_or_create_by_phone,
    grant_points_for_sale,
    list_customers,
    list_transactions,
    loyalty_settings,
    normalize_phone,
    update_customer,
)
from modules.payments.models import PaymentMethod, PaymentMethodKind  # noqa: E402
from modules.sales.models import Sale, SaleContext, SaleStatus  # noqa: E402
from modules.settings.service import set_setting  # noqa: E402

PASS = 0
FAIL = 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}")


def section(title):
    print(f"\n— {title} —")


# ============================================================
def _ensure_test_product(db):
    """يجلب صنفاً اختبارياً (لا يُنشئ فئة جديدة).

    يستخدم أول فئة موجودة لتجنّب تلويث قائمة الفئات في POS.
    """
    cat = (
        db.query(ProductCategory)
        .filter(ProductCategory.parent_id.is_(None))
        .order_by(ProductCategory.id)
        .first()
    )
    if cat is None:
        # حالة استثنائية: لا توجد فئات أصلاً → نُنشئ واحدة اسمها واضح
        cat = ProductCategory(name_ar="عام", color_hex="#64748b")
        db.add(cat)
        db.flush()
    p = (
        db.query(Product)
        .filter(Product.name_ar == "__TEST_LOYALTY_PRODUCT__")
        .first()
    )
    if p is None:
        p = Product(
            name_ar="__TEST_LOYALTY_PRODUCT__",
            sku=f"TST-LOY-{cat.id}",
            unit="وحدة",
            sell_price=Decimal("10.000"),
            kind=ProductKind.FINAL_SELLABLE,
            category_id=cat.id,
            is_active=True,  # ضروري للبيع — يُحذف في finally
        )
        db.add(p)
        db.flush()
    return p


def _cleanup_test_product(db):
    """يحذف صنف الاختبار وأي أثر له (يُستدعى في النهاية)."""
    from modules.inventory.models import StockMovement
    from modules.sales.models import SaleLine

    p = (
        db.query(Product)
        .filter(Product.name_ar == "__TEST_LOYALTY_PRODUCT__")
        .first()
    )
    if p is None:
        return
    db.query(SaleLine).filter(SaleLine.product_id == p.id).delete(
        synchronize_session=False
    )
    db.query(StockMovement).filter(StockMovement.product_id == p.id).delete(
        synchronize_session=False
    )
    db.delete(p)
    db.commit()


def _ensure_cash_method(db):
    pm = (
        db.query(PaymentMethod)
        .filter(PaymentMethod.kind == PaymentMethodKind.CASH)
        .first()
    )
    if pm is None:
        pm = PaymentMethod(
            name_ar="كاش-اختبار", kind=PaymentMethodKind.CASH, is_active=True
        )
        db.add(pm)
        db.flush()
    return pm


# ============================================================
def main():
    Session = get_session_factory()
    db = Session()
    try:
        # تنظيف العملاء التجريبيين السابقين (مع نزع المرجع من sales)
        from modules.customers.models import LoyaltyTransaction
        from modules.sales.models import Sale as SaleM

        db.query(LoyaltyTransaction).filter(
            LoyaltyTransaction.note.like("%test-loy%")
        ).delete(synchronize_session=False)
        for c in (
            db.query(Customer)
            .filter(Customer.phone.like("0911%"))
            .all()
        ):
            # افصل الفواتير المرتبطة (SQLite لا يطبّق SET NULL تلقائياً)
            for s in db.query(SaleM).filter(SaleM.customer_id == c.id).all():
                s.customer_id = None
            db.flush()
            db.query(LoyaltyTransaction).filter(
                LoyaltyTransaction.customer_id == c.id
            ).delete(synchronize_session=False)
            db.delete(c)
        db.commit()

        section("تطبيع رقم الهاتف")
        check(normalize_phone("  091 234 5678 ") == "0912345678", "إزالة الفراغات")
        check(normalize_phone("+218-91-234-5678") == "+218912345678", "حفظ + إزالة الشرطات")
        check(normalize_phone(None) == "", "None → سلسلة فارغة")

        section("إنشاء وتحديث العميل")
        c1 = create_customer(db, phone="0911000001", name="أحمد")
        db.commit()
        check(c1.id is not None, "إنشاء عميل بنجاح")
        check(c1.points_balance == 0, "رصيد النقاط الابتدائي صفر")
        check(c1.visits_count == 0, "عدد الزيارات الابتدائي صفر")

        try:
            create_customer(db, phone="0911000001", name="مكرر")
            db.commit()
            check(False, "منع تكرار الهاتف")
        except CustomersError:
            db.rollback()
            check(True, "منع تكرار الهاتف")

        c2 = get_or_create_by_phone(db, phone="0911000002", name="سارة")
        db.commit()
        check(c2.id is not None and c2.name == "سارة", "get_or_create يُنشئ جديداً")

        c2_again = get_or_create_by_phone(db, phone="0911000002")
        check(c2_again.id == c2.id, "get_or_create يُعيد الموجود")

        upd = update_customer(db, c1.id, name="أحمد محمد", notes="VIP")
        db.commit()
        check(upd.name == "أحمد محمد", "تحديث الاسم")
        check(upd.notes == "VIP", "تحديث الملاحظة")

        section("منح النقاط (إعداد افتراضي 1 نقطة لكل دينار)")
        sale_total = Decimal("50.000")
        granted = grant_points_for_sale(
            db, customer=c1, sale_id=None, sale_total=sale_total
        )
        db.commit()
        check(granted == Decimal("50.000"), f"منح 50 نقطة (فعلي: {granted})")
        c1_re = get_customer(db, c1.id)
        check(c1_re.points_balance == Decimal("50.000"), "تحديث رصيد النقاط")
        check(c1_re.visits_count == 1, "زيادة عدد الزيارات")
        check(c1_re.total_spent == Decimal("50.000"), "تحديث إجمالي المدفوع")

        section("التعديل اليدوي للنقاط")
        adjust_points(
            db, customer_id=c1.id, delta_points=Decimal("10"), note="مكافأة test-loy"
        )
        db.commit()
        c1_re = get_customer(db, c1.id)
        check(c1_re.points_balance == Decimal("60.000"), "إضافة 10 نقاط")
        adjust_points(
            db, customer_id=c1.id, delta_points=Decimal("-15"), note="استخدام test-loy"
        )
        db.commit()
        c1_re = get_customer(db, c1.id)
        check(c1_re.points_balance == Decimal("45.000"), "خصم 15 نقطة")
        try:
            adjust_points(
                db, customer_id=c1.id, delta_points=Decimal("-1000"), note="test-loy"
            )
            db.commit()
            check(False, "منع جعل الرصيد سالباً")
        except CustomersError:
            db.rollback()
            check(True, "منع جعل الرصيد سالباً")

        section("سجل النقاط")
        txns = list_transactions(db, c1.id)
        check(len(txns) >= 3, f"يوجد سجل بكل الحركات (n={len(txns)})")

        section("إعدادات الولاء قابلة للتغيير")
        set_setting(db, "loyalty_earn_per_dinar", "2")
        set_setting(db, "loyalty_redeem_value_per_point", "0.05")
        db.commit()
        s = loyalty_settings(db)
        check(
            s["earn_per_dinar"] == Decimal("2") and s["redeem_value_per_point"] == Decimal("0.05"),
            "قراءة الإعدادات الجديدة",
        )
        # أعِد الإعدادات للأصل
        set_setting(db, "loyalty_earn_per_dinar", "1")
        set_setting(db, "loyalty_redeem_value_per_point", "0.1")
        db.commit()

        section("تعطيل الولاء لا يمنح نقاطاً ولكن يحدّث الإحصاءات")
        set_setting(db, "loyalty_enabled", "0")
        db.commit()
        before = c2.points_balance
        granted2 = grant_points_for_sale(
            db, customer=c2, sale_id=None, sale_total=Decimal("30")
        )
        db.commit()
        c2_re = get_customer(db, c2.id)
        check(granted2 == Decimal("0"), "صفر نقاط مع تعطيل الولاء")
        check(c2_re.points_balance == before, "الرصيد لم يتغير")
        check(c2_re.visits_count == 1, "الزيارات تُحتسب رغم التعطيل")
        set_setting(db, "loyalty_enabled", "1")
        db.commit()

        section("بحث وقائمة العملاء")
        results = list_customers(db, search="0911")
        check(len([r for r in results if r.phone.startswith("0911")]) >= 2, "نتائج البحث")

        section("منع حذف عميل له سجل نقاط")
        try:
            delete_customer(db, c1.id)
            db.commit()
            check(False, "منع حذف عميل بسجل")
        except CustomersError:
            db.rollback()
            check(True, "منع حذف عميل بسجل")

        # ============================================================
        # تكامل HTTP
        # ============================================================
        client = TestClient(app)
        login = client.post(
            "/auth/login",
            data={"username": "admin", "password": "admin123"},
            follow_redirects=False,
        )
        check(login.status_code in (302, 303), f"تسجيل دخول الأدمن ({login.status_code})")

        section("صفحة العملاء")
        r = client.get("/admin/customers")
        check(r.status_code == 200, f"GET /admin/customers ({r.status_code})")
        check("0911000001" in r.text, "ظهور العميل في الصفحة")

        section("صفحة إعدادات الولاء")
        r = client.get("/admin/loyalty")
        check(r.status_code == 200, "GET /admin/loyalty")

        section("POS: تبديل السياق إلى EXTERNAL مع رقم هاتف جديد")
        # تأكد من وجود مسودة
        client.get("/pos")  # ينشئ مسودة
        r = client.post(
            "/pos/set-context",
            data={
                "context_type": "EXTERNAL",
                "customer_phone": "0911000999",
                "customer_name": "عميل جديد HTTP",
            },
            follow_redirects=False,
        )
        check(r.status_code == 302, f"POST /pos/set-context EXTERNAL ({r.status_code})")
        new_c = get_by_phone(db, "0911000999")
        check(new_c is not None, "إنشاء العميل تلقائياً عند تبديل السياق")

        section("POS: تبديل السياق إلى TABLE يلغي العميل")
        r = client.post(
            "/pos/set-context",
            data={"context_type": "TABLE"},
            follow_redirects=False,
        )
        check(r.status_code == 302, "POST /pos/set-context TABLE")
        # تأكد أن المسودة لا تحمل عميلاً
        from modules.sales.models import Sale as SaleM, SaleStatus as SS

        draft = (
            db.query(SaleM)
            .filter(SaleM.status == SS.DRAFT)
            .order_by(SaleM.id.desc())
            .first()
        )
        if draft:
            db.refresh(draft)
            check(draft.customer_id is None, "مسودة TABLE لا تحمل عميلاً")
            check(
                draft.context_type == SaleContext.TABLE
                or draft.context_type.value == "TABLE",
                "context_type=TABLE",
            )

        section("POS checkout كامل: عميل خارجي → نقاط ولاء")
        # الدخول كـ admin (له كل الصلاحيات)
        # نظّف أي مسودة سابقة
        client.post("/pos/cancel-draft", follow_redirects=False)
        # أنشئ منتجاً وكاش
        prod = _ensure_test_product(db)
        pm = _ensure_cash_method(db)
        db.commit()
        # افتح POS
        client.get("/pos")
        # اضبط السياق على EXTERNAL برقم هاتف
        r = client.post(
            "/pos/set-context",
            data={
                "context_type": "EXTERNAL",
                "customer_phone": "0911555000",
                "customer_name": "عميل دفع كامل",
            },
            follow_redirects=False,
        )
        check(r.status_code == 302, "set-context EXTERNAL لاختبار checkout")
        # أَضف صنفاً
        r = client.post(
            "/pos/add-line",
            data={"product_id": prod.id, "quantity": "3"},
            follow_redirects=False,
        )
        check(r.status_code in (200, 302), f"add-line ({r.status_code})")
        # checkout
        r = client.post(
            "/pos/checkout",
            data={"payment_method_id": pm.id, "pay_mode": "now"},
            follow_redirects=False,
        )
        check(r.status_code == 302, f"checkout ({r.status_code})")
        # تحقق من النقاط
        c_paid = get_by_phone(db, "0911555000")
        check(c_paid is not None, "العميل الجديد مُنشأ")
        check(
            c_paid.points_balance >= Decimal("30"),
            f"تم منح نقاط الولاء (الرصيد: {c_paid.points_balance})",
        )
        check(c_paid.visits_count >= 1, "زيارة محتسبة")

        section("POS /charge-room: قيد على حساب شقة بدون شاشة دفع")
        # نظّف وابدأ من جديد
        client.post("/pos/cancel-draft", follow_redirects=False)
        client.get("/pos")
        # أنشئ غرفة اختبار
        from modules.hotel.models import HotelRoom as HR

        room_cr = (
            db.query(HR).filter(HR.number == "TEST-CHARGE-1").first()
        )
        if room_cr is None:
            room_cr = HR(number="TEST-CHARGE-1", is_active=True)
            db.add(room_cr)
            db.commit()
        # اضبط السياق على ROOM
        r = client.post(
            "/pos/set-context",
            data={"context_type": "ROOM", "room_id": room_cr.id},
            follow_redirects=False,
        )
        check(r.status_code == 302, "set-context ROOM لاختبار charge-room")
        # أَضف صنفاً (نفس صنف الاختبار)
        r = client.post(
            "/pos/add-line",
            data={"product_id": prod.id, "quantity": "2"},
            follow_redirects=False,
        )
        check(r.status_code in (200, 302), "add-line في سياق ROOM")

        # محاولة الـ checkout بدون اسم نزيل → يجب أن تفشل (و RoomCharge لا يُنشأ)
        from modules.hotel.models import RoomCharge as RC0

        before = (
            db.query(RC0).filter(RC0.room_id == room_cr.id).count()
        )
        r = client.post(
            "/pos/charge-room",
            data={"guest_name": ""},
            follow_redirects=False,
        )
        loc = r.headers.get("location", "")
        after = db.query(RC0).filter(RC0.room_id == room_cr.id).count()
        check(
            r.status_code == 302 and "ctx_err" in loc and after == before,
            f"رفض القيد بدون اسم نزيل (لم يُنشأ charge: {before}→{after})",
        )

        # محاولة صحيحة
        r = client.post(
            "/pos/charge-room",
            data={"guest_name": "النزيل التجريبي", "note": "وجبة عشاء"},
            follow_redirects=False,
        )
        loc = r.headers.get("location", "")
        check(
            r.status_code == 302 and "ok=1" in loc and "room=1" in loc,
            f"قيد ناجح على حساب الشقة ({loc[:60]}...)",
        )

        # تحقق من إنشاء RoomCharge
        from modules.hotel.models import RoomCharge as RC

        new_charges = (
            db.query(RC)
            .filter(RC.room_id == room_cr.id, RC.is_settled.is_(False))
            .all()
        )
        check(
            len(new_charges) >= 1,
            f"تم إنشاء RoomCharge ({len(new_charges)})",
        )
        if new_charges:
            check(
                new_charges[0].guest_name_snapshot == "النزيل التجريبي",
                "اسم النزيل محفوظ في snapshot",
            )

        # تحقق أنه لا يوجد SalePayment للفاتورة الجديدة (غير مدفوعة)
        from modules.payments.models import SalePayment as SP

        for ch in new_charges:
            payments = (
                db.query(SP).filter(SP.sale_id == ch.sale_id).all()
            )
            check(
                len(payments) == 0,
                f"الفاتورة #{ch.sale_id} غير مدفوعة (count={len(payments)})",
            )

        # تنظيف غرفة الاختبار + الـ charges
        for ch in db.query(RC).filter(RC.room_id == room_cr.id).all():
            # افصل المرجع عن الفاتورة قبل الحذف
            db.delete(ch)
        db.commit()
        db.delete(room_cr)
        db.commit()

        section("POS: السياق ROOM يستلزم صلاحية HOTEL_CHARGE")
        # admin له كل الصلاحيات، نختبر أن الـ context_type يُضبط
        # نحتاج غرفة فعلية
        from modules.hotel.models import HotelRoom

        room = (
            db.query(HotelRoom).filter(HotelRoom.number == "TEST-CTX-1").first()
        )
        if room is None:
            room = HotelRoom(number="TEST-CTX-1", is_active=True)
            db.add(room)
            db.commit()
        client.post("/pos/cancel-draft", follow_redirects=False)
        client.get("/pos")
        r = client.post(
            "/pos/set-context",
            data={
                "context_type": "ROOM",
                "room_id": room.id,
                "customer_name": "نزيل HTTP",
            },
            follow_redirects=False,
        )
        check(r.status_code == 302, f"set-context ROOM ({r.status_code})")
        # جلب آخر مسودة
        draft = (
            db.query(SaleM)
            .filter(SaleM.status == SS.DRAFT)
            .order_by(SaleM.id.desc())
            .first()
        )
        db.refresh(draft)
        check(
            draft.context_type == SaleContext.ROOM
            or draft.context_type.value == "ROOM",
            "context_type=ROOM",
        )

        # تنظيف: ألغِ المسودة
        client.post("/pos/cancel-draft", follow_redirects=False)
        # احذف غرفة الاختبار
        from modules.hotel.models import RoomCharge

        db.query(RoomCharge).filter(RoomCharge.room_id == room.id).delete(
            synchronize_session=False
        )
        db.delete(room)
        db.commit()

        section("تنظيف نهائي للأثر التجريبي")
        # 1) احذف صنف الاختبار
        _cleanup_test_product(db)
        check(
            db.query(Product)
            .filter(Product.name_ar == "__TEST_LOYALTY_PRODUCT__")
            .first()
            is None,
            "حذف صنف الاختبار",
        )
        # 2) احذف العملاء التجريبيين + معاملات النقاط (مع فصل الفواتير)
        from modules.customers.models import LoyaltyTransaction as LT
        from modules.sales.models import Sale as SaleM2

        for c in (
            db.query(Customer).filter(Customer.phone.like("0911%")).all()
        ):
            for s in db.query(SaleM2).filter(SaleM2.customer_id == c.id).all():
                s.customer_id = None
            db.flush()
            db.query(LT).filter(LT.customer_id == c.id).delete(
                synchronize_session=False
            )
            db.delete(c)
        db.commit()
        leftover = (
            db.query(Customer).filter(Customer.phone.like("0911%")).count()
        )
        check(leftover == 0, f"حذف العملاء التجريبيين (متبقّي={leftover})")
        # 3) أعِد إعدادات الولاء للقيم الافتراضية
        set_setting(db, "loyalty_enabled", "1")
        set_setting(db, "loyalty_earn_per_dinar", "1")
        set_setting(db, "loyalty_redeem_value_per_point", "0.1")
        set_setting(db, "loyalty_min_points_to_redeem", "50")
        db.commit()
        check(True, "إعادة إعدادات الولاء للافتراضي")

    finally:
        # تنظيف ضماني: يحدث حتى لو فشل الاختبار في المنتصف
        try:
            _cleanup_test_product(db)
            from modules.customers.models import LoyaltyTransaction as _LT
            from modules.sales.models import Sale as _SaleM

            for c in (
                db.query(Customer).filter(Customer.phone.like("0911%")).all()
            ):
                for s in (
                    db.query(_SaleM).filter(_SaleM.customer_id == c.id).all()
                ):
                    s.customer_id = None
                db.flush()
                db.query(_LT).filter(_LT.customer_id == c.id).delete(
                    synchronize_session=False
                )
                db.delete(c)
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        db.close()

    print()
    print("=" * 60)
    print(f"  PASS: {PASS}    FAIL: {FAIL}")
    print("=" * 60)
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
