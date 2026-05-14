from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.catalog.models import BillOfMaterialsLine, Product, ProductKind, ProductUnit


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
    if parent.kind != ProductKind.FINAL_SELLABLE:
        raise CatalogError("وصفة التركيب مسموحة فقط للمنتج النهائي.")
    if qty <= 0:
        raise CatalogError("الكمية يجب أن تكون أكبر من صفر.")
    if parent.id == component_id:
        raise CatalogError("لا يمكن أن يكون المنتج مكوّناً لنفسه.")
    comp = db.get(Product, component_id)
    if comp is None:
        raise CatalogError("المكوّن غير موجود.")
    if comp.kind != ProductKind.STOCK_ONLY:
        raise CatalogError("المكوّن يجب أن يكون من نوع «مخزني فقط» في هذه النسخة.")


def assert_product_sellable(product: Product) -> None:
    if product.kind != ProductKind.FINAL_SELLABLE:
        raise CatalogError("لا يُباع هذا الصنف مباشرة من نقطة البيع.")


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
    """الأصناف القابلة للحفظ في المخزون: المكوّنات الخام التي تُشترى من المورّد
    (STOCK_ONLY فقط).
    المنتجات النهائية (FINAL_SELLABLE) لا تظهر في المخزون لأنها إمّا تُصنَّع داخلياً
    من مكوّناتها (تُخصم المكوّنات عند البيع) أو لم تُعرَّف لها وصفة تركيب بعد."""
    stmt = (
        select(Product)
        .where(Product.is_active.is_(True))
        .where(Product.kind == ProductKind.STOCK_ONLY)
        .order_by(Product.name_ar)
    )
    return list(db.scalars(stmt).all())


def assert_unit_can_delete(db: Session, unit_name: str) -> None:
    in_use = db.execute(select(Product.id).where(Product.unit == unit_name).limit(1)).scalar_one_or_none()
    if in_use is not None:
        raise CatalogError("لا يمكن حذف الوحدة لأنها مستخدمة في أصناف حالية.")
