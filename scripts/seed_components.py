"""سكربت تعبئة بيانات نموذجية لمكوّنات المخزون ووصفات التركيب.

الهدف: تجربة دورة كاملة من شراء المكوّنات → تكوين البرجر/الشيش/القهوة من
هذه المكوّنات → بيع المنتج النهائي → خصم المكوّنات تلقائياً من المخزون.

ينفّذ السكربت ما يلي:
1) تنظيف فاتورة شراء يتيمة (بدون بنود) إن وُجدت.
2) إنشاء أصناف STOCK_ONLY (مكوّنات): خبز، لحم برجر، جبن، خس، طماطم، بصل،
   لحم شيش، خبز شيش، فلفل، صلصة شيش، بن قهوة، سكر، علبة كولا.
3) ربط البرجر/الشيش/القهوة/الكولا بوصفات تركيب (BOM) من تلك المكوّنات.
4) تسجيل فاتورة شراء حقيقية لمورّد «مخبز ومستلزمات» يضيف الكميات للمخزون.

طريقة التشغيل:
    python scripts/seed_components.py
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

import modules.authz.models  # noqa: F401  register users tables
import modules.catalog.models  # noqa: F401  register catalog tables
import modules.inventory.models  # noqa: F401  register inventory tables
import modules.payments.models  # noqa: F401  register payment tables
import modules.sales.models  # noqa: F401  register sales tables
from infra.db import get_session_factory
from modules.catalog.models import (
    BillOfMaterialsLine,
    Product,
    ProductKind,
    ProductUnit,
)
from modules.payments.models import (
    PaymentMethod,
    Purchase,
    PurchaseLine,
)
from modules.payments.service import record_inventory_purchase


# (name, unit, reorder_level)
COMPONENTS: list[tuple[str, str, str]] = [
    ("خبز برجر", "قطعة", "20"),
    ("لحم برجر", "جرام", "1000"),
    ("جبن شرائح", "قطعة", "20"),
    ("خس", "جرام", "500"),
    ("طماطم", "جرام", "500"),
    ("بصل", "جرام", "500"),
    ("خبز شيش", "قطعة", "10"),
    ("لحم شيش", "جرام", "1000"),
    ("فلفل ملوّن", "جرام", "300"),
    ("صلصة شيش", "ملي لتر", "500"),
    ("بن قهوة", "جرام", "500"),
    ("سكر", "جرام", "1000"),
    ("علبة كولا", "علبة", "12"),
]

# parent name → list of (component name, qty per parent)
RECIPES: dict[str, list[tuple[str, str]]] = {
    "برجر": [
        ("خبز برجر", "1"),
        ("لحم برجر", "120"),
        ("جبن شرائح", "1"),
        ("خس", "20"),
        ("طماطم", "30"),
        ("بصل", "10"),
    ],
    "شيش": [
        ("خبز شيش", "1"),
        ("لحم شيش", "200"),
        ("بصل", "30"),
        ("فلفل ملوّن", "30"),
        ("صلصة شيش", "50"),
    ],
    "قهوة": [
        ("بن قهوة", "10"),
        ("سكر", "5"),
    ],
    "كولا": [
        ("علبة كولا", "1"),
    ],
}

# (component name, qty, unit_cost) — فاتورة شراء أوّلية
INITIAL_PURCHASE: list[tuple[str, str, str]] = [
    ("خبز برجر", "100", "0.500"),
    ("لحم برجر", "5000", "0.040"),
    ("جبن شرائح", "100", "0.300"),
    ("خس", "2000", "0.005"),
    ("طماطم", "3000", "0.004"),
    ("بصل", "3000", "0.003"),
    ("خبز شيش", "60", "0.700"),
    ("لحم شيش", "6000", "0.045"),
    ("فلفل ملوّن", "1500", "0.006"),
    ("صلصة شيش", "2000", "0.002"),
    ("بن قهوة", "1000", "0.080"),
    ("سكر", "5000", "0.002"),
    ("علبة كولا", "48", "1.200"),
]


def ensure_unit(db, name_ar: str) -> None:
    if db.execute(select(ProductUnit).where(ProductUnit.name_ar == name_ar)).scalar_one_or_none() is None:
        db.add(ProductUnit(name_ar=name_ar, sort_order=99))


def get_product(db, name_ar: str) -> Product | None:
    return db.execute(
        select(Product).where(Product.name_ar == name_ar)
    ).scalar_one_or_none()


def cleanup_orphan_purchases(db) -> int:
    """يحذف فواتير شراء INVENTORY بدون أي بند (حالة شاذة من تجارب سابقة)."""
    count = 0
    for p in db.scalars(select(Purchase)).all():
        line_count = db.execute(
            select(PurchaseLine).where(PurchaseLine.purchase_id == p.id)
        ).first()
        if line_count is None and p.kind.value == "INVENTORY":
            print(f"  - حذف فاتورة يتيمة #{p.id} (المبلغ {p.amount})")
            db.delete(p)
            count += 1
    return count


def upsert_components(db) -> dict[str, Product]:
    units = {u for (_, u, _) in COMPONENTS}
    for u in units:
        ensure_unit(db, u)
    db.flush()

    out: dict[str, Product] = {}
    for name, unit, reorder in COMPONENTS:
        existing = get_product(db, name)
        if existing is not None:
            existing.kind = ProductKind.STOCK_ONLY
            existing.unit = unit
            existing.reorder_level = Decimal(reorder)
            existing.is_active = True
            out[name] = existing
            print(f"  - تحديث مكوّن: {name} ({unit})")
            continue
        p = Product(
            name_ar=name,
            unit=unit,
            kind=ProductKind.STOCK_ONLY,
            reorder_level=Decimal(reorder),
            is_active=True,
        )
        db.add(p)
        db.flush()
        out[name] = p
        print(f"  + إضافة مكوّن: {name} ({unit})")
    return out


def upsert_recipes(db, components: dict[str, Product]) -> int:
    written = 0
    for parent_name, lines in RECIPES.items():
        parent = get_product(db, parent_name)
        if parent is None:
            print(f"  ! تخطي وصفة: المنتج النهائي «{parent_name}» غير موجود")
            continue
        if parent.kind != ProductKind.FINAL_SELLABLE:
            print(f"  ! تخطي وصفة: «{parent_name}» ليس FINAL_SELLABLE")
            continue
        existing_components = {
            b.component_product_id: b
            for b in db.scalars(
                select(BillOfMaterialsLine).where(
                    BillOfMaterialsLine.parent_product_id == parent.id
                )
            ).all()
        }
        for comp_name, qty in lines:
            comp = components.get(comp_name) or get_product(db, comp_name)
            if comp is None:
                print(f"    ! المكوّن «{comp_name}» غير موجود")
                continue
            if comp.id in existing_components:
                existing_components[comp.id].qty_per_parent = Decimal(qty)
                print(f"    ~ {parent_name} ← {comp_name}: {qty} {comp.unit}")
            else:
                db.add(
                    BillOfMaterialsLine(
                        parent_product_id=parent.id,
                        component_product_id=comp.id,
                        qty_per_parent=Decimal(qty),
                    )
                )
                print(f"    + {parent_name} ← {comp_name}: {qty} {comp.unit}")
            written += 1
    return written


def initial_inventory_purchase(db, components: dict[str, Product]) -> Purchase | None:
    """فاتورة شراء أوّلية لمحفظة الكاش لتعبئة المخزون بكميات افتتاحية."""
    cash = db.execute(
        select(PaymentMethod).where(PaymentMethod.is_active.is_(True)).order_by(PaymentMethod.id)
    ).scalars().first()
    if cash is None:
        print("  ! لا توجد محفظة نشطة لتسجيل فاتورة الشراء")
        return None

    lines: list[tuple[int, Decimal, Decimal]] = []
    for name, qty, cost in INITIAL_PURCHASE:
        p = components.get(name) or get_product(db, name)
        if p is None:
            print(f"    ! المكوّن «{name}» غير موجود — تخطي")
            continue
        lines.append((p.id, Decimal(qty), Decimal(cost)))

    if not lines:
        return None

    purchase = record_inventory_purchase(
        db,
        payment_method_id=cash.id,
        supplier="مخبز ومستلزمات نموذجية",
        note="فاتورة افتتاحية لتعبئة المخزون (مولّدة من سكربت seed)",
        lines=lines,
        user_id=None,
    )
    print(f"  + فاتورة شراء #{purchase.id} على محفظة «{cash.name_ar}» بإجمالي {purchase.amount} د.ل")
    return purchase


def main() -> None:
    db = get_session_factory()()
    try:
        print("[1] تنظيف الفواتير اليتيمة...")
        removed = cleanup_orphan_purchases(db)
        if removed:
            db.flush()
        print(f"    حُذفت {removed} فاتورة.")

        print("[2] إضافة/تحديث مكوّنات المخزون...")
        components = upsert_components(db)

        print("[3] إنشاء وصفات التركيب (BOM)...")
        bom_count = upsert_recipes(db, components)
        print(f"    عدد بنود الوصفات: {bom_count}")

        print("[4] تسجيل فاتورة شراء افتتاحية...")
        initial_inventory_purchase(db, components)

        db.commit()
        print("\nتم بنجاح. اذهب الآن إلى:")
        print("  - /inventory      لعرض رصيد المكوّنات")
        print("  - /pos            لبيع برجر/شيش ومراقبة خصم المكوّنات")
        print("  - /reports        للاطلاع على التقارير")
    except Exception as exc:
        db.rollback()
        print(f"!! خطأ: {exc}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
