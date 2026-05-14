"""اختبار أمان POS عند سياق الطاولة.

يتحقق من أن النظام لا يسمح بإتمام الطلب إذا كان السياق «طاولة»
من دون اختيار طاولة، ويوجه الكاشير لاستخدام «خارجي» إن لم يكن الطلب
مربوطاً بطاولة.
"""
from __future__ import annotations

import os
import re
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
from urllib.parse import unquote  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.main import app  # noqa: E402
from infra.db import get_session_factory  # noqa: E402
from modules.catalog.models import DiningTable, Product, ProductCategory, ProductKind  # noqa: E402

PASS = 0
FAIL = 0

TEST_PRODUCT_NAME = "__POS_TABLE_SAFETY_TEST__"
TEST_TABLE_NAME = "__POS_TABLE_SAFETY_TABLE__"


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


def get_active_sale_id(c: TestClient) -> int | None:
    rr = c.get("/pos")
    assert rr.status_code == 200
    m = re.search(r"مسودة #(\d+)", rr.text)
    return int(m.group(1)) if m else None


Session = get_session_factory()
db = Session()
try:
    cat = db.execute(select(ProductCategory).limit(1)).scalar_one_or_none()
    if cat is None:
        cat = ProductCategory(name_ar="فئة اختبار طاولة")
        db.add(cat)
        db.flush()

    product = db.execute(
        select(Product).where(Product.name_ar == TEST_PRODUCT_NAME)
    ).scalar_one_or_none()
    if product is None:
        product = Product(
            name_ar=TEST_PRODUCT_NAME,
            sell_price=Decimal("10.000"),
            is_active=True,
            category_id=cat.id,
            kind=ProductKind.FINAL_SELLABLE,
        )
        db.add(product)
        db.flush()
    else:
        product.is_active = True

    table = db.execute(
        select(DiningTable).where(DiningTable.name_ar == TEST_TABLE_NAME)
    ).scalar_one_or_none()
    if table is None:
        table = DiningTable(name_ar=TEST_TABLE_NAME, is_active=True)
        db.add(table)
        db.flush()
    else:
        table.is_active = True

    test_product_id = product.id
    test_table_id = table.id
    db.commit()
finally:
    db.close()


client = TestClient(app)
login = client.post(
    "/auth/login",
    data={"username": "admin", "password": "admin123"},
    follow_redirects=False,
)
if login.status_code not in (302, 303):
    print(f"❌ login failed: {login.status_code}")
    sys.exit(1)


section("1) فتح تبويب الطاولة بدون اختيار")
client.post("/pos/cancel-draft", follow_redirects=False)
sale_id = get_active_sale_id(client)
print(f"  → sale_id = {sale_id}")

r = client.post(
    "/pos/set-context",
    data={"context_type": "TABLE"},
    follow_redirects=False,
)
if r.status_code in (302, 303):
    ok("فتح تبويب الطاولة نجح بدون أخطاء")
else:
    fail(f"فتح تبويب الطاولة فشل: {r.status_code}")

page = client.get("/pos")
if "يجب اختيار طاولة" in page.text and "خارجي" in page.text:
    ok("ظهر تنبيه واضح لاختيار طاولة أو التحويل إلى خارجي")
else:
    fail("لم يظهر تنبيه الطاولة المتوقع في واجهة POS")


section("2) إضافة صنف بدون طاولة يجب أن تُرفض")
add_line = client.post(
    "/pos/add-line",
    data={"product_id": str(test_product_id), "quantity": "1"},
    follow_redirects=False,
)
if add_line.status_code == 400:
    ok("إضافة الصنف رُفضت لعدم اختيار طاولة")
else:
    fail(f"كان يجب رفض إضافة الصنف: {add_line.status_code}")

if "اختر الطاولة" in add_line.text and "خارجي" in add_line.text:
    ok("ظهرت رسالة واضحة لاختيار طاولة أو التحويل إلى خارجي")
else:
    fail("لم تظهر رسالة المنع المتوقعة عند إضافة الصنف")


section("3) اختيار طاولة ثم إضافة الصنف")
pick_table = client.post(
    "/pos/set-context",
    data={"context_type": "TABLE", "table_id": str(test_table_id)},
    follow_redirects=False,
)
if pick_table.status_code in (302, 303):
    ok("اختيار الطاولة نجح")
else:
    fail(f"اختيار الطاولة فشل: {pick_table.status_code}")

add_line = client.post(
    "/pos/add-line",
    data={"product_id": str(test_product_id), "quantity": "1"},
    follow_redirects=False,
)
if add_line.status_code in (200, 302):
    ok("إضافة الصنف نجحت بعد اختيار الطاولة")
else:
    fail(f"كان يجب نجاح إضافة الصنف بعد اختيار الطاولة: {add_line.status_code}")


section("4) إزالة اختيار الطاولة ثم منع الدفع")
clear_table = client.post(
    "/pos/set-context",
    data={"context_type": "TABLE"},
    follow_redirects=False,
)
if clear_table.status_code in (302, 303):
    ok("تم تفريغ اختيار الطاولة")
else:
    fail(f"فشل تفريغ اختيار الطاولة: {clear_table.status_code}")

checkout_page = client.get("/pos/checkout", follow_redirects=False)
location = unquote(checkout_page.headers.get("location", ""))
if checkout_page.status_code in (302, 303) and "ctx_err" in location and "اختر الطاولة" in location:
    ok("فتح صفحة الدفع رُفض لعدم اختيار طاولة")
else:
    fail(f"كان يجب رفض صفحة الدفع، لكن النتيجة كانت: {checkout_page.status_code} {location}")

checkout_submit = client.post(
    "/pos/checkout",
    data={"payment_method_id": ""},
    follow_redirects=False,
)
location = unquote(checkout_submit.headers.get("location", ""))
if checkout_submit.status_code in (302, 303) and "ctx_err" in location and "خارجي" in location:
    ok("إتمام البيع رُفض لعدم اختيار طاولة")
else:
    fail(f"كان يجب رفض الإتمام، لكن النتيجة كانت: {checkout_submit.status_code} {location}")


section("5) اختيار طاولة ثم السماح بالانتقال للدفع")
pick_table = client.post(
    "/pos/set-context",
    data={"context_type": "TABLE", "table_id": str(test_table_id)},
    follow_redirects=False,
)
if pick_table.status_code in (302, 303):
    ok("اختيار الطاولة نجح")
else:
    fail(f"اختيار الطاولة فشل: {pick_table.status_code}")

checkout_page = client.get("/pos/checkout", follow_redirects=False)
if checkout_page.status_code == 200:
    ok("صفحة الدفع أصبحت متاحة بعد اختيار الطاولة")
else:
    fail(f"كان يجب السماح بصفحة الدفع بعد اختيار الطاولة: {checkout_page.status_code}")


section("تنظيف الأثر التجريبي")
db = Session()
try:
    client.post("/pos/cancel-draft", follow_redirects=False)

    product = db.execute(
        select(Product).where(Product.name_ar == TEST_PRODUCT_NAME)
    ).scalar_one_or_none()
    if product is not None:
        from modules.inventory.models import StockMovement
        from modules.sales.models import SaleLine

        for sale_line in (
            db.execute(select(SaleLine).where(SaleLine.product_id == product.id))
            .scalars()
            .all()
        ):
            db.delete(sale_line)
        for move in (
            db.execute(select(StockMovement).where(StockMovement.product_id == product.id))
            .scalars()
            .all()
        ):
            db.delete(move)
        db.flush()
        db.delete(product)

    table = db.execute(
        select(DiningTable).where(DiningTable.name_ar == TEST_TABLE_NAME)
    ).scalar_one_or_none()
    if table is not None:
        from modules.sales.models import Sale

        for sale in (
            db.execute(select(Sale).where(Sale.table_id == table.id))
            .scalars()
            .all()
        ):
            sale.table_id = None
        db.flush()
        db.delete(table)

    db.commit()
    ok("تم تنظيف بيانات الاختبار")
except Exception as exc:
    db.rollback()
    print(f"  ⚠ تنظيف فشل: {type(exc).__name__}: {exc}")
finally:
    db.close()


print(f"\n{'=' * 50}")
print(f"  PASS: {PASS}    FAIL: {FAIL}")
print("=" * 50)
sys.exit(0 if FAIL == 0 else 1)
