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
from infra.db import get_engine, get_session_factory  # noqa: E402
from infra.sqlite_patch import patch_sqlite_schema  # noqa: E402
from modules.authz.service import get_user_by_username  # noqa: E402
from modules.catalog.models import (  # noqa: E402
    BillOfMaterialsLine,
    DiningTable,
    Product,
    ProductCategory,
    ProductKind,
)
from modules.customers.models import Customer, LoyaltyTransaction  # noqa: E402
from modules.customers.service import create_customer, grant_points_for_sale  # noqa: E402
from modules.hotel.models import HotelRoom, RoomCharge  # noqa: E402
from modules.hotel.service import open_room_charge, room_open_total, settle_charges  # noqa: E402
from modules.inventory.models import StockBalance, StockMovement  # noqa: E402
from modules.payments.models import (  # noqa: E402
    PaymentMethod,
    PaymentMethodKind,
    PaymentTransfer,
    Purchase,
    PurchaseLine,
    RefundPayment,
    SalePayment,
)
from modules.payments.service import (  # noqa: E402
    create_payment_method,
    get_sale_payment,
    list_payment_methods,
    record_inventory_purchase,
    record_sale_payment,
    wallet_breakdown,
)
from modules.refunds.models import SaleReturn  # noqa: E402
from modules.refunds.models import SaleReturnLine  # noqa: E402
from modules.refunds.service import (  # noqa: E402
    create_sale_return,
    sale_returned_total,
)
from modules.reporting import queries as report_queries  # noqa: E402
from modules.sales.models import Sale, SaleLine  # noqa: E402
from modules.sales.models import KitchenTicket  # noqa: E402
from modules.sales.service import add_line_to_sale, complete_sale, create_draft_sale  # noqa: E402

PASS = 0
FAIL = 0

TAG = "__RFND_TEST__"
TEST_PHONE = "999888777123"
TEST_TS = datetime(2035, 5, 6, 10, 0, tzinfo=timezone.utc)
PERIOD_START = TEST_TS.replace(hour=0, minute=0, second=0, microsecond=0)
PERIOD_END = PERIOD_START + timedelta(days=1)


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


def fmt(value: Decimal) -> str:
    return f"{Decimal(str(value)).quantize(Decimal('0.001'))}"


def must(cond: bool, msg: str) -> None:
    if cond:
        ok(msg)
    else:
        fail(msg)


def get_or_create_category(db):
    cat = db.execute(
        select(ProductCategory).where(ProductCategory.name_ar == f"{TAG}-CAT")
    ).scalar_one_or_none()
    if cat is None:
        cat = ProductCategory(name_ar=f"{TAG}-CAT")
        db.add(cat)
        db.flush()
    return cat


def get_or_create_method(db, name: str, kind: PaymentMethodKind) -> PaymentMethod:
    method = db.execute(
        select(PaymentMethod).where(PaymentMethod.name_ar == name)
    ).scalar_one_or_none()
    if method is None:
        method = create_payment_method(db, name_ar=name, kind=kind, sort_order=900)
        db.flush()
    method.is_active = True
    db.flush()
    return method


def create_sale_with_payment(
    db,
    *,
    user_id: int,
    product_id: int,
    qty: Decimal,
    payment_method_id: int,
    created_at: datetime,
    customer_id: int | None = None,
) -> Sale:
    sale = create_draft_sale(db, user_id)
    sale.created_at = created_at
    if customer_id:
        sale.customer_id = customer_id
    add_line_to_sale(db, sale.id, product_id, qty, user_id)
    complete_sale(db, sale.id, user_id)
    payment = record_sale_payment(db, sale.id, payment_method_id, sale.total)
    payment.created_at = created_at
    if customer_id:
        customer = db.get(Customer, customer_id)
        assert customer is not None
        grant_points_for_sale(
            db,
            customer=customer,
            sale_id=sale.id,
            sale_total=sale.total,
            user_id=user_id,
        )
    db.flush()
    return sale


Session = get_session_factory()


def cleanup_test_data() -> None:
    dbx = Session()
    try:
        final = dbx.execute(
            select(Product).where(Product.name_ar == f"{TAG}-FINAL")
        ).scalar_one_or_none()
        component = dbx.execute(
            select(Product).where(Product.name_ar == f"{TAG}-COMP")
        ).scalar_one_or_none()
        sale_ids: list[int] = []
        if final is not None:
            sale_ids = [
                int(sid)
                for sid in dbx.scalars(
                    select(SaleLine.sale_id).where(SaleLine.product_id == final.id)
                ).all()
            ]
        customer = dbx.execute(
            select(Customer).where(Customer.phone == TEST_PHONE)
        ).scalar_one_or_none()
        if sale_ids:
            dbx.execute(
                update(Sale).where(Sale.id.in_(sale_ids)).values(customer_id=None)
            )
            dbx.execute(delete(LoyaltyTransaction).where(LoyaltyTransaction.sale_id.in_(sale_ids)))
            if customer is not None:
                dbx.execute(
                    delete(LoyaltyTransaction).where(
                        LoyaltyTransaction.customer_id == customer.id
                    )
                )
            return_ids = [
                rid
                for rid in dbx.scalars(
                    select(SaleReturn.id).where(SaleReturn.original_sale_id.in_(sale_ids))
                ).all()
            ]
            if return_ids:
                dbx.execute(delete(PaymentTransfer).where(PaymentTransfer.sale_return_id.in_(return_ids)))
                dbx.execute(delete(RefundPayment).where(RefundPayment.sale_return_id.in_(return_ids)))
                dbx.execute(delete(SaleReturnLine).where(SaleReturnLine.sale_return_id.in_(return_ids)))
                dbx.execute(delete(SaleReturn).where(SaleReturn.id.in_(return_ids)))
            dbx.execute(delete(SalePayment).where(SalePayment.sale_id.in_(sale_ids)))
            dbx.execute(delete(KitchenTicket).where(KitchenTicket.sale_id.in_(sale_ids)))
            dbx.execute(delete(RoomCharge).where(RoomCharge.sale_id.in_(sale_ids)))
            dbx.execute(delete(SaleLine).where(SaleLine.sale_id.in_(sale_ids)))
            dbx.execute(delete(Sale).where(Sale.id.in_(sale_ids)))
        if final is not None:
            dbx.execute(
                delete(BillOfMaterialsLine).where(
                    BillOfMaterialsLine.parent_product_id == final.id
                )
            )
        purchase_ids = [
            int(pid)
            for pid in dbx.scalars(
                select(Purchase.id).where(Purchase.supplier == f"{TAG} Supplier")
            ).all()
        ]
        if purchase_ids:
            dbx.execute(delete(PurchaseLine).where(PurchaseLine.purchase_id.in_(purchase_ids)))
            dbx.execute(delete(Purchase).where(Purchase.id.in_(purchase_ids)))
        if component is not None or final is not None:
            ids = [pid for pid in [component.id if component else None, final.id if final else None] if pid]
            dbx.execute(delete(StockMovement).where(StockMovement.product_id.in_(ids)))
            dbx.execute(delete(StockBalance).where(StockBalance.product_id.in_(ids)))
        if final is not None:
            dbx.delete(final)
        if component is not None:
            dbx.delete(component)
        room = dbx.execute(
            select(HotelRoom).where(HotelRoom.number == f"{TAG}-ROOM")
        ).scalar_one_or_none()
        if room is not None:
            dbx.delete(room)
        if customer is not None:
            dbx.delete(customer)
        dbx.execute(
            delete(PaymentMethod).where(
                PaymentMethod.name_ar.in_([f"{TAG}-CASH", f"{TAG}-BANK"])
            )
        )
        cat = dbx.execute(
            select(ProductCategory).where(ProductCategory.name_ar == f"{TAG}-CAT")
        ).scalar_one_or_none()
        if cat is not None:
            dbx.delete(cat)
        dbx.commit()
    except Exception:
        dbx.rollback()
    finally:
        dbx.close()


cleanup_test_data()
patch_sqlite_schema(get_engine())
db = Session()
sale1_id = sale2_id = sale3_id = None
cash_id = bank_id = None
component_id = final_id = room_id = None
try:
    admin = get_user_by_username(db, "admin")
    if admin is None:
        print("❌ admin user missing")
        sys.exit(1)
    user_id = admin.id

    cat = get_or_create_category(db)
    cash = get_or_create_method(db, f"{TAG}-CASH", PaymentMethodKind.CASH)
    bank = get_or_create_method(db, f"{TAG}-BANK", PaymentMethodKind.BANK)
    cash_id = cash.id
    bank_id = bank.id

    component = db.execute(
        select(Product).where(Product.name_ar == f"{TAG}-COMP")
    ).scalar_one_or_none()
    if component is None:
        component = Product(
            name_ar=f"{TAG}-COMP",
            unit="وحدة",
            kind=ProductKind.STOCK_ONLY,
            sell_price=None,
            is_active=True,
            category_id=cat.id,
        )
        db.add(component)
        db.flush()
    component_id = component.id

    final = db.execute(
        select(Product).where(Product.name_ar == f"{TAG}-FINAL")
    ).scalar_one_or_none()
    if final is None:
        final = Product(
            name_ar=f"{TAG}-FINAL",
            unit="قطعة",
            kind=ProductKind.FINAL_SELLABLE,
            sell_price=Decimal("20.000"),
            is_active=True,
            category_id=cat.id,
        )
        db.add(final)
        db.flush()
    final_id = final.id

    bom = db.execute(
        select(BillOfMaterialsLine).where(
            BillOfMaterialsLine.parent_product_id == final.id,
            BillOfMaterialsLine.component_product_id == component.id,
        )
    ).scalar_one_or_none()
    if bom is None:
        bom = BillOfMaterialsLine(
            parent_product_id=final.id,
            component_product_id=component.id,
            qty_per_parent=Decimal("2.0000"),
        )
        db.add(bom)
        db.flush()
    else:
        bom.qty_per_parent = Decimal("2.0000")

    customer = db.execute(
        select(Customer).where(Customer.phone == TEST_PHONE)
    ).scalar_one_or_none()
    if customer is None:
        customer = create_customer(db, phone=TEST_PHONE, name=f"{TAG} Customer")
    room = db.execute(
        select(HotelRoom).where(HotelRoom.number == f"{TAG}-ROOM")
    ).scalar_one_or_none()
    if room is None:
        room = HotelRoom(number=f"{TAG}-ROOM", is_active=True)
        db.add(room)
        db.flush()
    room_id = room.id

    from modules.inventory.service import get_main_warehouse

    purchase = record_inventory_purchase(
        db,
        payment_method_id=cash.id,
        supplier=f"{TAG} Supplier",
        note="تهيئة مخزون المرتجع",
        lines=[(component.id, Decimal("100.0000"), Decimal("5.000"))],
        user_id=user_id,
        warehouse_id=get_main_warehouse(db).id,
        created_at=TEST_TS - timedelta(days=30),
    )
    purchase.created_at = TEST_TS - timedelta(days=30)

    sale1 = create_sale_with_payment(
        db,
        user_id=user_id,
        product_id=final.id,
        qty=Decimal("2.0000"),
        payment_method_id=cash.id,
        created_at=TEST_TS,
        customer_id=customer.id,
    )
    sale1_id = sale1.id

    sale2 = create_sale_with_payment(
        db,
        user_id=user_id,
        product_id=final.id,
        qty=Decimal("1.0000"),
        payment_method_id=cash.id,
        created_at=TEST_TS + timedelta(minutes=5),
    )
    sale2_id = sale2.id

    sale3 = create_draft_sale(db, user_id)
    sale3.created_at = TEST_TS + timedelta(minutes=10)
    add_line_to_sale(db, sale3.id, final.id, Decimal("1.0000"), user_id)
    complete_sale(db, sale3.id, user_id)
    sale3_id = sale3.id
    rc = open_room_charge(
        db,
        sale_id=sale3.id,
        room_id=room.id,
        guest_name=f"{TAG} Guest",
        note="room charge",
        user_id=user_id,
    )
    rc.created_at = TEST_TS + timedelta(minutes=10)
    db.commit()

    section("1) المرتجع الجزئي المدفوع بنفس الوسيلة")
    sale1_line = db.execute(
        select(SaleLine).where(SaleLine.sale_id == sale1.id)
    ).scalar_one()
    ret1 = create_sale_return(
        db,
        sale_id=sale1.id,
        lines=[(sale1_line.id, Decimal("1.0000"))],
        created_by_id=user_id,
        reason="partial",
        refund_payment_method_id=cash.id,
    )
    ret1.created_at = TEST_TS + timedelta(minutes=20)
    rp1 = db.execute(
        select(RefundPayment).where(RefundPayment.sale_return_id == ret1.id)
    ).scalar_one()
    rp1.created_at = TEST_TS + timedelta(minutes=20)
    db.commit()
    db.refresh(customer)
    bal = db.get(StockBalance, component.id)
    must(ret1.total == Decimal("20.000"), "إجمالي المرتجع الجزئي صحيح")
    must(bal is not None and bal.quantity == Decimal("94.0000"), "المخزون عاد بمكوّنات الـ BOM بعد المرتجع الجزئي")
    must(rp1.payment_method_id == cash.id, "رد المبلغ بنفس الوسيلة الأصلية")
    must(
        Decimal(str(customer.points_balance or 0)) == Decimal("20.000"),
        "تم عكس نقاط الولاء للجزء المرتجع",
    )

    section("2) منع الرد بوسيلة مختلفة للكاشير ثم السماح به مع تسوية")
    sale2_line = db.execute(
        select(SaleLine).where(SaleLine.sale_id == sale2.id)
    ).scalar_one()
    blocked = False
    try:
        create_sale_return(
            db,
            sale_id=sale2.id,
            lines=[(sale2_line.id, Decimal("1.0000"))],
            created_by_id=user_id,
            reason="override blocked",
            refund_payment_method_id=bank.id,
            allow_payment_override=False,
        )
    except Exception:
        db.rollback()
        blocked = True
    must(blocked, "تم منع الرد بوسيلة مختلفة دون صلاحية أعلى")

    ret2 = create_sale_return(
        db,
        sale_id=sale2.id,
        lines=[(sale2_line.id, Decimal("1.0000"))],
        created_by_id=user_id,
        reason="override ok",
        refund_payment_method_id=bank.id,
        allow_payment_override=True,
        approved_by_id=user_id,
    )
    ret2.created_at = TEST_TS + timedelta(minutes=25)
    rp2 = db.execute(
        select(RefundPayment).where(RefundPayment.sale_return_id == ret2.id)
    ).scalar_one()
    rp2.created_at = TEST_TS + timedelta(minutes=25)
    tf2 = db.execute(
        select(PaymentTransfer).where(PaymentTransfer.sale_return_id == ret2.id)
    ).scalar_one()
    tf2.created_at = TEST_TS + timedelta(minutes=25)
    db.commit()
    must(ret2.total == Decimal("20.000"), "إجمالي المرتجع المختلف الوسيلة صحيح")
    must(rp2.payment_method_id == bank.id, "تم رد المبلغ من الوسيلة الجديدة")
    must(
        tf2.from_payment_method_id == cash.id and tf2.to_payment_method_id == bank.id,
        "تم إنشاء قيد تسوية بين الوسيلتين",
    )

    section("3) فاتورة غرفة غير مسددة: تخفيض الرصيد ثم تسوية الباقي فقط")
    sale3_line = db.execute(
        select(SaleLine).where(SaleLine.sale_id == sale3.id)
    ).scalar_one()
    ret3 = create_sale_return(
        db,
        sale_id=sale3.id,
        lines=[(sale3_line.id, Decimal("0.5000"))],
        created_by_id=user_id,
        reason="room partial",
    )
    ret3.created_at = TEST_TS + timedelta(minutes=30)
    db.commit()
    room_total = room_open_total(db, room.id)
    must(ret3.total == Decimal("10.000"), "مرتجع الغرفة غير المسدد صحيح")
    must(room_total == Decimal("10.000"), "الرصيد المفتوح للغرفة انخفض بعد المرتجع")
    must(
        db.execute(
            select(RefundPayment).where(RefundPayment.sale_return_id == ret3.id)
        ).scalar_one_or_none()
        is None,
        "لم يُسجل رد مالي لمرتجع الفاتورة غير المسددة",
    )
    settle_charges(
        db,
        charge_ids=[rc.id],
        payment_method_id=cash.id,
        user_id=user_id,
    )
    payment3 = get_sale_payment(db, sale3.id)
    if payment3 is not None:
        payment3.created_at = TEST_TS + timedelta(minutes=35)
    db.commit()
    must(payment3 is not None and payment3.amount == Decimal("10.000"), "تم تحصيل المتبقي فقط عند تسوية الغرفة")

    section("4) التقارير والمحافظ والواجهة")
    summary = report_queries.sales_summary(db, PERIOD_START, PERIOD_END)
    cogs = report_queries.cogs_summary(db, PERIOD_START, PERIOD_END)
    by_pm = report_queries.sales_by_payment_method(db, PERIOD_START, PERIOD_END)
    wallets = wallet_breakdown(db, PERIOD_START, PERIOD_END)
    must(summary.gross_revenue == Decimal("80.000"), "التقرير يحسب إجمالي المبيعات قبل المرتجعات")
    must(summary.returns_total == Decimal("50.000"), "التقرير يحسب إجمالي المرتجعات")
    must(summary.net_revenue == Decimal("30.000"), "التقرير يحسب صافي المبيعات")
    must(cogs == Decimal("15.000"), "تكلفة المباع صارت صافية بعد إعادة المخزون")
    cash_row = next((row for row in by_pm if row[0] == cash.name_ar), None)
    bank_row = next((row for row in by_pm if row[0] == bank.name_ar), None)
    must(cash_row is not None and cash_row[2] == Decimal("70.000"), "تحصيل الكاش ظهر بتاريخ الدفع الفعلي")
    must(cash_row is not None and cash_row[4] == Decimal("20.000"), "رد الكاش ظهر في تقرير طرق الدفع")
    must(bank_row is not None and bank_row[4] == Decimal("20.000"), "رد المصرف ظهر في تقرير طرق الدفع")
    wallet_cash = next((row for row in wallets if row.method.id == cash.id), None)
    wallet_bank = next((row for row in wallets if row.method.id == bank.id), None)
    must(wallet_cash is not None and wallet_cash.net == Decimal("30.000"), "صافي محفظة الكاش صحيح بعد الرد والتسوية")
    must(wallet_bank is not None and wallet_bank.net == Decimal("0.000"), "التسوية حافظت على توازن محفظة المصرف")

    section("5) استرداد بدون عودة للمخزن (وجبة مطهاة)")
    sale_nr = create_sale_with_payment(
        db,
        user_id=user_id,
        product_id=final.id,
        qty=Decimal("1.0000"),
        payment_method_id=cash.id,
        created_at=TEST_TS + timedelta(minutes=40),
    )
    line_nr = db.execute(
        select(SaleLine).where(SaleLine.sale_id == sale_nr.id)
    ).scalar_one()
    bal_before_nr = db.get(StockBalance, component.id).quantity
    create_sale_return(
        db,
        sale_id=sale_nr.id,
        lines=[(line_nr.id, Decimal("1.0000"))],
        created_by_id=user_id,
        reason="وجبة لا تعاد للثلاجة",
        refund_payment_method_id=cash.id,
        line_restock={line_nr.id: False},
    )
    db.commit()
    bal_after_nr = db.get(StockBalance, component.id).quantity
    must(bal_after_nr == bal_before_nr, "عند تعطيل العودة للمخزن لا يتغير رصيد المكوّن")

    client = TestClient(app)
    login = client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=False,
    )
    must(login.status_code in (302, 303), "تسجيل الدخول للاختبار الويب نجح")
    page = client.get("/refunds")
    detail = client.get(f"/refunds/sale/{sale1.id}")
    receipt = client.get(f"/refunds/receipt/{ret1.id}")
    must(page.status_code == 200 and "استرداد وترجيع المبيعات" in page.text, "صفحة الاسترداد الرئيسية تعمل")
    must(
        detail.status_code == 200 and f"استرداد فاتورة #{sale1.id}" in detail.text,
        "تفاصيل الفاتورة للاسترداد تعمل",
    )
    must(receipt.status_code == 200 and f"استرداد #{ret1.id}" in receipt.text, "سند الاسترداد يعرض بشكل مستقل")

finally:
    section("تنظيف الأثر التجريبي")
    try:
        cleanup_test_data()
        ok("تم تنظيف بيانات اختبار المرتجعات")
    except Exception as exc:
        print(f"  ⚠ تنظيف فشل: {type(exc).__name__}: {exc}")
    finally:
        db.close()

print(f"\n{'=' * 50}")
print(f"  PASS: {PASS}    FAIL: {FAIL}")
print("=" * 50)
sys.exit(0 if FAIL == 0 else 1)
