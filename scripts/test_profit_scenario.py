"""سيناريو إثبات صحة حساب الربح من النهاية للنهاية.

يقوم السكربت بـ:
1) عرض حالة المخزون والشراء قبل البيع.
2) عرض متوسط سعر شراء كل مكوّن.
3) حساب التكلفة المتوقّعة لوجبة برجر ووجبة شيش يدوياً (انطلاقاً من الوصفة).
4) تسجيل فاتورة بيع تجريبية (برجر + شيش).
5) عرض ما خُصم فعلياً من المخزون.
6) عرض حساب الربح: إيراد − COGS = إجمالي ربح، ثم − مصروفات − أصول = صافي.
7) مقارنة ما حسبه النظام بما حسبناه يدوياً.
"""
from __future__ import annotations

from decimal import Decimal

import modules.authz.models  # noqa: F401
import modules.catalog.models  # noqa: F401
import modules.inventory.models  # noqa: F401
import modules.payments.models  # noqa: F401
import modules.sales.models  # noqa: F401
from sqlalchemy import select

from app.dashboard_stats import collect
from infra.db import get_session_factory
from modules.catalog.models import BillOfMaterialsLine, Product, ProductKind
from modules.inventory.models import StockBalance, StockMovement, StockMovementType
from modules.payments.models import PaymentMethod
from modules.payments.service import record_sale_payment
from modules.reporting.queries import (
    avg_unit_cost_per_product,
    cogs_summary,
    profit_by_product,
)
from modules.sales.models import Sale, SaleLine, SaleStatus
from modules.sales.service import complete_sale


def hr(title: str = "") -> None:
    print()
    if title:
        print(f"━━━━ {title} " + "━" * (60 - len(title)))
    else:
        print("━" * 64)


def fmt(n) -> str:
    return f"{Decimal(str(n or 0)):>12,.3f}"


def show_balances(db) -> None:
    print(f"  {'الصنف':<15}{'الرصيد':>14}{'الوحدة':<12}")
    for p in db.scalars(
        select(Product).where(Product.kind == ProductKind.STOCK_ONLY).order_by(Product.name_ar)
    ).all():
        bal = db.get(StockBalance, p.id)
        q = bal.quantity if bal else Decimal("0")
        print(f"  {p.name_ar:<15}{q!s:>14}  {p.unit}")


def show_avg_costs(db) -> None:
    avg = avg_unit_cost_per_product(db)
    print(f"  {'الصنف':<15}{'الوحدة':<10}{'متوسط الشراء':>16}")
    for p in db.scalars(
        select(Product).where(Product.kind == ProductKind.STOCK_ONLY).order_by(Product.name_ar)
    ).all():
        c = avg.get(p.id, Decimal("0"))
        print(f"  {p.name_ar:<15}{p.unit:<10}{fmt(c)} د.ل / {p.unit}")


def expected_cost_for_parent(db, parent: Product, avg: dict[int, Decimal]) -> Decimal:
    boms = list(
        db.scalars(
            select(BillOfMaterialsLine).where(
                BillOfMaterialsLine.parent_product_id == parent.id
            )
        ).all()
    )
    print(f"  وصفة «{parent.name_ar}»:")
    total = Decimal("0")
    for b in boms:
        comp = db.get(Product, b.component_product_id)
        c = avg.get(comp.id, Decimal("0"))
        line = (b.qty_per_parent * c).quantize(Decimal("0.001"))
        total += line
        print(
            f"    - {comp.name_ar:<15}{str(b.qty_per_parent):>10} {comp.unit:<8}× "
            f"{fmt(c)} د.ل = {fmt(line)} د.ل"
        )
    print(f"    {'إجمالي تكلفة الوحدة':<35}={fmt(total)} د.ل")
    return total.quantize(Decimal("0.001"))


def main() -> None:
    db = get_session_factory()()
    try:
        hr("١) رصيد المخزون قبل البيع")
        show_balances(db)

        hr("٢) متوسط سعر شراء المكوّنات")
        show_avg_costs(db)

        hr("٣) التكلفة المتوقّعة لوجبة (حساب يدوي من الوصفة)")
        avg = avg_unit_cost_per_product(db)
        burger = db.execute(
            select(Product).where(Product.name_ar == "برجر")
        ).scalar_one()
        shish = db.execute(
            select(Product).where(Product.name_ar == "شيش")
        ).scalar_one()
        cost_burger = expected_cost_for_parent(db, burger, avg)
        print()
        cost_shish = expected_cost_for_parent(db, shish, avg)

        # سيناريو: 3× برجر + 2× شيش
        n_burger = Decimal("3")
        n_shish = Decimal("2")
        rev_burger = n_burger * burger.sell_price
        rev_shish = n_shish * shish.sell_price
        revenue = rev_burger + rev_shish
        expected_cogs = (cost_burger * n_burger + cost_shish * n_shish).quantize(
            Decimal("0.001")
        )
        expected_gross = revenue - expected_cogs

        hr("٤) سيناريو البيع التجريبي")
        print(f"  ٣ × برجر  : {fmt(burger.sell_price)} × 3 = {fmt(rev_burger)} د.ل")
        print(f"  ٢ × شيش   : {fmt(shish.sell_price)} × 2 = {fmt(rev_shish)} د.ل")
        print(f"  الإيرادات                       = {fmt(revenue)} د.ل")
        print(f"  COGS متوقّع                      = {fmt(expected_cogs)} د.ل")
        print(f"  إجمالي الربح متوقّع              = {fmt(expected_gross)} د.ل")
        margin = (expected_gross / revenue * 100) if revenue else Decimal("0")
        print(f"  هامش الربح المتوقّع              = {margin.quantize(Decimal('0.1'))}%")

        hr("٥) تنفيذ البيع فعلياً")
        cash = db.execute(
            select(PaymentMethod).where(PaymentMethod.is_active.is_(True))
        ).scalars().first()
        sale = Sale(status=SaleStatus.DRAFT, total=Decimal("0"))
        db.add(sale)
        db.flush()
        db.add(
            SaleLine(
                sale_id=sale.id,
                product_id=burger.id,
                quantity=n_burger,
                unit_price=burger.sell_price,
                line_total=rev_burger,
            )
        )
        db.add(
            SaleLine(
                sale_id=sale.id,
                product_id=shish.id,
                quantity=n_shish,
                unit_price=shish.sell_price,
                line_total=rev_shish,
            )
        )
        sale.total = revenue
        db.flush()
        complete_sale(db, sale_id=sale.id, user_id=None)
        record_sale_payment(
            db, sale_id=sale.id, payment_method_id=cash.id, amount=revenue
        )
        db.commit()
        print(f"  ✓ سُجّلت فاتورة بيع #{sale.id} بإجمالي {fmt(revenue)} د.ل على «{cash.name_ar}»")

        hr("٦) المخزون بعد البيع — ما خُصم فعلياً")
        show_balances(db)

        hr("٧) حركات SALE لهذه الفاتورة")
        movs = db.scalars(
            select(StockMovement)
            .where(
                StockMovement.sale_id == sale.id,
                StockMovement.movement_type == StockMovementType.SALE,
            )
            .order_by(StockMovement.id)
        ).all()
        sub = Decimal("0")
        print(f"  {'المكوّن':<15}{'الكمية':>10}{'متوسط الشراء':>18}{'التكلفة':>14}")
        for m in movs:
            p = db.get(Product, m.product_id)
            c = avg.get(p.id, Decimal("0"))
            line_cost = (abs(m.quantity) * c).quantize(Decimal("0.001"))
            sub += line_cost
            print(
                f"  {p.name_ar:<15}{str(m.quantity):>10}{fmt(c)} د.ل/{p.unit:<6}{fmt(line_cost)} د.ل"
            )
        print(f"  {'مجموع التكلفة الفعلية':<43}{fmt(sub)} د.ل")

        hr("٨) ما يُحسب على لوحة التحكم")
        from modules.reporting.queries import period_bounds

        s, e = period_bounds("month")
        cogs_sys = cogs_summary(db, s, e)
        stats = collect(db)
        print(f"  إيرادات الشهر (من النظام)         : {fmt(stats.month.revenue)} د.ل")
        print(f"  COGS الشهر (cogs_summary)        : {fmt(cogs_sys)} د.ل")
        print(f"  COGS الشهر (DashboardStats)      : {fmt(stats.month_cogs)} د.ل")
        print(f"  إجمالي ربح الشهر (DashboardStats): {fmt(stats.month_gross_profit)} د.ل")
        print(f"  مصروفات الشهر                    : {fmt(stats.month_expenses)} د.ل")
        print(f"  أصول/أدوات الشهر                 : {fmt(stats.month_assets)} د.ل")
        print(f"  صافي ربح الشهر                   : {fmt(stats.month_net_profit)} د.ل")
        print(f"  هامش إجمالي                       : {stats.month_gross_margin_pct}%")
        print(f"  هامش صافي                         : {stats.month_net_margin_pct}%")

        hr("٩) ربح كل منتج (profit_by_product)")
        rows = profit_by_product(db, s, e)
        print(f"  {'المنتج':<15}{'الكمية':>8}{'الإيراد':>12}{'تكلفة الوحدة':>16}{'COGS':>14}{'الربح':>14}")
        for r in rows:
            print(
                f"  {r.name_ar:<15}{str(r.qty_sold):>8}{fmt(r.revenue):>}"
                f"{fmt(r.avg_cost):>}{fmt(r.cogs):>}{fmt(r.gross_profit):>}"
            )

        hr("١٠) خلاصة التحقّق")
        ok_cogs = cogs_sys >= expected_cogs - Decimal("0.01") and cogs_sys <= expected_cogs + Decimal("0.01")
        print(f"  COGS اليدوي:  {fmt(expected_cogs)} د.ل")
        print(f"  COGS النظام:  {fmt(cogs_sys)} د.ل")
        print(f"  مطابقة:        {'✓ نعم' if ok_cogs else '✗ لا'}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
