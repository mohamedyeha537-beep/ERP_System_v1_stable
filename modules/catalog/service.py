from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.catalog.bom_explosion import BomExplosionError, can_have_bom, would_create_bom_cycle
from modules.catalog.models import BillOfMaterialsLine, Product, ProductCategory, ProductKind, ProductUnit


class CatalogError(Exception):
    pass


DEFAULT_UNITS: tuple[tuple[str, int], ...] = (
    ("قطعة", 10),
    ("جرام", 20),
    ("كيلو جرام", 30),
    ("لتر", 40),
    ("ملي لتر", 50),
)


def ensure_default_units(db: Session) -> None:
    existing = {u.name_ar for u in db.scalars(select(ProductUnit)).all()}
    created = False
    for name_ar, sort_order in DEFAULT_UNITS:
        if name_ar in existing:
            continue
        db.add(ProductUnit(name_ar=name_ar, sort_order=sort_order))
        created = True
    if created:
        db.commit()


def assert_barcode_available(db: Session, barcode: str | None, exclude_product_id: int | None = None) -> None:
    if not barcode or not barcode.strip():
        return
    b = barcode.strip()
    stmt = select(Product).where(Product.barcode == b)
    if exclude_product_id is not None:
        stmt = stmt.where(Product.id != exclude_product_id)
    if db.execute(stmt.limit(1)).scalar_one_or_none() is not None:
        raise CatalogError("الباركود مستخدم لصنف آخر.")


def validate_bom_line(db: Session, parent: Product, component_id: int, qty: Decimal) -> None:
    if not can_have_bom(parent):
        raise CatalogError("وصفة التركيب مسموحة للمنتج النهائي أو المخزني الوسيط (مثل الصوص).")
    if qty <= 0:
        raise CatalogError("الكمية يجب أن تكون أكبر من صفر.")
    if parent.id == component_id:
        raise CatalogError("لا يمكن أن يكون المنتج مكوّناً لنفسه.")
    comp = db.get(Product, component_id)
    if comp is None:
        raise CatalogError("المكوّن غير موجود.")
    if comp.kind not in (ProductKind.STOCK_ONLY, ProductKind.FINAL_SELLABLE):
        raise CatalogError("المكوّن يجب أن يكون صنفاً مخزنياً أو منتجاً نهائياً بسيطاً قابلاً للتوريد.")
    if would_create_bom_cycle(db, parent.id, component_id):
        raise CatalogError("هذا المكوّن يُنشئ دورة في وصفة التركيب.")


def load_pos_hidden_category_ids(db: Session) -> frozenset[int]:
    """معرّفات الفئات المخفية عن جلسة البيع (الفئة أو أي أب لها show_in_pos=False)."""
    rows = list(db.scalars(select(ProductCategory)).all())
    by_id = {c.id: c for c in rows}
    hidden: set[int] = set()
    for c in rows:
        cur: ProductCategory | None = c
        visible = True
        while cur is not None:
            if not cur.show_in_pos:
                visible = False
                break
            cur = by_id.get(cur.parent_id) if cur.parent_id else None
        if not visible:
            hidden.add(c.id)
    return frozenset(hidden)


def pos_visible_product_criteria(
    *, hidden_category_ids: frozenset[int] | None = None
):
    """شروط عرض الصنف في كatalog نقطة البيع."""
    from sqlalchemy import or_

    crit: list = [
        Product.kind == ProductKind.FINAL_SELLABLE,
        Product.is_active.is_(True),
        Product.show_in_pos.is_(True),
    ]
    if hidden_category_ids:
        crit.append(
            or_(
                Product.category_id.is_(None),
                Product.category_id.notin_(hidden_category_ids),
            )
        )
    return tuple(crit)


def assert_product_sellable(product: Product) -> None:
    if product.kind != ProductKind.FINAL_SELLABLE:
        raise CatalogError("لا يُباع هذا الصنف مباشرة من نقطة البيع.")
    if not product.show_in_pos:
        raise CatalogError("هذا الصنف غير معروض في جلسة البيع.")


def assert_product_pos_category_visible(
    db: Session, product: Product
) -> None:
    """يرفض البيع إن كانت فئة الصنف (أو أحد أسلافها) مخفية عن جلسة البيع."""
    if product.category_id is None:
        return
    if product.category_id in load_pos_hidden_category_ids(db):
        raise CatalogError("فئة هذا الصنف غير معروضة في جلسة البيع.")


def is_composite_product(db: Session, product_id: int) -> bool:
    """منتج مركّب = له بنود وصفة تركيب (BOM) وبالتالي يُصنَّع داخلياً.
    لا يُشترى ولا يدخل في المخزون كصنف مستقل (مكوّناته فقط هي ما يُتابَع)."""
    found = db.execute(
        select(BillOfMaterialsLine.id)
        .where(BillOfMaterialsLine.parent_product_id == product_id)
        .limit(1)
    ).scalar_one_or_none()
    return found is not None


def list_stockable_products(db: Session) -> list[Product]:
    """الأصناف القابلة للتوريد للمخزن.

    المكوّنات المخزنية تُشترى دائماً. المنتج النهائي يظهر في الشراء فقط
    إذا فُعّل له خيار الشراء المباشر من كرت الصنف.
    """
    stmt = (
        select(Product)
        .where(Product.is_active.is_(True))
        .where(
            (Product.kind == ProductKind.STOCK_ONLY)
            | (
                (Product.kind == ProductKind.FINAL_SELLABLE)
                & Product.direct_purchase_enabled.is_(True)
            )
        )
        .order_by(Product.name_ar)
    )
    return list(db.scalars(stmt).all())


def assert_unit_can_delete(db: Session, unit_name: str) -> None:
    in_use = db.execute(select(Product.id).where(Product.unit == unit_name).limit(1)).scalar_one_or_none()
    if in_use is not None:
        raise CatalogError("لا يمكن حذف الوحدة لأنها مستخدمة في أصناف حالية.")


@dataclass
class ProductUsage:
    sale_lines: int = 0
    purchase_lines: int = 0
    return_lines: int = 0
    stock_movements: int = 0
    bom_as_component: int = 0

    @property
    def has_activity(self) -> bool:
        return (
            self.sale_lines
            + self.purchase_lines
            + self.return_lines
            + self.stock_movements
            + self.bom_as_component
        ) > 0


def product_usage(db: Session, product_id: int) -> ProductUsage:
    from modules.inventory.models import StockMovement
    from modules.payments.models import PurchaseLine
    from modules.refunds.models import SaleReturnLine
    from modules.sales.models import SaleLine

    return ProductUsage(
        sale_lines=int(
            db.scalar(select(func.count()).select_from(SaleLine).where(SaleLine.product_id == product_id))
            or 0
        ),
        purchase_lines=int(
            db.scalar(
                select(func.count()).select_from(PurchaseLine).where(PurchaseLine.product_id == product_id)
            )
            or 0
        ),
        return_lines=int(
            db.scalar(
                select(func.count())
                .select_from(SaleReturnLine)
                .where(SaleReturnLine.product_id == product_id)
            )
            or 0
        ),
        stock_movements=int(
            db.scalar(
                select(func.count())
                .select_from(StockMovement)
                .where(StockMovement.product_id == product_id)
            )
            or 0
        ),
        bom_as_component=int(
            db.scalar(
                select(func.count())
                .select_from(BillOfMaterialsLine)
                .where(BillOfMaterialsLine.component_product_id == product_id)
            )
            or 0
        ),
    )


def product_delete_block_reason(db: Session, product_id: int) -> str | None:
    u = product_usage(db, product_id)
    if u.sale_lines:
        return "مُستخدم في فواتير بيع."
    if u.purchase_lines:
        return "مُستخدم في فواتير شراء."
    if u.return_lines:
        return "مُستخدم في مرتجعات."
    if u.stock_movements:
        return "له حركات مخزون."
    if u.bom_as_component:
        return "مكوّن في وصفات تركيب أخرى."
    return None


def bulk_product_deletable(db: Session, product_ids: list[int]) -> dict[int, bool]:
    """تحديد قابلية الحذف لمجموعة أصناف — 5 استعلامات بدلاً من 5×N."""
    if not product_ids:
        return {}
    ids = list({int(x) for x in product_ids})
    blocked: set[int] = set()
    from modules.inventory.models import StockMovement
    from modules.payments.models import PurchaseLine
    from modules.refunds.models import SaleReturnLine
    from modules.sales.models import SaleLine

    usage_sources = (
        select(SaleLine.product_id).where(SaleLine.product_id.in_(ids)),
        select(PurchaseLine.product_id).where(PurchaseLine.product_id.in_(ids)),
        select(SaleReturnLine.product_id).where(SaleReturnLine.product_id.in_(ids)),
        select(StockMovement.product_id).where(StockMovement.product_id.in_(ids)),
        select(BillOfMaterialsLine.component_product_id).where(
            BillOfMaterialsLine.component_product_id.in_(ids)
        ),
    )
    for src in usage_sources:
        blocked.update(int(x) for x in db.scalars(src.distinct()).all())
    return {pid: pid not in blocked for pid in ids}


def delete_product(db: Session, product: Product) -> None:
    reason = product_delete_block_reason(db, product.id)
    if reason:
        raise CatalogError(f"لا يمكن حذف «{product.name_ar}»: {reason}")
    db.delete(product)


@dataclass
class BulkProductsResult:
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def bulk_apply_products(db: Session, product_ids: list[int], action: str) -> BulkProductsResult:
    result = BulkProductsResult()
    if not product_ids:
        result.errors.append("لم يُحدَّد أي صنف.")
        return result
    allowed = {
        "hide_pos",
        "show_pos",
        "hide_shop",
        "show_shop",
        "stock_only",
        "deactivate",
        "delete",
    }
    if action not in allowed:
        result.errors.append("إجراء غير معروف.")
        return result

    products = list(
        db.scalars(select(Product).where(Product.id.in_(product_ids)).order_by(Product.id)).all()
    )
    if not products:
        result.errors.append("لم يُعثر على أصناف مطابقة.")
        return result

    for p in products:
        try:
            if action == "hide_pos":
                if p.kind != ProductKind.FINAL_SELLABLE:
                    result.skipped += 1
                    continue
                p.show_in_pos = False
                result.updated += 1
            elif action == "show_pos":
                if p.kind != ProductKind.FINAL_SELLABLE:
                    result.skipped += 1
                    continue
                p.show_in_pos = True
                result.updated += 1
            elif action == "hide_shop":
                if p.kind != ProductKind.FINAL_SELLABLE:
                    result.skipped += 1
                    continue
                p.show_in_shop = False
                result.updated += 1
            elif action == "show_shop":
                if p.kind != ProductKind.FINAL_SELLABLE:
                    result.skipped += 1
                    continue
                p.show_in_shop = True
                result.updated += 1
            elif action == "stock_only":
                p.kind = ProductKind.STOCK_ONLY
                p.show_in_pos = False
                p.show_in_shop = False
                p.direct_purchase_enabled = False
                p.sell_price = None
                result.updated += 1
            elif action == "deactivate":
                p.is_active = False
                p.show_in_pos = False
                p.show_in_shop = False
                p.direct_purchase_enabled = False
                result.updated += 1
            elif action == "delete":
                delete_product(db, p)
                result.deleted += 1
        except CatalogError as e:
            result.skipped += 1
            result.errors.append(str(e))
    db.flush()
    return result


def bulk_fill_reference_costs_from_purchase(
    db: Session, *, overwrite: bool = False
) -> BulkProductsResult:
    """نسخ متوسط سعر الشراء (من فواتير المخزون) إلى التكلفة المرجعية في كرت الصنف."""
    from modules.reporting.queries import avg_unit_cost_per_product

    avg_costs = avg_unit_cost_per_product(db)
    result = BulkProductsResult()
    products = list(db.scalars(select(Product).order_by(Product.id)).all())
    for p in products:
        pid = int(p.id)
        if (
            not overwrite
            and p.reference_unit_cost is not None
            and p.reference_unit_cost > 0
        ):
            result.skipped += 1
            continue
        avg = avg_costs.get(pid)
        if avg is None or avg <= 0:
            result.skipped += 1
            continue
        p.reference_unit_cost = avg
        result.updated += 1
    db.flush()
    return result


def bulk_fill_reference_costs_from_purchase(
    db: Session, *, overwrite: bool = False
) -> BulkProductsResult:
    """نسخ متوسط سعر الشراء (من فواتير المخزون) إلى التكلفة المرجعية في كرت الصنف."""
    from modules.reporting.queries import avg_unit_cost_per_product

    avg_costs = avg_unit_cost_per_product(db)
    result = BulkProductsResult()
    products = list(db.scalars(select(Product).order_by(Product.id)).all())
    for p in products:
        pid = int(p.id)
        if (
            not overwrite
            and p.reference_unit_cost is not None
            and p.reference_unit_cost > 0
        ):
            result.skipped += 1
            continue
        avg = avg_costs.get(pid)
        if avg is None or avg <= 0:
            result.skipped += 1
            continue
        p.reference_unit_cost = avg
        result.updated += 1
    db.flush()
    return result
