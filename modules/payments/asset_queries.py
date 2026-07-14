"""استعلامات فصل الأصول الثابتة عن الاستهلاكات."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from modules.payments.models import Purchase, PurchaseKind


def purchase_has_fixed_asset_line(purchase: Purchase) -> bool:
    return any(ln.is_fixed_asset for ln in (purchase.lines or []))


def purchase_is_consumable_only(purchase: Purchase) -> bool:
    lines = purchase.lines or []
    if not lines:
        return False
    return all(not ln.is_fixed_asset and ln.product_id is None for ln in lines)


def list_asset_purchases_in_period(
    db: Session,
    start: datetime,
    end: datetime,
    *,
    fixed_only: bool = False,
    consumable_only: bool = False,
    consumable_category: str | None = None,
    filter_domain=None,
    limit: int = 500,
) -> list[Purchase]:
    """يشمل فواتير ASSET التقليدية + بنود أصول/استهلاك داخل فواتير INVENTORY المختلطة."""
    stmt = (
        select(Purchase)
        .where(
            Purchase.created_at >= start,
            Purchase.created_at < end,
            Purchase.kind.in_([PurchaseKind.ASSET, PurchaseKind.INVENTORY]),
        )
        .options(selectinload(Purchase.lines), selectinload(Purchase.method))
        .order_by(Purchase.id.desc())
        .limit(limit * 3)
    )
    if filter_domain is not None:
        from modules.platform.business_domain import purchase_domain_db_values

        domain_vals = purchase_domain_db_values(filter_domain)
        if domain_vals:
            stmt = stmt.where(Purchase.business_domain.in_(domain_vals))
    if consumable_category:
        stmt = stmt.where(Purchase.expense_category == consumable_category)
    items = list(db.scalars(stmt).all())
    # فواتير المخزون الخالصة تُستبعد من قوائم الأصول/الاستهلاك
    items = [
        p
        for p in items
        if p.kind == PurchaseKind.ASSET
        or any(
            (ln.product_id is None and (ln.item_name or "").strip())
            for ln in (p.lines or [])
        )
    ]
    if fixed_only:
        items = [p for p in items if purchase_has_fixed_asset_line(p)]
    elif consumable_only:
        items = [
            p
            for p in items
            if purchase_is_consumable_only(p)
            or (
                p.kind == PurchaseKind.INVENTORY
                and any(ln.is_consumable_line for ln in (p.lines or []))
                and not purchase_has_fixed_asset_line(p)
            )
        ]
    return items[:limit]


def summarize_consumables_by_category(
    purchases: list[Purchase],
) -> list[tuple[str, Decimal, int]]:
    buckets: dict[str, list[Purchase]] = defaultdict(list)
    for p in purchases:
        cat = (p.expense_category or "غير مصنّف").strip() or "غير مصنّف"
        buckets[cat].append(p)
    rows: list[tuple[str, Decimal, int]] = []
    for cat, ps in buckets.items():
        total = sum((Decimal(str(p.amount or 0)) for p in ps), Decimal("0"))
        rows.append((cat, total.quantize(Decimal("0.001")), len(ps)))
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows
