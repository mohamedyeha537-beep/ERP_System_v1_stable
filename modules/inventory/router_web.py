from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import (
    CATALOG_WRITE,
    INVENTORY_ADJUST,
    INVENTORY_VIEW,
    WAREHOUSES_MANAGE,
)
from modules.catalog.models import Product, ProductKind
from modules.catalog.service import list_stockable_products
from modules.inventory.inventory_page import (
    FILTER_ALL,
    PAGE_SIZE_OPTIONS,
    SORT_MOST_REQUESTED,
    SORT_MOST_USED,
    SORT_NAME,
    build_inventory_page,
)
from modules.inventory.loss_reasons import get_loss_reasons
from modules.inventory.models import StockMovementType
from modules.inventory.product_ledger import (
    LedgerError,
    apply_stock_count,
    apply_waste,
    bom_usage_for_component,
    build_product_ledger,
)
from modules.inventory.service import (
    apply_movement,
    get_balance,
    get_main_warehouse,
    get_warehouse,
    default_inventory_warehouse_id,
    list_sales_deduction_warehouses,
    list_warehouses,
    low_stock_by_warehouse,
    low_stock_products,
    reconcile_all_stock_balances,
    resolve_warehouse_id,
)
from modules.inventory.transfer_service import create_transfer_request_between

router = APIRouter(prefix="/inventory", tags=["inventory"])


def _has_direct_stock(product: Product) -> bool:
    return product.kind == ProductKind.STOCK_ONLY or (
        product.kind == ProductKind.FINAL_SELLABLE
        and bool(product.direct_purchase_enabled)
    )


def _parse_warehouse_id(db, raw: str | None) -> int:
    if raw:
        try:
            return resolve_warehouse_id(db, int(raw))
        except Exception:
            pass
    return default_inventory_warehouse_id(db)


@router.get("/export.csv")
def inventory_export_csv(
    db: DBSession,
    _: User = Depends(require_permission(INVENTORY_VIEW)),
    w: str | None = Query(None),
    q: str | None = Query(None),
    sort: str = Query(SORT_NAME),
    stock: str = Query(FILTER_ALL),
    scope: str = Query("all"),
    ids: str | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
) -> Response:
    from modules.inventory.export import inventory_export_csv as build_csv

    warehouse_id = _parse_warehouse_id(db, w)
    warehouses = list_warehouses(db)
    sales_deduction_warehouses = list_sales_deduction_warehouses(db)
    current_wh = next((x for x in warehouses if x.id == warehouse_id), get_main_warehouse(db))
    scope_norm = (scope or "all").strip().lower()
    if scope_norm not in ("all", "selected"):
        raise HTTPException(status_code=400, detail="نطاق التصدير غير صالح.")
    return build_csv(
        db,
        warehouse_id,
        current_wh.name_ar,
        search_q=q or "",
        sort=sort,
        stock_filter=stock,
        scope=scope_norm,
        ids_raw=ids,
        start_str=start,
        end_str=end,
    )


@router.get("/export.xlsx")
def inventory_export_xlsx(
    db: DBSession,
    _: User = Depends(require_permission(INVENTORY_VIEW)),
    w: str | None = Query(None),
    q: str | None = Query(None),
    sort: str = Query(SORT_NAME),
    stock: str = Query(FILTER_ALL),
    scope: str = Query("all"),
    ids: str | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
) -> Response:
    from modules.inventory.export import inventory_export_xlsx as build_xlsx

    warehouse_id = _parse_warehouse_id(db, w)
    warehouses = list_warehouses(db)
    current_wh = next((x for x in warehouses if x.id == warehouse_id), get_main_warehouse(db))
    scope_norm = (scope or "all").strip().lower()
    if scope_norm not in ("all", "selected"):
        raise HTTPException(status_code=400, detail="نطاق التصدير غير صالح.")
    return build_xlsx(
        db,
        warehouse_id,
        current_wh.name_ar,
        search_q=q or "",
        sort=sort,
        stock_filter=stock,
        scope=scope_norm,
        ids_raw=ids,
        start_str=start,
        end_str=end,
    )


@router.get("", response_class=HTMLResponse)
def inventory_home(
    request: Request,
    db: DBSession,
    _: User = Depends(require_permission(INVENTORY_VIEW)),
    w: str | None = Query(None),
    q: str | None = Query(None),
    sort: str = Query(SORT_NAME),
    stock: str = Query(FILTER_ALL),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
):
    warehouses = list_warehouses(db)
    sales_deduction_warehouses = list_sales_deduction_warehouses(db)
    warehouse_id = _parse_warehouse_id(db, w)
    current_wh = next((x for x in warehouses if x.id == warehouse_id), get_main_warehouse(db))
    reconcile_all_stock_balances(db)
    all_products = list_stockable_products(db)
    balances = {p.id: get_balance(db, p.id, warehouse_id) for p in all_products}
    low = low_stock_products(db, warehouse_id)
    stockable_ids = {p.id for p in all_products}
    low = [(p, qty) for (p, qty) in low if p.id in stockable_ids]
    low_all = low_stock_by_warehouse(db)
    from modules.reporting import queries as report_queries

    avg_costs = report_queries.avg_unit_cost_per_product(db)
    inv_page = build_inventory_page(
        db,
        warehouse_id,
        search_q=q or "",
        sort=sort,
        stock_filter=stock,
        page=page,
        page_size=page_size,
        avg_costs=avg_costs,
        get_balance_fn=get_balance,
    )
    from app.datetime_local import format_local_dt, local_period_bounds, now_local

    month_start, _ = local_period_bounds("month")
    export_start_default = format_local_dt(month_start, "%Y-%m-%d")
    export_end_default = format_local_dt(now_local(), "%Y-%m-%d")
    return templates.TemplateResponse(
        "inventory.html",
        {
            "request": request,
            "all_products": all_products,
            "inventory_rows": inv_page.rows,
            "balances": balances,
            "avg_costs": avg_costs,
            "inventory_total": inv_page.inventory_total,
            "low_stock": low,
            "low_stock_all": low_all,
            "warehouses": warehouses,
            "sales_deduction_warehouses": sales_deduction_warehouses,
            "current_warehouse": current_wh,
            "warehouse_id": warehouse_id,
            "search_q": inv_page.search_q,
            "sort": inv_page.sort,
            "stock_filter": inv_page.stock_filter,
            "filter_all": FILTER_ALL,
            "filter_low": "low",
            "filter_in_stock": "in_stock",
            "filter_zero": "zero",
            "filter_negative": "negative",
            "page": inv_page.page,
            "page_size": inv_page.page_size,
            "total_pages": inv_page.total_pages,
            "total_count": inv_page.total_count,
            "page_size_options": PAGE_SIZE_OPTIONS,
            "sort_name": SORT_NAME,
            "sort_most_requested": SORT_MOST_REQUESTED,
            "sort_most_used": SORT_MOST_USED,
            "export_start_default": export_start_default,
            "export_end_default": export_end_default,
        },
    )


@router.post("/products/sales-warehouse", response_class=HTMLResponse)
def inventory_products_sales_warehouse_bulk(
    db: DBSession,
    _: User = Depends(require_permission(CATALOG_WRITE)),
    product_ids: list[int] = Form(default=[]),
    sales_warehouse_id: str = Form(""),
    w: str = Form(""),
    q: str = Form(""),
    sort: str = Form(SORT_NAME),
    page: int = Form(1),
    page_size: int = Form(25),
):
    back = f"/inventory?w={w or ''}&page={page}&page_size={page_size}&sort={sort}"
    if q.strip():
        from urllib.parse import quote

        back += "&q=" + quote(q.strip())
    if not product_ids:
        return RedirectResponse(back + "&err=no_products", status_code=302)

    target_id: int | None = None
    if sales_warehouse_id.strip():
        try:
            target_id = int(sales_warehouse_id.strip())
        except ValueError:
            return RedirectResponse(back + "&err=sales_warehouse", status_code=302)
        wh = get_warehouse(db, target_id)
        if wh is None or not wh.is_active or not wh.deduct_sales_enabled:
            return RedirectResponse(back + "&err=sales_warehouse", status_code=302)

    products = list(
        db.scalars(
            select(Product).where(
                Product.id.in_(product_ids),
                (
                    (Product.kind == ProductKind.STOCK_ONLY)
                    | (
                        (Product.kind == ProductKind.FINAL_SELLABLE)
                        & Product.direct_purchase_enabled.is_(True)
                    )
                ),
            )
        ).all()
    )
    for p in products:
        p.sales_warehouse_id = target_id
    db.commit()
    return RedirectResponse(back + f"&assigned={len(products)}", status_code=302)


@router.post("/products/transfer", response_class=HTMLResponse)
def inventory_products_transfer_bulk(
    db: DBSession,
    user: User = Depends(require_permission(WAREHOUSES_MANAGE)),
    product_ids: list[int] = Form(default=[]),
    quantities: list[str] = Form(default=[]),
    from_warehouse_id: int = Form(...),
    to_warehouse_id: str = Form(""),
    note: str = Form(""),
    w: str = Form(""),
    q: str = Form(""),
    sort: str = Form(SORT_NAME),
    page: int = Form(1),
    page_size: int = Form(25),
):
    back = f"/inventory?w={w or from_warehouse_id}&page={page}&page_size={page_size}&sort={sort}"
    if q.strip():
        from urllib.parse import quote

        back += "&q=" + quote(q.strip())
    if not product_ids:
        return RedirectResponse(back + "&err=no_products", status_code=302)
    try:
        target_id = int(to_warehouse_id.strip())
    except ValueError:
        return RedirectResponse(back + "&err=transfer_target", status_code=302)
    if target_id == from_warehouse_id:
        return RedirectResponse(back + "&err=transfer_target", status_code=302)

    lines: list[tuple[int, Decimal]] = []
    for pid, raw_qty in zip(product_ids, quantities):
        try:
            qty = Decimal((raw_qty or "0").strip() or "0")
        except Exception:
            return RedirectResponse(back + "&err=transfer_qty", status_code=302)
        if qty > 0:
            lines.append((int(pid), qty))
    if not lines:
        return RedirectResponse(back + "&err=transfer_qty", status_code=302)

    try:
        transfer = create_transfer_request_between(
            db,
            from_warehouse_id=from_warehouse_id,
            to_warehouse_id=target_id,
            lines=lines,
            user_id=user.id,
            note=note or None,
        )
        db.commit()
    except Exception as e:
        db.rollback()
        from urllib.parse import quote

        return RedirectResponse(back + "&err=" + quote(str(e)), status_code=302)
    return RedirectResponse(back + f"&transfer_created={transfer.id}", status_code=302)


@router.post("/movement", response_class=HTMLResponse)
def inventory_movement(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(INVENTORY_ADJUST)),
    product_id: int = Form(...),
    quantity: str = Form(...),
    movement_type: str = Form(...),
    note: str = Form(""),
    warehouse_id: int = Form(...),
):
    qty = Decimal(quantity.strip())
    try:
        mt = StockMovementType(movement_type)
    except ValueError:
        return RedirectResponse("/inventory?err=type", status_code=302)
    if mt == StockMovementType.SALE:
        return RedirectResponse("/inventory?err=sale_via_pos", status_code=302)
    target = db.get(Product, product_id)
    if target is None or not _has_direct_stock(target):
        return RedirectResponse("/inventory?err=composite", status_code=302)
    delta = qty
    if mt in (StockMovementType.PURCHASE, StockMovementType.ONLINE_SYNC):
        if delta < 0:
            delta = -delta
    elif mt == StockMovementType.ADJUSTMENT:
        pass
    else:
        delta = qty
    try:
        wid = resolve_warehouse_id(db, warehouse_id)
        apply_movement(
            db,
            product_id=product_id,
            quantity_delta=delta,
            movement_type=mt,
            user_id=user.id,
            warehouse_id=wid,
            sale_id=None,
            note=note.strip() or None,
        )
        db.commit()
    except Exception:
        db.rollback()
        return RedirectResponse(f"/inventory?w={warehouse_id}&err=stock", status_code=302)
    return RedirectResponse(f"/inventory?w={warehouse_id}", status_code=302)


def _ledger_redirect(product_id: int, warehouse_id: int, **qs: str) -> RedirectResponse:
    parts = [f"w={warehouse_id}"]
    for k, v in qs.items():
        if v:
            parts.append(f"{k}={v}")
    return RedirectResponse(
        f"/inventory/products/{product_id}?{'&'.join(parts)}",
        status_code=302,
    )


def _sales_history_for_product(db: DBSession, product_id: int, limit: int = 80):
    from modules.sales.models import Sale, SaleLine, SaleStatus

    return list(
        db.execute(
            select(SaleLine, Sale)
            .join(Sale, SaleLine.sale_id == Sale.id)
            .where(
                SaleLine.product_id == product_id,
                Sale.status == SaleStatus.COMPLETED,
            )
            .order_by(Sale.id.desc(), SaleLine.id.desc())
            .limit(limit)
        ).all()
    )


@router.get("/products/{product_id}", response_class=HTMLResponse)
def product_ledger_page(
    request: Request,
    product_id: int,
    db: DBSession,
    user: User = Depends(require_permission(INVENTORY_VIEW)),
    w: str | None = Query(None),
):
    from modules.authz.service import user_has_permission

    product = db.get(Product, product_id)
    if product is None:
        return RedirectResponse("/catalog/products", status_code=302)
    warehouse_id = _parse_warehouse_id(db, w)
    can_adjust = user_has_permission(user, INVENTORY_ADJUST)
    ctx = {
        "request": request,
        "product": product,
        "warehouses": list_warehouses(db),
        "warehouse_id": warehouse_id,
        "can_adjust": can_adjust,
        "saved": request.query_params.get("saved") == "1",
        "error": {
            "count": "تعذّر حفظ الجرد — تحقّق من الكمية.",
            "waste": "تعذّر تسجيل الهدر — تحقّق من الكمية والرصيد.",
            "reason": "سبب الهدر غير صالح — اختر من القائمة.",
        }.get(request.query_params.get("err") or ""),
        "loss_reasons": get_loss_reasons(db),
        "bom_usage": [],
        "ledger_rows": [],
        "inventory_lots": [],
        "current_balance": Decimal("0"),
        "sales_history": [],
    }
    if _has_direct_stock(product):
        from modules.reporting import queries as report_queries

        bal = get_balance(db, product_id, warehouse_id)
        avg_costs = report_queries.avg_unit_cost_per_product(db)
        unit_cost = avg_costs.get(product_id)
        inventory_value = (
            (bal * unit_cost).quantize(Decimal("0.001"))
            if unit_cost is not None
            else Decimal("0")
        )
        ctx["current_balance"] = bal
        ctx["unit_cost"] = unit_cost
        ctx["inventory_value"] = inventory_value
        ctx["bom_usage"] = bom_usage_for_component(db, product_id)
        ctx["ledger_rows"] = build_product_ledger(
            db, product_id, warehouse_id=warehouse_id
        )
        from modules.inventory.lots import list_active_lots

        ctx["inventory_lots"] = list_active_lots(
            db, product_id=product_id, warehouse_id=warehouse_id
        )
    if product.kind == ProductKind.FINAL_SELLABLE:
        ctx["sales_history"] = _sales_history_for_product(db, product_id)
    return templates.TemplateResponse("inventory_product_ledger.html", ctx)


@router.post("/products/{product_id}/count", response_class=HTMLResponse)
def product_stock_count(
    product_id: int,
    db: DBSession,
    user: User = Depends(require_permission(INVENTORY_ADJUST)),
    warehouse_id: int = Form(...),
    counted_qty: str = Form(...),
):
    product = db.get(Product, product_id)
    if product is None or not _has_direct_stock(product):
        return RedirectResponse("/inventory", status_code=302)
    try:
        counted = Decimal((counted_qty or "").strip())
        apply_stock_count(
            db,
            product_id=product_id,
            warehouse_id=warehouse_id,
            counted_qty=counted,
            user_id=user.id,
        )
        db.commit()
    except (LedgerError, Exception):
        db.rollback()
        return _ledger_redirect(product_id, warehouse_id, err="count")
    return _ledger_redirect(product_id, warehouse_id, saved="1")


@router.post("/products/{product_id}/waste", response_class=HTMLResponse)
def product_waste(
    product_id: int,
    db: DBSession,
    user: User = Depends(require_permission(INVENTORY_ADJUST)),
    warehouse_id: int = Form(...),
    quantity: str = Form(...),
    reason: str = Form(...),
    note: str = Form(""),
):
    product = db.get(Product, product_id)
    if product is None or not _has_direct_stock(product):
        return RedirectResponse("/inventory", status_code=302)
    allowed = set(get_loss_reasons(db))
    reason_label = (reason or "").strip()
    if reason_label not in allowed:
        return _ledger_redirect(product_id, warehouse_id, err="reason")
    try:
        apply_waste(
            db,
            product_id=product_id,
            warehouse_id=warehouse_id,
            quantity=Decimal((quantity or "").strip()),
            reason_label=reason_label,
            user_id=user.id,
            extra_note=note,
        )
        db.commit()
    except (LedgerError, Exception):
        db.rollback()
        return _ledger_redirect(product_id, warehouse_id, err="waste")
    return _ledger_redirect(product_id, warehouse_id, saved="1")
