"""اختبار أمان POS عند سياق الشقة (الإصلاح الجديد):

1) دخول تبويب «شقة» (POST /pos/set-context مع context_type=ROOM بدون room_id)
   - السياق يتغيّر إلى ROOM
   - لا يظهر خطأ
   - draft_room_id غير موجود في الجلسة

2) محاولة إضافة صنف في سياق ROOM بدون اختيار شقة
   - يجب أن تُرفض (400) ورسالة خطأ واضحة
   - SaleLine جديد لا يُضاف

3) اختيار شقة + إضافة صنف
   - ينجح كالمعتاد

4) تبديل التبويب بين TABLE و ROOM
   - الانتقال من ROOM (بشقة مختارة) إلى TABLE → يُفرَّغ draft_room_id

5) room_id غير صالح (مثلاً 99999)
   - يرفع خطأ مع redirect ctx_err، ولا يُحفظ شيء في الجلسة
"""
from __future__ import annotations

import os
import sys

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

from decimal import Decimal  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.main import app  # noqa: E402
from infra.db import get_session_factory  # noqa: E402
from modules.catalog.models import (  # noqa: E402
    Product,
    ProductCategory,
    ProductKind,
)
from modules.hotel.models import HotelRoom  # noqa: E402
from modules.sales.models import Sale, SaleContext  # noqa: E402

PASS = 0
FAIL = 0

TEST_PRODUCT_NAME = "__POS_ROOM_SAFETY_TEST__"
TEST_ROOM_NUMBER = "TEST-SAFE-101"


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"  ✓ {msg}")


def fail(msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  ✗ {msg}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ============================================================
# Setup: غرفة + صنف للاختبار
# ============================================================
Session = get_session_factory()
db = Session()
try:
    # غرفة
    room = db.execute(
        select(HotelRoom).where(HotelRoom.number == TEST_ROOM_NUMBER)
    ).scalar_one_or_none()
    if room is None:
        room = HotelRoom(number=TEST_ROOM_NUMBER, is_active=True)
        db.add(room)
        db.flush()
    test_room_id = room.id

    # صنف
    cat = db.execute(select(ProductCategory).limit(1)).scalar_one_or_none()
    if cat is None:
        cat = ProductCategory(name_ar="فئة اختبار شقة")
        db.add(cat)
        db.flush()

    p = db.execute(
        select(Product).where(Product.name_ar == TEST_PRODUCT_NAME)
    ).scalar_one_or_none()
    if p is None:
        p = Product(
            name_ar=TEST_PRODUCT_NAME,
            sell_price=Decimal("10.000"),
            is_active=True,
            category_id=cat.id,
            kind=ProductKind.FINAL_SELLABLE,
        )
        db.add(p)
        db.flush()
    else:
        p.is_active = True
    test_product_id = p.id
    db.commit()
    print(
        f"setup: room id={test_room_id} number={TEST_ROOM_NUMBER}, "
        f"product id={test_product_id}"
    )
finally:
    db.close()


# ============================================================
# HTTP client + login
# ============================================================
client = TestClient(app)
r = client.post(
    "/auth/login",
    data={"username": "admin", "password": "admin123"},
    follow_redirects=False,
)
if r.status_code not in (302, 303):
    print(f"❌ login failed: {r.status_code}")
    sys.exit(1)


def get_active_sale_id(c: TestClient) -> int | None:
    """يستخرج draft_sale_id من الجلسة عبر زيارة /pos."""
    rr = c.get("/pos")
    assert rr.status_code == 200
    # ابحث في الـ HTML عن "مسودة #N"
    import re
    m = re.search(r"مسودة #(\d+)", rr.text)
    return int(m.group(1)) if m else None


def get_sale_ctx_and_lines(sale_id: int) -> tuple[str, int]:
    """يعيد (context_type.value, lines_count) من القاعدة."""
    Session = get_session_factory()
    dbx = Session()
    try:
        s = dbx.get(Sale, sale_id)
        assert s is not None
        ctx_val = s.context_type.value if s.context_type else "TABLE"
        from modules.sales.models import SaleLine
        n = (
            dbx.execute(select(SaleLine).where(SaleLine.sale_id == sale_id))
            .scalars()
            .all()
        )
        return ctx_val, len(n)
    finally:
        dbx.close()


# ============================================================
# 1) دخول تبويب ROOM بدون room_id يجب أن ينجح
# ============================================================
section("1) فتح تبويب «شقة» بدون اختيار شقة (تنظيف الحالة)")

# ابدأ بسلة جديدة لضمان فحص نظيف
client.post("/pos/cancel-draft", follow_redirects=False)
sale_id = get_active_sale_id(client)
print(f"  → sale_id = {sale_id}")

r = client.post(
    "/pos/set-context",
    data={"context_type": "ROOM"},  # بدون room_id
    follow_redirects=False,
)
if r.status_code in (302, 303):
    loc = r.headers.get("location", "")
    if "ctx_err" not in loc:
        ok("لا خطأ عند فتح تبويب الشقة بدون اختيار")
    else:
        fail(f"ظهر ctx_err غير متوقع: {loc}")
else:
    fail(f"status غير متوقع: {r.status_code}")

ctx_val, n_lines = get_sale_ctx_and_lines(sale_id)
if ctx_val == "ROOM":
    ok(f"context_type أصبح ROOM (وليس {ctx_val})")
else:
    fail(f"context_type يجب أن يكون ROOM لكنه {ctx_val}")


# ============================================================
# 2) محاولة إضافة صنف بدون شقة → رفض
# ============================================================
section("2) إضافة صنف بدون اختيار شقة → يجب أن تُرفض")

r = client.post(
    "/pos/add-line",
    data={"product_id": str(test_product_id), "quantity": "1"},
    follow_redirects=False,
)
if r.status_code == 400:
    ok(f"الإضافة رُفضت بـ 400 (status={r.status_code})")
else:
    fail(f"كان يجب رفض الإضافة بـ 400 لكن status={r.status_code}")

if "اختر الشقة" in r.text or "اختر شقة" in r.text:
    ok("ظهرت رسالة «اختر الشقة أولاً»")
else:
    fail("لم تظهر رسالة «اختر الشقة» في الرد")

ctx_val, n_lines = get_sale_ctx_and_lines(sale_id)
if n_lines == 0:
    ok(f"لا أصناف أُضيفت للسلة (lines={n_lines})")
else:
    fail(f"أصناف أُضيفت رغم عدم اختيار الشقة! (lines={n_lines})")


# ============================================================
# 3) اختيار شقة + إضافة صنف → ينجح
# ============================================================
section("3) اختيار شقة ثم إضافة صنف → يجب أن ينجح")

r = client.post(
    "/pos/set-context",
    data={"context_type": "ROOM", "room_id": str(test_room_id)},
    follow_redirects=False,
)
if r.status_code in (302, 303):
    ok("اختيار الشقة نجح")
else:
    fail(f"اختيار الشقة فشل status={r.status_code}")

r = client.post(
    "/pos/add-line",
    data={"product_id": str(test_product_id), "quantity": "1"},
    follow_redirects=False,
)
if r.status_code in (200, 302):
    ok(f"الإضافة نجحت بعد اختيار الشقة (status={r.status_code})")
else:
    fail(f"الإضافة فشلت رغم اختيار الشقة status={r.status_code}: {r.text[:150]}")

ctx_val, n_lines = get_sale_ctx_and_lines(sale_id)
if n_lines == 1:
    ok(f"الصنف أُضيف بنجاح (lines={n_lines})")
else:
    fail(f"كان يجب أن يكون lines=1 لكنه {n_lines}")


# ============================================================
# 4) تبديل التبويب من ROOM إلى TABLE يُفرّغ الشقة
# ============================================================
section("4) الانتقال إلى تبويب TABLE يُفرّغ اختيار الشقة")

r = client.post(
    "/pos/set-context",
    data={"context_type": "TABLE"},
    follow_redirects=False,
)
ctx_val, _ = get_sale_ctx_and_lines(sale_id)
if ctx_val == "TABLE":
    ok("context رجع إلى TABLE")
else:
    fail(f"context ما زال {ctx_val}")

# ارجع إلى ROOM بدون room_id → يجب أن لا يظهر «الشقة 101» مختارة
r = client.post(
    "/pos/set-context",
    data={"context_type": "ROOM"},
    follow_redirects=False,
)
r2 = client.get("/pos")
# نتحقق أن الـ select يحتوي «— اختر شقة —» مع selected
if 'value="" selected' in r2.text or "اختر شقة" in r2.text:
    ok("الـ dropdown يبدأ بـ «اختر شقة» (لا اختيار افتراضي)")
else:
    fail("لم تظهر العلامة الافتراضية «— اختر شقة —» في الـ select")


# ============================================================
# 5) room_id غير صالح
# ============================================================
section("5) room_id غير صالح يُعطي ctx_err")

r = client.post(
    "/pos/set-context",
    data={"context_type": "ROOM", "room_id": "999999"},
    follow_redirects=False,
)
loc = r.headers.get("location", "") if r.status_code in (302, 303) else ""
# نفك تشفير URL لأن النص العربي يأتي %D8%A7… في الـ Location
from urllib.parse import unquote  # noqa: E402
loc_decoded = unquote(loc)
if "ctx_err" in loc and ("الشقة" in loc_decoded or "غير صالح" in loc_decoded):
    ok(f"رسالة خطأ ظهرت: {loc_decoded[:80]}…")
else:
    fail(f"كان يجب أن تظهر ctx_err للشقة غير الصالحة: {loc_decoded}")


# ============================================================
# Cleanup
# ============================================================
section("تنظيف الأثر التجريبي")
db = Session()
try:
    # ألغِ السلة الحالية
    client.post("/pos/cancel-draft", follow_redirects=False)
    # احذف الصنف
    p = db.execute(
        select(Product).where(Product.name_ar == TEST_PRODUCT_NAME)
    ).scalar_one_or_none()
    if p is not None:
        from modules.sales.models import SaleLine
        from modules.inventory.models import StockMovement
        # احذف أي SaleLine يستخدم هذا المنتج
        for sl in (
            db.execute(select(SaleLine).where(SaleLine.product_id == p.id))
            .scalars()
            .all()
        ):
            db.delete(sl)
        # احذف أي StockMovement
        for sm in (
            db.execute(
                select(StockMovement).where(StockMovement.product_id == p.id)
            )
            .scalars()
            .all()
        ):
            db.delete(sm)
        db.flush()
        db.delete(p)
    # احذف الغرفة (إن لم يكن لها سجل)
    rm = db.execute(
        select(HotelRoom).where(HotelRoom.number == TEST_ROOM_NUMBER)
    ).scalar_one_or_none()
    if rm is not None:
        from modules.hotel.models import RoomCharge
        for ch in (
            db.execute(select(RoomCharge).where(RoomCharge.room_id == rm.id))
            .scalars()
            .all()
        ):
            db.delete(ch)
        db.flush()
        db.delete(rm)
    db.commit()
    ok("تم تنظيف الصنف والغرفة التجريبية")
except Exception as e:
    db.rollback()
    print(f"  ⚠ تنظيف فشل: {type(e).__name__}: {e}")
finally:
    db.close()


# ============================================================
# Result
# ============================================================
print(f"\n{'=' * 50}")
print(f"  PASS: {PASS}    FAIL: {FAIL}")
print("=" * 50)
sys.exit(0 if FAIL == 0 else 1)
