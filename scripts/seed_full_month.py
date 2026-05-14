"""يولّد بيانات تجريبية واقعية لشهر كامل لاختبار كل وظائف النظام محاسبياً.

يدخل:
- أساليب دفع: كاش، مصرف الوحدة، مصرف التنمية.
- أصول ثابتة بمعدلات إهلاك متعددة (فرن، ثلاجة، طابعة، أثاث، مكيف).
- مستلزمات استهلاكية (أكياس، ورق، مواد تنظيف).
- تكاليف شهرية ثابتة (رواتب، إيجار، اشتراكات، إنترنت).
- فواتير شراء بضائع متعددة على مدار الشهر.
- مصروفات يومية متفرّقة (كهرباء، ماء، غاز، مواصلات، صيانة).
- مبيعات يومية متنوعة (3-12 فاتورة/يوم) بتشكيلة منتجات وأساليب دفع مختلفة.

طريقة التشغيل:
    python scripts/seed_full_month.py            # يضيف بيانات شهر سابق
    python scripts/seed_full_month.py --clear    # يصفّر الحركات أولاً ثم يُضيف
"""

from __future__ import annotations

import os
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__) + "/.."))
sys.stdout.reconfigure(encoding="utf-8")

import modules.authz.models  # noqa: F401
import modules.catalog.models  # noqa: F401
import modules.inventory.models  # noqa: F401
import modules.payments.models  # noqa: F401
import modules.sales.models  # noqa: F401
import modules.settings.models  # noqa: F401

from sqlalchemy import select

from infra.db import get_session_factory
from modules.authz.service import seed_if_empty, sync_permissions
from modules.catalog.models import Product, ProductKind
from modules.payments.daily_burden import upsert_recurring_cost
from modules.payments.models import (
    PaymentMethod,
    PaymentMethodKind,
    Purchase,
    PurchaseKind,
    PurchaseLine,
    RecurringCostCategory,
)
from modules.payments.service import (
    ensure_default_payment_methods,
    record_asset_purchase,
    record_expense,
    record_inventory_purchase,
    record_sale_payment,
)
from modules.sales.models import Sale, SaleLine, SaleStatus
from modules.sales.service import complete_sale


random.seed(42)  # نتائج قابلة لإعادة الإنتاج

# ============================================================
# 1) الإعدادات الزمنية
# ============================================================
NOW = datetime.now(timezone.utc).replace(microsecond=0)
# نوزّع البيانات على آخر 30 يوماً من اليوم (انتهاءً بالأمس)
# هكذا تظهر البيانات في فلاتر «اليوم/الأسبوع/الشهر الحالي/السنة» جميعاً.
PERIOD_END = NOW.replace(hour=23, minute=59, second=0, microsecond=0)
PERIOD_START = (PERIOD_END - timedelta(days=29)).replace(
    hour=0, minute=0, second=0, microsecond=0
)
DAYS_IN_PERIOD = (PERIOD_END.date() - PERIOD_START.date()).days + 1
# ابقاء الأسماء القديمة للتوافق مع باقي الكود
LAST_MONTH_START = PERIOD_START
LAST_MONTH_END = PERIOD_END
DAYS_IN_MONTH = DAYS_IN_PERIOD


# ============================================================
# 2) قوائم البيانات النموذجية
# ============================================================

PAYMENT_METHODS_DATA = [
    ("كاش", PaymentMethodKind.CASH, 0),
    ("مصرف الوحدة", PaymentMethodKind.BANK, 1),
    ("مصرف التنمية", PaymentMethodKind.BANK, 2),
]

# الأصول الثابتة: (اسم، وحدة، كمية، سعر/وحدة، عمر بالأشهر، خردة، أيام مضت منذ الشراء)
FIXED_ASSETS = [
    ("فرن غاز صناعي", "جهاز", 1, 5500, 84, 500, 600),  # شراء قبل ~20 شهراً
    ("ثلاجة عرض كبيرة", "جهاز", 1, 4200, 96, 400, 540),
    ("شواية فحم", "جهاز", 1, 1800, 60, 200, 480),
    ("مكيف هواء 2 طن", "جهاز", 2, 2200, 84, 200, 420),
    ("طاولات وكراسي (طقم)", "طقم", 1, 3500, 120, 350, 365),
    ("جهاز كاشير + شاشة", "جهاز", 1, 1600, 36, 100, 280),
    ("طابعة حرارية 80mm", "جهاز", 2, 350, 36, 0, 200),
    ("شاشة عرض المطبخ KDS", "جهاز", 1, 900, 36, 0, 150),
    ("ديكور المحل (إضاءة + لوحات)", "بند", 1, 2800, 84, 0, 365),
    ("سكاكين وأدوات مطبخ احترافية", "طقم", 1, 1200, 24, 0, 300),
]

# مستلزمات استهلاكية تشترى دورياً (life=0)
CONSUMABLE_ITEMS = [
    ("ورق طابعة كاشير 80mm", "لفة", 10, 12, 0),
    ("أكياس تغليف", "كيس", 500, 0.15, 0),
    ("مناديل ورقية", "علبة", 50, 1.5, 0),
    ("مواد تنظيف", "علبة", 8, 9, 0),
    ("أكواب ورقية", "علبة", 20, 6, 0),
]

# التكاليف الشهرية الثابتة (recurring costs)
RECURRING_COSTS = [
    ("راتب الشيف", RecurringCostCategory.SALARY, 1500, "راتب الشيف الرئيسي"),
    ("راتب مساعد طباخ", RecurringCostCategory.SALARY, 900, None),
    ("راتب كاشير", RecurringCostCategory.SALARY, 1000, None),
    ("راتب نادل", RecurringCostCategory.SALARY, 800, None),
    ("راتب عامل تنظيف", RecurringCostCategory.SALARY, 600, None),
    ("إيجار المحل", RecurringCostCategory.RENT, 2200, "عقد سنوي"),
    ("اشتراك إنترنت 100Mbps", RecurringCostCategory.UTILITY, 150, None),
    ("اشتراك خدمة تحصيل", RecurringCostCategory.SUBSCRIPTION, 120, None),
    ("تأمين المحل", RecurringCostCategory.INSURANCE, 80, "قسط شهري"),
    ("رخصة بلدية شهرية", RecurringCostCategory.LICENSE, 60, None),
]

# المصروفات اليومية/الدورية المتغيّرة (تُسجَّل كـ Purchase EXPENSE)
# (وصف، تصنيف، حد أدنى، حد أعلى، تكرار: "daily" / "weekly" / "monthly")
EXPENSE_TEMPLATES = [
    ("فاتورة كهرباء", "كهرباء/ماء/غاز", 350, 480, "monthly"),
    ("فاتورة ماء", "كهرباء/ماء/غاز", 70, 95, "monthly"),
    ("فاتورة غاز", "كهرباء/ماء/غاز", 55, 85, "weekly"),
    ("مواصلات/توصيل بضاعة", "مواصلات", 15, 35, "weekly"),
    ("صيانة طارئة", "صيانة", 30, 90, "weekly"),
    ("مستلزمات مكتبية", "مكتبية", 5, 25, "weekly"),
    ("ضيافة موظفين", "نفقات أخرى", 8, 18, "weekly"),
]

# قوالب فواتير شراء البضائع (تتكرر مرتين في الشهر مثلاً)
INVENTORY_PURCHASES = [
    {
        "supplier": "مخبز الجزيرة",
        "items": [
            ("خبز برجر", "120", "0.480"),
            ("خبز شيش", "80", "0.690"),
        ],
    },
    {
        "supplier": "ملحمة الإخوة",
        "items": [
            ("لحم برجر", "6000", "0.038"),
            ("لحم شيش", "5500", "0.044"),
        ],
    },
    {
        "supplier": "خضراوات السوق",
        "items": [
            ("خس", "2000", "0.005"),
            ("طماطم", "3000", "0.004"),
            ("بصل", "3000", "0.003"),
            ("فلفل ملوّن", "1500", "0.006"),
        ],
    },
    {
        "supplier": "مخزن البقالة الكبير",
        "items": [
            ("جبن شرائح", "100", "0.290"),
            ("صلصة شيش", "2500", "0.002"),
            ("بن قهوة", "1200", "0.078"),
            ("سكر", "5000", "0.0019"),
            ("علبة كولا", "60", "1.150"),
        ],
    },
]


# ============================================================
# 3) المساعدات
# ============================================================


def get_payment_method(db, name) -> PaymentMethod:
    pm = db.execute(
        select(PaymentMethod).where(PaymentMethod.name_ar == name)
    ).scalar_one_or_none()
    if pm is None:
        raise RuntimeError(f"أسلوب الدفع «{name}» غير موجود")
    return pm


def ensure_payment_methods(db) -> dict[str, PaymentMethod]:
    out = {}
    for name, kind, sort_order in PAYMENT_METHODS_DATA:
        pm = db.execute(
            select(PaymentMethod).where(PaymentMethod.name_ar == name)
        ).scalar_one_or_none()
        if pm is None:
            pm = PaymentMethod(
                name_ar=name, kind=kind, is_active=True, sort_order=sort_order
            )
            db.add(pm)
            db.flush()
            print(f"  + أسلوب دفع: {name}")
        else:
            print(f"  ~ أسلوب دفع موجود: {name}")
        out[name] = pm
    db.commit()
    return out


def get_final_products(db) -> list[Product]:
    return list(
        db.scalars(
            select(Product).where(
                Product.kind == ProductKind.FINAL_SELLABLE, Product.is_active.is_(True)
            )
        ).all()
    )


def find_component(db, name) -> Product | None:
    return db.execute(
        select(Product).where(
            Product.name_ar == name, Product.kind == ProductKind.STOCK_ONLY
        )
    ).scalar_one_or_none()


def random_pm(pms: dict[str, PaymentMethod]) -> PaymentMethod:
    """تشكيلة واقعية: 60% كاش، 25% الوحدة، 15% التنمية."""
    r = random.random()
    if r < 0.60:
        return pms["كاش"]
    elif r < 0.85:
        return pms["مصرف الوحدة"]
    return pms["مصرف التنمية"]


# ============================================================
# 4) إدخال البيانات
# ============================================================


def seed_recurring_costs(db) -> None:
    print("\n[1/6] التكاليف الشهرية الثابتة...")
    for name, cat, amount, notes in RECURRING_COSTS:
        # تخطّى إن كان موجوداً
        existing = db.execute(
            select(modules.payments.models.RecurringCost).where(
                modules.payments.models.RecurringCost.name_ar == name
            )
        ).scalar_one_or_none()
        if existing is not None:
            print(f"  ~ موجود: {name}")
            continue
        upsert_recurring_cost(
            db,
            rc_id=None,
            name_ar=name,
            category=cat,
            monthly_amount=Decimal(str(amount)),
            is_active=True,
            notes=notes,
        )
        print(f"  + {name}: {amount} د.ل/شهر")
    db.commit()


def seed_fixed_assets(db, pms) -> None:
    print("\n[2/6] الأصول الثابتة (مع الإهلاك)...")
    for name, unit, qty, unit_cost, life, salvage, days_ago in FIXED_ASSETS:
        purchase_date = NOW - timedelta(days=days_ago)
        # تجنب التكرار: ابحث ببساطة بالاسم
        existing_line = db.execute(
            select(PurchaseLine)
            .join(Purchase, Purchase.id == PurchaseLine.purchase_id)
            .where(
                PurchaseLine.item_name == f"[نموذج] {name}",
                Purchase.kind == PurchaseKind.ASSET,
            )
        ).scalar_one_or_none()
        if existing_line is not None:
            print(f"  ~ موجود: {name}")
            continue
        record_asset_purchase(
            db,
            payment_method_id=pms["مصرف الوحدة"].id,
            supplier=f"مورّد أصول {name[:15]}",
            note="بيانات نموذجية",
            lines=[
                (
                    f"[نموذج] {name}",
                    unit,
                    Decimal(str(qty)),
                    Decimal(str(unit_cost)),
                    int(life),
                    Decimal(str(salvage)),
                )
            ],
            user_id=None,
            created_at=purchase_date,
        )
        print(
            f"  + {name}: {qty}×{unit_cost} = {qty * unit_cost} د.ل، عمر {life}ش، خردة {salvage}"
        )
    db.commit()


def seed_consumables(db, pms) -> None:
    print("\n[3/6] المستلزمات الاستهلاكية...")
    # نضيف 2-3 فواتير على مدى الشهر
    for week_offset in (5, 18, 27):
        when = LAST_MONTH_START + timedelta(days=week_offset, hours=10)
        if when >= NOW:
            continue
        # عيّنة بنود
        items_subset = random.sample(CONSUMABLE_ITEMS, k=min(3, len(CONSUMABLE_ITEMS)))
        lines = []
        for name, unit, qty, unit_cost, life in items_subset:
            lines.append(
                (
                    f"[نموذج] {name}",
                    unit,
                    Decimal(str(qty)),
                    Decimal(str(unit_cost)),
                    int(life),
                    Decimal("0"),
                )
            )
        record_asset_purchase(
            db,
            payment_method_id=pms["كاش"].id,
            supplier="مكتبة المستلزمات",
            note=f"مستلزمات أسبوع {week_offset}",
            lines=lines,
            user_id=None,
            created_at=when,
        )
        total = sum((q * c) for (_, _, q, c, _, _) in lines)
        print(f"  + فاتورة بتاريخ {when.strftime('%Y-%m-%d')}: {total} د.ل")
    db.commit()


def seed_inventory_purchases(db, pms) -> int:
    print("\n[4/6] فواتير شراء البضائع (للمخزون)...")
    count = 0
    # كل قالب يتكرر مرتين خلال الشهر
    for inv_idx, tmpl in enumerate(INVENTORY_PURCHASES):
        for repeat, day_offset in enumerate([3 + inv_idx * 2, 18 + inv_idx * 2]):
            when = LAST_MONTH_START + timedelta(days=day_offset, hours=8 + repeat * 4)
            if when >= NOW:
                continue
            lines = []
            for comp_name, qty, cost in tmpl["items"]:
                comp = find_component(db, comp_name)
                if comp is None:
                    print(f"    ! المكوّن {comp_name} غير موجود — تخطٍ")
                    continue
                lines.append((comp.id, Decimal(qty), Decimal(cost)))
            if not lines:
                continue
            record_inventory_purchase(
                db,
                payment_method_id=pms["مصرف الوحدة"].id,
                supplier=tmpl["supplier"],
                note=f"شراء دوري #{repeat + 1}",
                lines=lines,
                user_id=None,
                created_at=when,
            )
            total = sum(q * c for (_, q, c) in lines)
            print(
                f"  + {when.strftime('%Y-%m-%d')} — {tmpl['supplier']}: {total:.3f} د.ل"
            )
            count += 1
    db.commit()
    return count


def seed_expenses(db, pms) -> int:
    print("\n[5/6] المصروفات الدورية...")
    count = 0
    for desc, cat, lo, hi, freq in EXPENSE_TEMPLATES:
        if freq == "monthly":
            offsets = [random.randint(20, 28)]
        elif freq == "weekly":
            offsets = [random.randint(2, 6), random.randint(9, 13), random.randint(16, 20), random.randint(23, 27)]
        else:  # daily
            offsets = list(range(1, DAYS_IN_MONTH))
        for off in offsets:
            when = LAST_MONTH_START + timedelta(days=off, hours=random.randint(9, 19))
            if when >= NOW:
                continue
            amt = Decimal(str(round(random.uniform(lo, hi), 3)))
            pm = random_pm({"كاش": pms["كاش"], "مصرف الوحدة": pms["مصرف الوحدة"], "مصرف التنمية": pms["مصرف التنمية"]})
            record_expense(
                db,
                payment_method_id=pm.id,
                amount=amt,
                expense_category=cat,
                supplier=None,
                note=desc,
                user_id=None,
                created_at=when,
            )
            count += 1
    db.commit()
    print(f"  + {count} مصروف")
    return count


def seed_sales(db, pms) -> int:
    """يولّد مبيعات يومية متنوعة عبر POS."""
    print("\n[6/6] المبيعات اليومية...")
    products = get_final_products(db)
    if not products:
        print("  ! لا توجد منتجات نهائية للبيع — شغّل seed_components.py أولاً")
        return 0

    # ضع/حدّث سعر بيع واقعي يكفي لتكوين هامش مساهمة معقول
    DEFAULT_PRICES = {"برجر": 25, "شيش": 35, "قهوة": 8, "كولا": 5}
    for p in products:
        target = DEFAULT_PRICES.get(p.name_ar)
        if target is None:
            if p.sell_price is None or p.sell_price <= 0:
                p.sell_price = Decimal("15")
            continue
        # حدّث السعر دائماً للمنتجات الأساسية لضمان أرقام مفهومة في التقارير
        p.sell_price = Decimal(str(target))
    db.commit()

    count = 0
    for day in range(DAYS_IN_MONTH):
        day_start = LAST_MONTH_START + timedelta(days=day)
        if day_start >= NOW:
            break
        # عدد فواتير اليوم: متغيّر (يومان ضعيفان وأسبوعان قوي)
        weekday = day_start.weekday()
        # أيام الويكند (خميس/جمعة/سبت) أكثر ازدحاماً
        if weekday in (3, 4, 5):
            n_sales = random.randint(8, 14)
        else:
            n_sales = random.randint(3, 8)
        for _ in range(n_sales):
            # وقت ضمن النهار (10 صباحاً - 23 مساءً)
            sale_time = day_start + timedelta(
                hours=random.randint(10, 22),
                minutes=random.randint(0, 59),
            )
            # سلة عشوائية: 1-4 منتجات
            basket_size = random.randint(1, 4)
            basket = random.sample(products, k=min(basket_size, len(products)))
            sale = Sale(
                status=SaleStatus.DRAFT,
                created_at=sale_time,
            )
            db.add(sale)
            db.flush()
            total = Decimal("0")
            for prod in basket:
                qty = Decimal(str(random.randint(1, 3)))
                price = Decimal(str(prod.sell_price))
                line_total = (qty * price).quantize(Decimal("0.001"))
                db.add(
                    SaleLine(
                        sale_id=sale.id,
                        product_id=prod.id,
                        quantity=qty,
                        unit_price=price,
                        line_total=line_total,
                    )
                )
                total += line_total
            sale.total = total.quantize(Decimal("0.001"))
            db.flush()
            try:
                complete_sale(db, sale.id, user_id=None)
            except Exception as e:  # noqa: BLE001
                # في حال نقص مخزون مكوّن (نادر بعد كل المشتريات)، نتجاهل
                db.rollback()
                continue
            # تسجيل الدفعة
            pm = random_pm(pms)
            try:
                record_sale_payment(db, sale.id, pm.id, sale.total)
                db.commit()
            except Exception:
                db.rollback()
                continue
            count += 1
        if day % 7 == 0:
            print(f"  ... يوم {day_start.strftime('%Y-%m-%d')}: {n_sales} فواتير")
    print(f"  + إجمالي الفواتير المُنشأة: {count}")
    return count


# ============================================================
# 5) التشغيل الرئيسي
# ============================================================


def main():
    clear_first = "--clear" in sys.argv

    # تأكد من وجود user/role/permission و payment methods
    Session = get_session_factory()
    db = Session()
    try:
        try:
            sync_permissions(db)
            seed_if_empty(db)
            ensure_default_payment_methods(db)
        except Exception as e:  # noqa: BLE001
            print(f"تحذير: تعذّر التهيئة: {e}")

        if clear_first:
            print("\n[!] تصفير الحركات قبل التعبئة...")
            from modules.backup.service import clear_transactions

            counts = clear_transactions(db, keep_fixed_assets=False)
            total = sum(counts.values())
            print(f"  - حُذف {total} صفّاً")

        print("=" * 60)
        print(
            f"تعبئة بيانات على آخر {DAYS_IN_PERIOD} يوماً: "
            f"{PERIOD_START.strftime('%Y-%m-%d')} → {PERIOD_END.strftime('%Y-%m-%d')}"
        )
        print("(تظهر في فلاتر اليوم/الأسبوع/الشهر/السنة جميعاً)")
        print("=" * 60)

        pms = ensure_payment_methods(db)
        seed_recurring_costs(db)
        seed_fixed_assets(db, pms)
        seed_consumables(db, pms)
        invs = seed_inventory_purchases(db, pms)
        exps = seed_expenses(db, pms)
        sales_n = seed_sales(db, pms)

        print()
        print("=" * 60)
        print("[OK] انتهى توليد البيانات")
        print(f"  - أساليب دفع: {len(pms)}")
        print(f"  - تكاليف شهرية: {len(RECURRING_COSTS)}")
        print(f"  - أصول ثابتة: {len(FIXED_ASSETS)}")
        print(f"  - فواتير شراء بضائع: {invs}")
        print(f"  - مصروفات: {exps}")
        print(f"  - مبيعات: {sales_n}")
        print()
        print("افتح:")
        print("  /                       — لوحة التحكم (تحليل التعادل + بيان الدخل)")
        print("  /reports/break-even     — تحليل التعادل اليومي")
        print("  /reports/profit         — الأرباح والخسائر")
        print("  /reports/comprehensive  — التقرير الشامل")
        print("  /reports/fixed-assets   — سجل الأصول الثابتة والإهلاك")
        print("  /admin/recurring-costs  — إدارة التكاليف الشهرية")
        print("  /admin/backup           — النسخ الاحتياطي والتصفير")
    finally:
        db.close()


if __name__ == "__main__":
    main()
