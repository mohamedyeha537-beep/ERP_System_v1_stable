"""تصدير أرصدة المخزون إلى CSV و Excel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.number_format import format_qty_plain
from modules.catalog.models import Product
from modules.catalog.service import list_stockable_products
from modules.inventory.inventory_page import (
    FILTER_ALL,
    SORT_MOST_REQUESTED,
    SORT_MOST_USED,
    SORT_NAME,
    VALID_SORTS,
    _matches_search,
    _matches_stock_filter,
)
from modules.inventory.models import StockMovement, StockMovementType


@dataclass
class InventoryExportRow:
    product: Product
    balance: Decimal
    unit_cost: Decimal | None
    line_value: Decimal
    sale_in_period: Decimal
    out_in_period: Decimal
    in_in_period: Decimal
    is_low: bool


def _parse_product_ids(raw: str | None) -> set[int] | None:
    if not raw or not raw.strip():
        return None
    out: set[int] = set()
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out or None


def _movement_stats_in_period(
    db: Session,
    warehouse_id: int,
    product_ids: list[int],
    start: datetime | None,
    end: datetime | None,
) -> tuple[dict[int, Decimal], dict[int, Decimal], dict[int, Decimal]]:
    """(بيع، صرف، إدخال) لكل صنف ضمن الفترة — إن وُجدت."""
    if not product_ids or start is None or end is None:
        return {}, {}, {}
    sale_out: dict[int, Decimal] = {}
    total_out: dict[int, Decimal] = {}
    total_in: dict[int, Decimal] = {}
    time_filter = (
        StockMovement.created_at >= start,
        StockMovement.created_at < end,
    )
    for pid, total in db.execute(
        select(
            StockMovement.product_id,
            func.coalesce(func.sum(func.abs(StockMovement.quantity)), 0),
        )
        .where(
            StockMovement.warehouse_id == warehouse_id,
            StockMovement.product_id.in_(product_ids),
            StockMovement.quantity < 0,
            StockMovement.movement_type == StockMovementType.SALE,
            *time_filter,
        )
        .group_by(StockMovement.product_id)
    ).all():
        sale_out[int(pid)] = Decimal(str(total or 0))
    for pid, total in db.execute(
        select(
            StockMovement.product_id,
            func.coalesce(func.sum(func.abs(StockMovement.quantity)), 0),
        )
        .where(
            StockMovement.warehouse_id == warehouse_id,
            StockMovement.product_id.in_(product_ids),
            StockMovement.quantity < 0,
            *time_filter,
        )
        .group_by(StockMovement.product_id)
    ).all():
        total_out[int(pid)] = Decimal(str(total or 0))
    for pid, total in db.execute(
        select(
            StockMovement.product_id,
            func.coalesce(func.sum(StockMovement.quantity), 0),
        )
        .where(
            StockMovement.warehouse_id == warehouse_id,
            StockMovement.product_id.in_(product_ids),
            StockMovement.quantity > 0,
            *time_filter,
        )
        .group_by(StockMovement.product_id)
    ).all():
        total_in[int(pid)] = Decimal(str(total or 0))
    return sale_out, total_out, total_in


def build_inventory_export_rows(
    db: Session,
    warehouse_id: int,
    *,
    search_q: str = "",
    sort: str = SORT_NAME,
    stock_filter: str = FILTER_ALL,
    product_ids: set[int] | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    avg_costs: dict[int, Decimal] | None = None,
    get_balance_fn=None,
) -> list[InventoryExportRow]:
    from modules.inventory.service import get_balance as default_get_balance
    from modules.inventory.service import is_low_stock
    from modules.reporting import queries as report_queries

    if get_balance_fn is None:
        get_balance_fn = default_get_balance
    if avg_costs is None:
        avg_costs = report_queries.avg_unit_cost_per_product(db)

    sort_key = sort if sort in VALID_SORTS else SORT_NAME
    all_products = list_stockable_products(db)
    if product_ids:
        all_products = [p for p in all_products if p.id in product_ids]

    pids = [p.id for p in all_products]
    sale_map, out_map, in_map = _movement_stats_in_period(
        db, warehouse_id, pids, period_start, period_end
    )

    rows: list[InventoryExportRow] = []
    for p in all_products:
        if not _matches_search(p, search_q):
            continue
        bal = get_balance_fn(db, p.id, warehouse_id)
        if not _matches_stock_filter(bal, p.reorder_level, stock_filter):
            continue
        from modules.inventory.lots import fifo_inventory_value

        lot_qty, lot_value = fifo_inventory_value(db, p.id, warehouse_id=warehouse_id)
        if lot_qty > 0 and lot_value > 0:
            unit_cost = (lot_value / lot_qty).quantize(Decimal("0.001"))
            line_val = lot_value.quantize(Decimal("0.001"))
        else:
            unit_cost = avg_costs.get(p.id)
            line_val = (
                (bal * unit_cost).quantize(Decimal("0.001"))
                if unit_cost is not None
                else Decimal("0")
            )
        rows.append(
            InventoryExportRow(
                product=p,
                balance=bal,
                unit_cost=unit_cost,
                line_value=line_val,
                sale_in_period=sale_map.get(p.id, Decimal("0")),
                out_in_period=out_map.get(p.id, Decimal("0")),
                in_in_period=in_map.get(p.id, Decimal("0")),
                is_low=is_low_stock(bal, p.reorder_level),
            )
        )

    if sort_key == SORT_MOST_REQUESTED:
        rows.sort(key=lambda r: (-r.sale_in_period, r.product.name_ar))
    elif sort_key == SORT_MOST_USED:
        rows.sort(key=lambda r: (-r.out_in_period, r.product.name_ar))
    else:
        rows.sort(key=lambda r: r.product.name_ar)
    return rows


def _parse_period(start_str: str | None, end_str: str | None):
    from modules.reporting import queries as report_queries

    period_start: datetime | None = None
    period_end: datetime | None = None
    has_period = False
    if (start_str or "").strip() and (end_str or "").strip():
        parsed = report_queries.parse_custom_range(start_str, end_str)
        if parsed:
            period_start, period_end = parsed
            has_period = True
    return period_start, period_end, has_period


def _inventory_export_table(
    db: Session,
    warehouse_id: int,
    *,
    search_q: str = "",
    sort: str = SORT_NAME,
    stock_filter: str = FILTER_ALL,
    scope: str = "all",
    ids_raw: str | None = None,
    start_str: str | None = None,
    end_str: str | None = None,
) -> tuple[list[str], list[list], bool]:
    from fastapi import HTTPException

    period_start, period_end, has_period = _parse_period(start_str, end_str)

    product_ids: set[int] | None = None
    if scope == "selected":
        product_ids = _parse_product_ids(ids_raw)
        if not product_ids:
            raise HTTPException(status_code=400, detail="لم يُحدَّد أي صنف للتصدير.")

    rows = build_inventory_export_rows(
        db,
        warehouse_id,
        search_q=search_q,
        sort=sort,
        stock_filter=stock_filter,
        product_ids=product_ids,
        period_start=period_start,
        period_end=period_end,
    )

    headers = [
        "الصنف",
        "الباركود",
        "SKU",
        "الوحدة",
        "الرصيد الحالي",
        "حد التنبيه",
        "متوسط التكلفة",
        "القيمة المالية",
        "حالة",
    ]
    if has_period:
        headers.extend(["طلب (بيع) في الفترة", "صرف في الفترة", "إدخال في الفترة"])

    table_rows = []
    for r in rows:
        p = r.product
        line = [
            p.name_ar,
            p.barcode or "",
            p.sku or "",
            p.unit or "",
            format_qty_plain(r.balance),
            format_qty_plain(p.reorder_level or 0),
            r.unit_cost if r.unit_cost is not None else "",
            r.line_value if r.unit_cost is not None else "",
            "منخفض" if r.is_low else "—",
        ]
        if has_period:
            line.extend(
                [
                    format_qty_plain(r.sale_in_period),
                    format_qty_plain(r.out_in_period),
                    format_qty_plain(r.in_in_period),
                ]
            )
        table_rows.append(line)

    return headers, table_rows, has_period


def _export_filename(
    warehouse_name: str, *, scope: str, start_str: str | None, end_str: str | None
) -> str:
    tag = "inventory"
    if start_str and end_str:
        tag += f"-{start_str}-to-{end_str}"
    suffix = "selected" if scope == "selected" else "all"
    return f"{tag}-{suffix}"


def inventory_export_csv(
    db: Session,
    warehouse_id: int,
    warehouse_name: str,
    *,
    search_q: str = "",
    sort: str = SORT_NAME,
    stock_filter: str = FILTER_ALL,
    scope: str = "all",
    ids_raw: str | None = None,
    start_str: str | None = None,
    end_str: str | None = None,
):
    from modules.reporting.exports import csv_response

    headers, rows, _has_period = _inventory_export_table(
        db,
        warehouse_id,
        search_q=search_q,
        sort=sort,
        stock_filter=stock_filter,
        scope=scope,
        ids_raw=ids_raw,
        start_str=start_str,
        end_str=end_str,
    )
    return csv_response(
        _export_filename(
            warehouse_name, scope=scope, start_str=start_str, end_str=end_str
        ),
        headers,
        rows,
    )


def inventory_export_xlsx(
    db: Session,
    warehouse_id: int,
    warehouse_name: str,
    *,
    search_q: str = "",
    sort: str = SORT_NAME,
    stock_filter: str = FILTER_ALL,
    scope: str = "all",
    ids_raw: str | None = None,
    start_str: str | None = None,
    end_str: str | None = None,
):
    from modules.reporting.exports import SheetSpec, xlsx_response

    headers, rows, _has_period = _inventory_export_table(
        db,
        warehouse_id,
        search_q=search_q,
        sort=sort,
        stock_filter=stock_filter,
        scope=scope,
        ids_raw=ids_raw,
        start_str=start_str,
        end_str=end_str,
    )

    return xlsx_response(
        _export_filename(
            warehouse_name, scope=scope, start_str=start_str, end_str=end_str
        ),
        [SheetSpec(name="المخزون", headers=headers, rows=rows)],
    )
