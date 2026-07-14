"""قائمة المخزون: بحث، ترتيب، وترقيم صفحات."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.catalog.models import Product
from modules.catalog.service import list_stockable_products
from modules.inventory.models import StockMovement, StockMovementType

SORT_NAME = "name"
SORT_MOST_REQUESTED = "most_requested"
SORT_MOST_USED = "most_used"
VALID_SORTS = {SORT_NAME, SORT_MOST_REQUESTED, SORT_MOST_USED}
PAGE_SIZE_OPTIONS = (15, 25, 50, 100)
DEFAULT_PAGE_SIZE = 25
FILTER_ALL = "all"
FILTER_LOW = "low"
FILTER_IN_STOCK = "in_stock"
FILTER_ZERO = "zero"
FILTER_NEGATIVE = "negative"
VALID_STOCK_FILTERS = {
    FILTER_ALL,
    FILTER_LOW,
    FILTER_IN_STOCK,
    FILTER_ZERO,
    FILTER_NEGATIVE,
}


@dataclass
class InventoryPageRow:
    product: Product
    balance: Decimal
    unit_cost: Decimal | None
    line_value: Decimal
    sale_out: Decimal
    total_out: Decimal


@dataclass
class InventoryPageResult:
    rows: list[InventoryPageRow]
    total_count: int
    page: int
    page_size: int
    total_pages: int
    inventory_total: Decimal
    sort: str
    search_q: str
    stock_filter: str


def _normalize_page_size(raw: int | None) -> int:
    if raw in PAGE_SIZE_OPTIONS:
        return raw
    return DEFAULT_PAGE_SIZE


def _matches_search(product: Product, q: str) -> bool:
    term = (q or "").strip().lower()
    if not term:
        return True
    name = (product.name_ar or "").lower()
    if term in name:
        return True
    barcode = (product.barcode or "").strip().lower()
    if barcode and (term in barcode or barcode == term):
        return True
    sku = (product.sku or "").strip().lower()
    if sku and term in sku:
        return True
    return False


def _matches_stock_filter(
    balance: Decimal, reorder_level: Decimal | None, stock_filter: str
) -> bool:
    key = stock_filter if stock_filter in VALID_STOCK_FILTERS else FILTER_ALL
    qty = Decimal(str(balance or 0))
    reorder = Decimal(str(reorder_level or 0))
    is_low = qty < 0 or (reorder > 0 and qty <= reorder)
    if key == FILTER_LOW:
        return is_low
    if key == FILTER_IN_STOCK:
        return qty > 0 and not is_low
    if key == FILTER_ZERO:
        return qty == 0
    if key == FILTER_NEGATIVE:
        return qty < 0
    return True


def _outbound_stats(
    db: Session, warehouse_id: int, product_ids: list[int]
) -> tuple[dict[int, Decimal], dict[int, Decimal]]:
    if not product_ids:
        return {}, {}
    base = (
        StockMovement.warehouse_id == warehouse_id,
        StockMovement.product_id.in_(product_ids),
        StockMovement.quantity < 0,
    )
    sale_out: dict[int, Decimal] = {}
    for pid, total in db.execute(
        select(
            StockMovement.product_id,
            func.coalesce(func.sum(func.abs(StockMovement.quantity)), 0),
        )
        .where(*base, StockMovement.movement_type == StockMovementType.SALE)
        .group_by(StockMovement.product_id)
    ).all():
        sale_out[int(pid)] = Decimal(str(total or 0))
    total_out: dict[int, Decimal] = {}
    for pid, total in db.execute(
        select(
            StockMovement.product_id,
            func.coalesce(func.sum(func.abs(StockMovement.quantity)), 0),
        )
        .where(*base)
        .group_by(StockMovement.product_id)
    ).all():
        total_out[int(pid)] = Decimal(str(total or 0))
    return sale_out, total_out


def build_inventory_page(
    db: Session,
    warehouse_id: int,
    *,
    search_q: str = "",
    sort: str = SORT_NAME,
    page: int = 1,
    page_size: int | None = None,
    stock_filter: str = FILTER_ALL,
    avg_costs: dict[int, Decimal] | None = None,
    get_balance_fn=None,
) -> InventoryPageResult:
    """يبني صفحة أرصدة المخزون مع بحث وترتيب وترقيم."""
    from modules.reporting import queries as report_queries

    if get_balance_fn is None:
        from modules.inventory.service import get_balance as get_balance_fn

    sort_key = sort if sort in VALID_SORTS else SORT_NAME
    filter_key = stock_filter if stock_filter in VALID_STOCK_FILTERS else FILTER_ALL
    ps = _normalize_page_size(page_size)
    page_num = max(1, int(page or 1))

    if avg_costs is None:
        avg_costs = report_queries.avg_unit_cost_per_product(db)

    all_products = list_stockable_products(db)
    product_ids = [p.id for p in all_products]
    sale_out_map, total_out_map = _outbound_stats(db, warehouse_id, product_ids)

    inventory_total = Decimal("0")
    candidates: list[InventoryPageRow] = []
    for p in all_products:
        bal = get_balance_fn(db, p.id, warehouse_id)
        unit_cost = avg_costs.get(p.id)
        line_val = (
            (bal * unit_cost).quantize(Decimal("0.001"))
            if unit_cost is not None
            else Decimal("0")
        )
        inventory_total += line_val
        if not _matches_search(p, search_q):
            continue
        if not _matches_stock_filter(bal, p.reorder_level, filter_key):
            continue
        candidates.append(
            InventoryPageRow(
                product=p,
                balance=bal,
                unit_cost=unit_cost,
                line_value=line_val,
                sale_out=sale_out_map.get(p.id, Decimal("0")),
                total_out=total_out_map.get(p.id, Decimal("0")),
            )
        )

    inventory_total = inventory_total.quantize(Decimal("0.001"))

    if sort_key == SORT_MOST_REQUESTED:
        candidates.sort(
            key=lambda r: (-r.sale_out, r.product.name_ar),
        )
    elif sort_key == SORT_MOST_USED:
        candidates.sort(
            key=lambda r: (-r.total_out, r.product.name_ar),
        )
    else:
        candidates.sort(key=lambda r: r.product.name_ar)

    total_count = len(candidates)
    total_pages = max(1, (total_count + ps - 1) // ps)
    page_num = min(page_num, total_pages)
    start = (page_num - 1) * ps
    rows = candidates[start : start + ps]

    return InventoryPageResult(
        rows=rows,
        total_count=total_count,
        page=page_num,
        page_size=ps,
        total_pages=total_pages,
        inventory_total=inventory_total,
        sort=sort_key,
        search_q=(search_q or "").strip(),
        stock_filter=filter_key,
    )
