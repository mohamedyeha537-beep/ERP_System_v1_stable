"""توسيع وصفة التركيب (BOM) — منتج وسيط مثل «صوص» يُستخدم في أكثر من منتج نهائي."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.catalog.models import BillOfMaterialsLine, Product, ProductKind


class BomExplosionError(Exception):
    pass


def product_has_recipe(db: Session, product_id: int) -> bool:
    row = db.execute(
        select(BillOfMaterialsLine.id)
        .where(BillOfMaterialsLine.parent_product_id == product_id)
        .limit(1)
    ).scalar_one_or_none()
    return row is not None


def can_have_bom(parent: Product) -> bool:
    """منتج نهائي أو مخزني وسيط (صوص) — وليس مكوّناً خاماً فقط."""
    return parent.kind in (ProductKind.FINAL_SELLABLE, ProductKind.STOCK_ONLY)


def would_create_bom_cycle(db: Session, parent_id: int, component_id: int) -> bool:
    """يمنع A → B → A في سلسلة التركيب."""
    if parent_id == component_id:
        return True
    seen: set[int] = {parent_id}
    stack = [component_id]
    while stack:
        pid = stack.pop()
        if pid in seen:
            return True
        seen.add(pid)
        child_ids = db.scalars(
            select(BillOfMaterialsLine.component_product_id).where(
                BillOfMaterialsLine.parent_product_id == pid
            )
        ).all()
        stack.extend(int(c) for c in child_ids)
    return False


def expand_bom_requirements(
    db: Session,
    product_id: int,
    quantity: Decimal,
    *,
    include_packaging: bool = False,
    _stack: frozenset[int] | None = None,
) -> dict[int, Decimal]:
    """يُرجع احتياج المخزون لكمية من منتج.

    المنتجات النهائية البسيطة المفعّلة للشراء المباشر تُخصم من رصيدها
    كمنتج جاهز، أما المنتجات المركّبة فتُخصم مكوّناتها.
    """
    if quantity <= 0:
        return {}
    stack = _stack or frozenset()
    if product_id in stack:
        raise BomExplosionError("دورة في وصفة التركيب — راجع المنتجات الوسيطة.")
    stack = stack | {product_id}

    product = db.get(Product, product_id)
    lines = list(
        db.scalars(
            select(BillOfMaterialsLine).where(
                BillOfMaterialsLine.parent_product_id == product_id
            )
        ).all()
    )
    if not lines:
        if (
            product is not None
            and product.kind == ProductKind.FINAL_SELLABLE
            and product.direct_purchase_enabled
        ):
            return {product_id: quantity.quantize(Decimal("0.0001"))}
        if product is not None and product.kind == ProductKind.STOCK_ONLY:
            return {product_id: quantity.quantize(Decimal("0.0001"))}
        return {}

    needs: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for line in lines:
        if line.packaging_only and not include_packaging:
            continue
        sub_qty = (Decimal(str(line.qty_per_parent or 0)) * quantity).quantize(
            Decimal("0.0001")
        )
        if sub_qty <= 0:
            continue
        comp_id = int(line.component_product_id)
        if product_has_recipe(db, comp_id):
            for pid, q in expand_bom_requirements(
                db, comp_id, sub_qty, include_packaging=include_packaging, _stack=stack
            ).items():
                needs[pid] += q
        else:
            needs[comp_id] += sub_qty
    return dict(needs)


def merge_line_requirements(
    db: Session,
    items: list[tuple[int | None, Decimal]],
    *,
    include_packaging: bool = False,
) -> dict[int, Decimal]:
    """دمج احتياجات عدة بنود بيع."""
    needs: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for product_id, qty in items:
        if product_id is None or qty <= 0:
            continue
        for pid, q in expand_bom_requirements(
            db, int(product_id), qty, include_packaging=include_packaging
        ).items():
            needs[pid] += q
    return dict(needs)
