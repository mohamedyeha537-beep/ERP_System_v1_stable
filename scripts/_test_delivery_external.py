from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import modules.authz.models  # noqa: F401, E402
import modules.catalog.models  # noqa: F401, E402
import modules.customers.models  # noqa: F401, E402
import modules.delivery.models  # noqa: F401, E402
import modules.hotel.models  # noqa: F401, E402
import modules.hr.models  # noqa: F401, E402
import modules.inventory.models  # noqa: F401, E402
import modules.payments.models  # noqa: F401, E402
import modules.refunds.models  # noqa: F401, E402
import modules.sales.models  # noqa: F401, E402
import modules.settings.models  # noqa: F401, E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete, select, update  # noqa: E402

from app.main import app  # noqa: E402
from infra.db import get_session_factory  # noqa: E402
from modules.authz.service import get_user_by_username  # noqa: E402
from modules.catalog.models import Product, ProductCategory, ProductKind  # noqa: E402
from modules.customers.models import Customer  # noqa: E402
from modules.delivery.models import DeliveryCashSettlement, DeliveryZone  # noqa: E402
from modules.delivery.service import (  # noqa: E402
    create_zone,
    delivery_orders_report,
    get_default_cash_method,
    get_sale_delivery_cash_settlement,
)
from modules.payments.models import (  # noqa: E402
    PaymentMethod,
    PaymentMethodKind,
    RefundPayment,
    SalePayment,
)
from modules.payments.service import (  # noqa: E402
    create_payment_method,
    list_sale_payments,
    wallet_breakdown,
)
from modules.refunds.models import SaleReturn  # noqa: E402
from modules.refunds.service import create_sale_return  # noqa: E402
from modules.sales.models import (  # noqa: E402
    ExternalOrderType,
    KitchenTicket,
    Sale,
    SaleContext,
    SaleLine,
    SaleStatus,
)

PASS = 0
FAIL = 0
TAG = "__DLV_TEST__"
TEST_PHONE = "0917000555"


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"  ✓ {msg}")


def fail(msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  ✗ {msg}")


def must(cond: bool, msg: str) -> None:
    if cond:
        ok(msg)
    else:
        fail(msg)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.001"))


Session = get_session_factory()


def _ensure_test_product(db):
    cat = (
        db.query(ProductCategory)
        .filter(ProductCategory.parent_id.is_(None))
        .order_by(ProductCategory.id)
        .first()
    )
    if cat is None:
        cat = ProductCategory(name_ar="عام", color_hex="#64748b")
        db.add(cat)
        db.flush()
    p = db.execute(
        select(Product).where(Product.name_ar == f"{TAG}-PRODUCT")
    ).scalar_one_or_none()
    if p is None:
        p = Product(
            name_ar=f"{TAG}-PRODUCT",
            sku=f"{TAG}-SKU",
            unit="وحدة",
            sell_price=Decimal("20.000"),
            kind=ProductKind.FINAL_SELLABLE,
            category_id=cat.id,
            is_active=True,
        )
        db.add(p)
        db.flush()
    return p


def _ensure_bank_method(db) -> PaymentMethod:
    bank = db.execute(
        select(PaymentMethod).where(PaymentMethod.name_ar == f"{TAG}-BANK")
    ).scalar_one_or_none()
    if bank is None:
        bank = create_payment_method(
            db,
            name_ar=f"{TAG}-BANK",
            kind=PaymentMethodKind.BANK,
            sort_order=990,
        )
    bank.is_active = True
    db.flush()
    return bank


def _wallet_map(db):
    return {row.method.id: row for row in wallet_breakdown(db, None, None)}


def cleanup() -> None:
    db = Session()
    try:
        zone = db.execute(
            select(DeliveryZone).where(DeliveryZone.name_ar == f"{TAG}-ZONE")
        ).scalar_one_or_none()
        customer = db.execute(
            select(Customer).where(Customer.phone == TEST_PHONE)
        ).scalar_one_or_none()
        sale_ids = [
            int(sid)
            for sid in db.scalars(
                select(Sale.id).where(
                    (Sale.delivery_zone_name == f"{TAG}-ZONE")
                    | (Sale.external_order_id == f"{TAG}-ORDER")
                )
            ).all()
        ]
        if sale_ids:
            db.execute(
                update(Sale)
                .where(Sale.id.in_(sale_ids))
                .values(
                    customer_id=None,
                    delivery_zone_id=None,
                    delivery_zone_name=None,
                    delivery_fee=0,
                )
            )
            return_ids = [
                int(rid)
                for rid in db.scalars(
                    select(SaleReturn.id).where(SaleReturn.original_sale_id.in_(sale_ids))
                ).all()
            ]
            if return_ids:
                db.execute(
                    delete(RefundPayment).where(
                        RefundPayment.sale_return_id.in_(return_ids)
                    )
                )
                db.execute(delete(SaleReturn).where(SaleReturn.id.in_(return_ids)))
            db.execute(
                delete(DeliveryCashSettlement).where(
                    DeliveryCashSettlement.sale_id.in_(sale_ids)
                )
            )
            db.execute(delete(SalePayment).where(SalePayment.sale_id.in_(sale_ids)))
            db.execute(delete(KitchenTicket).where(KitchenTicket.sale_id.in_(sale_ids)))
            db.execute(delete(SaleLine).where(SaleLine.sale_id.in_(sale_ids)))
            db.execute(delete(Sale).where(Sale.id.in_(sale_ids)))
        prod = db.execute(
            select(Product).where(Product.name_ar == f"{TAG}-PRODUCT")
        ).scalar_one_or_none()
        if prod is not None:
            db.delete(prod)
        bank = db.execute(
            select(PaymentMethod).where(PaymentMethod.name_ar == f"{TAG}-BANK")
        ).scalar_one_or_none()
        if bank is not None:
            db.delete(bank)
        if customer is not None:
            db.delete(customer)
        if zone is not None:
            db.delete(zone)
        db.commit()
    finally:
        db.close()


cleanup()

db = Session()
client = TestClient(app)

try:
    admin = get_user_by_username(db, "admin")
    if admin is None:
        print("❌ admin user missing")
        raise SystemExit(1)

    product = _ensure_test_product(db)
    bank_method = _ensure_bank_method(db)
    cash_method = get_default_cash_method(db)
    if cash_method is None:
        print("❌ no active cash method found")
        raise SystemExit(1)
    zone = create_zone(
        db,
        name_ar=f"{TAG}-ZONE",
        fee=Decimal("5.000"),
        sort_order=1,
        notes="منطقة اختبار التوصيل",
    )
    db.commit()

    before_wallets = _wallet_map(db)
    cash_before = before_wallets.get(cash_method.id)
    bank_before = before_wallets.get(bank_method.id)
    cash_delivery_before = money(cash_before.delivery_fees_out if cash_before else 0)
    bank_sales_before = money(bank_before.sales_in if bank_before else 0)
    bank_refunds_before = money(bank_before.refunds_out if bank_before else 0)

    section("تسجيل الدخول وتكوين طلب خارجي توصيل")
    login = client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=False,
    )
    must(login.status_code in (302, 303), "تسجيل دخول الأدمن نجح")
    client.post("/pos/cancel-draft", follow_redirects=False)
    client.get("/pos", follow_redirects=False)
    set_ctx = client.post(
        "/pos/set-context",
        data={
            "context_type": "EXTERNAL",
            "external_order_type": "DELIVERY",
            "delivery_zone_id": str(zone.id),
            "customer_phone": TEST_PHONE,
            "customer_name": "عميل توصيل اختبار",
        },
        follow_redirects=False,
    )
    must(set_ctx.status_code == 302, "حفظ سياق الطلب الخارجي للتوصيل نجح")

    add_line = client.post(
        "/pos/add-line",
        data={"product_id": product.id, "quantity": "2"},
        follow_redirects=False,
    )
    must(add_line.status_code in (200, 302), "إضافة صنف للطلب الخارجي نجحت")

    checkout = client.post(
        "/pos/checkout",
        data={"payment_method_id": str(bank_method.id), "pay_mode": "now"},
        follow_redirects=False,
    )
    must(checkout.status_code == 302, "إتمام بيع طلب التوصيل نجح")

    sale = db.execute(
        select(Sale)
        .where(
            Sale.status == SaleStatus.COMPLETED,
            Sale.context_type == SaleContext.EXTERNAL,
            Sale.delivery_zone_name == zone.name_ar,
        )
        .order_by(Sale.id.desc())
    ).scalar_one_or_none()
    must(sale is not None, "تم إنشاء فاتورة توصيل مكتملة")
    assert sale is not None
    must(sale.external_order_type == ExternalOrderType.DELIVERY, "نوع الطلب الخارجي = توصيل")
    must(money(sale.total) == Decimal("40.000"), "قيمة الطلب للمطعم صحيحة")
    must(money(sale.delivery_fee) == Decimal("5.000"), "أجرة التوصيل حُفظت على الفاتورة")

    sale_payments = list_sale_payments(db, sale.id)
    must(len(sale_payments) == 1, "سُجلت دفعة واحدة للطلب")
    must(
        sale_payments[0].payment_method_id == bank_method.id
        and money(sale_payments[0].amount) == Decimal("40.000"),
        "دفع الطلب سُجل بالمصرف بقيمة الأصناف فقط",
    )

    settlement = get_sale_delivery_cash_settlement(db, sale.id)
    must(settlement is not None, "سُجل خصم أجرة التوصيل كحركة مستقلة")
    assert settlement is not None
    must(
        settlement.cash_method_id == cash_method.id
        and money(settlement.amount) == Decimal("5.000"),
        "أجرة التوصيل خُصمت من محفظة الكاش الافتراضية",
    )

    after_checkout_wallets = _wallet_map(db)
    cash_after = after_checkout_wallets.get(cash_method.id)
    bank_after = after_checkout_wallets.get(bank_method.id)
    must(
        money(cash_after.delivery_fees_out if cash_after else 0) - cash_delivery_before
        == Decimal("5.000"),
        "كشف المحافظ أظهر خصم أجرة التوصيل من الكاش",
    )
    must(
        money(bank_after.sales_in if bank_after else 0) - bank_sales_before
        == Decimal("40.000"),
        "كشف المحافظ أظهر تحصيل الطلب بالمصرف",
    )

    section("تقرير التوصيل")
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    rows = delivery_orders_report(db, start=day_start, end=day_end)
    row = next((r for r in rows if r.sale_id == sale.id), None)
    must(row is not None, "الطلب ظهر في تقرير التوصيل")
    if row is not None:
        must(row.zone_name == zone.name_ar, "منطقة التوصيل ظهرت في التقرير")
        must(row.delivery_fee == Decimal("5.000"), "أجرة التوصيل ظهرت في التقرير")
        must(row.payment_method_name == bank_method.name_ar, "طريقة دفع الطلب ظهرت في التقرير")

    report_page = client.get("/reports/delivery?period=day", follow_redirects=False)
    must(report_page.status_code == 200, "صفحة تقرير التوصيل تعمل")
    must(
        f"#{sale.id}" in report_page.text and zone.name_ar in report_page.text,
        "صفحة التقرير تعرض الفاتورة والمنطقة",
    )

    section("الترجيع لا يرد أجرة التوصيل")
    sale_return = create_sale_return(
        db,
        sale_id=sale.id,
        lines=[(sale.lines[0].id, Decimal("2"))],
        reason="اختبار ترجيع التوصيل",
        note="يجب أن يُرد ثمن الأصناف فقط",
        created_by_id=admin.id,
        refund_payment_method_id=bank_method.id,
        allow_payment_override=False,
    )
    db.commit()
    must(money(sale_return.total) == Decimal("40.000"), "المرتجع أعاد قيمة الأصناف فقط دون أجرة التوصيل")

    final_wallets = _wallet_map(db)
    cash_final = final_wallets.get(cash_method.id)
    bank_final = final_wallets.get(bank_method.id)
    must(
        money(cash_final.delivery_fees_out if cash_final else 0) - cash_delivery_before
        == Decimal("5.000"),
        "خصم أجرة التوصيل بقي قائماً بعد الترجيع",
    )
    must(
        money(bank_final.refunds_out if bank_final else 0) - bank_refunds_before
        == Decimal("40.000"),
        "رد قيمة الأصناف سُجل بنفس وسيلة الدفع الأصلية",
    )

finally:
    db.close()
    cleanup()

print(f"\nPASS={PASS} FAIL={FAIL}")
if FAIL:
    raise SystemExit(1)
