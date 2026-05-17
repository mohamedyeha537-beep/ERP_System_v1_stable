from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import REPORTS_VIEW
from modules.customers.models import Customer
from modules.delivery.service import delivery_fee_cash_out_total
from modules.payments.daily_burden import (
    compute_daily_burden,
    recurring_costs_breakdown_in_period,
    recurring_costs_in_period,
)
from modules.payments.depreciation import (
    consumable_assets_total_in_period,
    fixed_assets_summary,
    list_consumable_asset_lines_in_period,
    list_fixed_asset_lines,
    snapshot_at,
    total_depreciation_in_period,
)
from modules.payments.models import PaymentMethod, PurchaseKind
from modules.payments.service import list_purchases, wallet_breakdown
from modules.reporting import queries as report_queries
from modules.reporting.exports import csv_response

router = APIRouter(prefix="/reports", tags=["reports"])


_PERIOD_LABELS = {
    "day": "اليوم",
    "week": "الأسبوع الحالي",
    "month": "الشهر الحالي",
    "year": "السنة الحالية",
    "custom": "فترة مخصصة",
}


def _resolve_period(
    period: str, start: str | None, end: str | None
) -> tuple[str, datetime, datetime]:
    """Returns (effective_period, start, end) — falls back to 'day' on bad custom input."""
    if period == "custom":
        custom = report_queries.parse_custom_range(start, end)
        if custom is None:
            s, e = report_queries.period_bounds("day")
            return "day", s, e
        return "custom", custom[0], custom[1]
    if period not in _PERIOD_LABELS:
        period = "day"
    s, e = report_queries.period_bounds(period)
    return period, s, e


def _common_ctx(request: Request, period: str, s, e, start: str | None, end: str | None):
    return {
        "request": request,
        "period": period,
        "period_labels": _PERIOD_LABELS,
        "start": s,
        "end": e,
        "start_str": start or s.strftime("%Y-%m-%d"),
        "end_str": end or (e.strftime("%Y-%m-%d") if e else ""),
    }


_perm = require_permission(REPORTS_VIEW)


def _payment_flow_totals(rows) -> tuple[Decimal, Decimal, Decimal]:
    cash_in = sum((row[2] for row in rows), Decimal("0"))
    refunds_out = sum((row[4] for row in rows), Decimal("0"))
    net = (cash_in - refunds_out).quantize(Decimal("0.001"))
    return (
        cash_in.quantize(Decimal("0.001")),
        refunds_out.quantize(Decimal("0.001")),
        net,
    )


@router.get("", response_class=HTMLResponse)
def reports_hub(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)

    sales_sum = report_queries.sales_summary(db, s, e)
    sales_by_pm = report_queries.sales_by_payment_method(db, s, e)
    sales_cash_in, refunds_cash_out, net_collected = _payment_flow_totals(sales_by_pm)
    delivery_cash_out = delivery_fee_cash_out_total(db, start=s, end=e)
    inv_purch = report_queries.inventory_purchases_summary(db, s, e)
    expenses = report_queries.expenses_summary(db, s, e)
    assets = report_queries.assets_summary(db, s, e)
    cogs = report_queries.cogs_summary(db, s, e)
    consumables_period = consumable_assets_total_in_period(db, s, e)
    depreciation_period = total_depreciation_in_period(db, s, e)
    operating_assets_expense = (consumables_period + depreciation_period).quantize(
        Decimal("0.001")
    )
    # حصة التكاليف الشهرية (رواتب، إيجار، اشتراكات...) المنسوبة للفترة
    rec_monthly_total, rec_period_share, period_days = recurring_costs_in_period(
        db, s, e
    )
    rec_breakdown = recurring_costs_breakdown_in_period(db, s, e)

    # حسبة (1): إجمالي الربح / هامش المساهمة = إيراد − تكلفة المباع
    gross_profit = (sales_sum.revenue - cogs).quantize(Decimal("0.001"))
    # حسبة (2): صافي الربح = إجمالي − (مصاريف + رواتب وإيجار + مستلزمات + إهلاك)
    net_profit = (
        gross_profit
        - expenses.total
        - rec_period_share
        - operating_assets_expense
    ).quantize(Decimal("0.001"))
    cashflow = (
        net_collected - delivery_cash_out - inv_purch.total - expenses.total - assets.total
    ).quantize(Decimal("0.001"))
    inv_value = report_queries.inventory_value(db)
    low_count = sum(1 for r in report_queries.inventory_snapshot(db, only_low=True))

    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "sales_sum": sales_sum,
            "sales_by_pm": sales_by_pm,
            "sales_cash_in": sales_cash_in,
            "refunds_cash_out": refunds_cash_out,
            "net_collected": net_collected,
            "delivery_cash_out": delivery_cash_out,
            "inv_purch": inv_purch,
            "expenses": expenses,
            "assets": assets,
            "cogs": cogs,
            "gross_profit": gross_profit,
            "net_profit": net_profit,
            "cashflow": cashflow,
            "inv_value": inv_value,
            "low_count": low_count,
            "consumables_period": consumables_period,
            "depreciation_period": depreciation_period,
            "operating_assets_expense": operating_assets_expense,
            "rec_monthly_total": rec_monthly_total,
            "rec_period_share": rec_period_share,
            "rec_breakdown": rec_breakdown,
            "period_days": period_days,
        }
    )
    return templates.TemplateResponse("reports_hub.html", ctx)


@router.get("/sales", response_class=HTMLResponse)
def reports_sales(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    period: str = Query("day"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)
    summary = report_queries.sales_summary(db, s, e)
    top = report_queries.top_products(db, s, e)
    sales_by_pm = report_queries.sales_by_payment_method(db, s, e)
    sales_cash_in, refunds_cash_out, net_collected = _payment_flow_totals(sales_by_pm)
    delivery_cash_out = delivery_fee_cash_out_total(db, start=s, end=e)
    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "summary": summary,
            "top": top,
            "sales_by_pm": sales_by_pm,
            "sales_cash_in": sales_cash_in,
            "refunds_cash_out": refunds_cash_out,
            "net_collected": net_collected,
            "delivery_cash_out": delivery_cash_out,
        }
    )
    return templates.TemplateResponse("reports_sales.html", ctx)


@router.get("/purchases", response_class=HTMLResponse)
def reports_purchases(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)
    summary = report_queries.inventory_purchases_summary(db, s, e)
    by_pm = report_queries.purchases_by_payment_method(
        db, s, e, kind=PurchaseKind.INVENTORY
    )
    items = list_purchases(db, s, e, kind=PurchaseKind.INVENTORY)
    pay_filter = (request.query_params.get("pay") or "all").lower()
    if pay_filter not in ("all", "paid", "partial", "unpaid"):
        pay_filter = "all"
    balance_only = request.query_params.get("balance_only") == "1"
    from modules.payables.service import build_payable_rows, payables_summary

    ap_summary = payables_summary(db, kind=PurchaseKind.INVENTORY)
    pay_rows = build_payable_rows(
        db,
        start=s,
        end=e,
        kind=PurchaseKind.INVENTORY,
        pay_filter=pay_filter,
        only_with_balance=balance_only,
    )
    pay_by_id = {r.purchase_id: r for r in pay_rows}
    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "summary": summary,
            "by_pm": by_pm,
            "items": items,
            "ap_summary": ap_summary,
            "pay_by_id": pay_by_id,
            "pay_filter": pay_filter,
            "balance_only": balance_only,
        }
    )
    return templates.TemplateResponse("reports_purchases.html", ctx)


@router.get("/expenses", response_class=HTMLResponse)
def reports_expenses(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)
    summary = report_queries.expenses_summary(db, s, e)
    by_pm = report_queries.purchases_by_payment_method(
        db, s, e, kind=PurchaseKind.EXPENSE
    )
    by_cat = report_queries.expenses_by_category(db, s, e)
    items = list_purchases(db, s, e, kind=PurchaseKind.EXPENSE)
    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "summary": summary,
            "by_pm": by_pm,
            "by_cat": by_cat,
            "items": items,
        }
    )
    return templates.TemplateResponse("reports_expenses.html", ctx)


@router.get("/profit", response_class=HTMLResponse)
def reports_profit(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)
    sales_sum = report_queries.sales_summary(db, s, e)
    sales_by_pm = report_queries.sales_by_payment_method(db, s, e)
    sales_cash_in, refunds_cash_out, net_collected = _payment_flow_totals(sales_by_pm)
    delivery_cash_out = delivery_fee_cash_out_total(db, start=s, end=e)
    cogs = report_queries.cogs_summary(db, s, e)
    expenses = report_queries.expenses_summary(db, s, e)
    assets = report_queries.assets_summary(db, s, e)
    inv_purch = report_queries.inventory_purchases_summary(db, s, e)
    # المعالجة المحاسبية الصحيحة (IAS 16): الأصل الثابت يُهلَك، والمستهلك يُخصم كاملاً.
    consumables_period = consumable_assets_total_in_period(db, s, e)
    depreciation_period = total_depreciation_in_period(db, s, e)
    operating_assets_expense = (consumables_period + depreciation_period).quantize(
        Decimal("0.001")
    )
    rec_monthly_total, rec_period_share, period_days = recurring_costs_in_period(
        db, s, e
    )
    rec_breakdown = recurring_costs_breakdown_in_period(db, s, e)
    gross_profit = (sales_sum.revenue - cogs).quantize(Decimal("0.001"))
    net_profit = (
        gross_profit
        - expenses.total
        - rec_period_share
        - operating_assets_expense
    ).quantize(Decimal("0.001"))
    cashflow = (
        net_collected - delivery_cash_out - inv_purch.total - expenses.total - assets.total
    ).quantize(Decimal("0.001"))
    by_product = report_queries.profit_by_product(db, s, e)
    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "sales_sum": sales_sum,
            "sales_by_pm": sales_by_pm,
            "sales_cash_in": sales_cash_in,
            "refunds_cash_out": refunds_cash_out,
            "net_collected": net_collected,
            "delivery_cash_out": delivery_cash_out,
            "cogs": cogs,
            "expenses": expenses,
            "assets": assets,
            "inv_purch": inv_purch,
            "consumables_period": consumables_period,
            "depreciation_period": depreciation_period,
            "operating_assets_expense": operating_assets_expense,
            "rec_monthly_total": rec_monthly_total,
            "rec_period_share": rec_period_share,
            "rec_breakdown": rec_breakdown,
            "period_days": period_days,
            "gross_profit": gross_profit,
            "net_profit": net_profit,
            "cashflow": cashflow,
            "by_product": by_product,
        }
    )
    return templates.TemplateResponse("reports_profit.html", ctx)


@router.get("/inventory", response_class=HTMLResponse)
def reports_inventory(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    q: str = Query(""),
    only_low: int = Query(0, ge=0, le=1),
    w: str | None = Query(None),
):
    from modules.inventory.service import get_main_warehouse, get_warehouse, list_warehouses

    period, s, e = _resolve_period(period, start, end)
    warehouses = list_warehouses(db)
    warehouse_id = get_main_warehouse(db).id
    if w:
        try:
            wid = int(w)
            if get_warehouse(db, wid) is not None:
                warehouse_id = wid
        except ValueError:
            pass
    current_wh = get_warehouse(db, warehouse_id) or get_main_warehouse(db)
    rows = report_queries.inventory_snapshot(
        db, search=q or None, only_low=bool(only_low), warehouse_id=warehouse_id
    )
    movements = report_queries.stock_movements_aggregate(
        db, s, e, warehouse_id=warehouse_id
    )
    inv_value = report_queries.inventory_value(db, warehouse_id)
    inv_by_wh = report_queries.inventory_value_by_warehouse(db)
    low_count = sum(
        1
        for r in report_queries.inventory_snapshot(
            db, only_low=True, warehouse_id=warehouse_id
        )
    )
    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "rows": rows,
            "movements": movements,
            "q": q,
            "only_low": bool(only_low),
            "inv_value": inv_value,
            "inv_by_wh": inv_by_wh,
            "low_count": low_count,
            "warehouses": warehouses,
            "warehouse_id": warehouse_id,
            "current_warehouse": current_wh,
        }
    )
    return templates.TemplateResponse("reports_inventory.html", ctx)


@router.get("/assets", response_class=HTMLResponse)
def reports_assets(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)
    summary = report_queries.assets_summary(db, s, e)
    by_pm = report_queries.purchases_by_payment_method(
        db, s, e, kind=PurchaseKind.ASSET
    )
    items = list_purchases(db, s, e, kind=PurchaseKind.ASSET)
    consumables_total = consumable_assets_total_in_period(db, s, e)
    depreciation_total = total_depreciation_in_period(db, s, e)
    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "summary": summary,
            "by_pm": by_pm,
            "items": items,
            "consumables_total": consumables_total,
            "depreciation_total": depreciation_total,
        }
    )
    return templates.TemplateResponse("reports_assets.html", ctx)


@router.get("/fixed-assets", response_class=HTMLResponse)
def reports_fixed_assets(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    """سجل الأصول الثابتة وحسابات الإهلاك (Fixed Assets Register)."""
    period, s, e = _resolve_period(period, start, end)
    lines = list_fixed_asset_lines(db)
    snapshots = []
    period_depr_total = Decimal("0")
    for ln in lines:
        snap = snapshot_at(ln, e)
        from modules.payments.depreciation import depreciation_in_period

        period_depr = depreciation_in_period(ln, s, e)
        period_depr_total += period_depr
        snapshots.append({"snap": snap, "period_depr": period_depr})
    summary = fixed_assets_summary(db, e)
    consumables_total = consumable_assets_total_in_period(db, s, e)
    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "snapshots": snapshots,
            "summary": summary,
            "period_depr_total": period_depr_total.quantize(Decimal("0.001")),
            "consumables_total": consumables_total,
        }
    )
    return templates.TemplateResponse("reports_fixed_assets.html", ctx)


@router.get("/comprehensive", response_class=HTMLResponse)
def reports_comprehensive(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)

    sales_sum = report_queries.sales_summary(db, s, e)
    sales_by_pm = report_queries.sales_by_payment_method(db, s, e)
    sales_cash_in, refunds_cash_out, net_collected = _payment_flow_totals(sales_by_pm)
    delivery_cash_out = delivery_fee_cash_out_total(db, start=s, end=e)
    cogs = report_queries.cogs_summary(db, s, e)
    inv_purch = report_queries.inventory_purchases_summary(db, s, e)
    expenses = report_queries.expenses_summary(db, s, e)
    assets = report_queries.assets_summary(db, s, e)
    consumables_period = consumable_assets_total_in_period(db, s, e)
    depreciation_period = total_depreciation_in_period(db, s, e)
    operating_assets_expense = (consumables_period + depreciation_period).quantize(
        Decimal("0.001")
    )
    rec_monthly_total, rec_period_share, period_days = recurring_costs_in_period(
        db, s, e
    )
    rec_breakdown = recurring_costs_breakdown_in_period(db, s, e)
    gross_profit = (sales_sum.revenue - cogs).quantize(Decimal("0.001"))
    net_profit = (
        gross_profit
        - expenses.total
        - rec_period_share
        - operating_assets_expense
    ).quantize(Decimal("0.001"))
    cashflow = (
        net_collected - delivery_cash_out - inv_purch.total - expenses.total - assets.total
    ).quantize(Decimal("0.001"))
    purch_by_pm = report_queries.purchases_by_payment_method(
        db, s, e, kind=PurchaseKind.INVENTORY
    )
    exp_by_pm = report_queries.purchases_by_payment_method(
        db, s, e, kind=PurchaseKind.EXPENSE
    )
    asset_by_pm = report_queries.purchases_by_payment_method(
        db, s, e, kind=PurchaseKind.ASSET
    )
    exp_by_cat = report_queries.expenses_by_category(db, s, e)

    top = report_queries.top_products(db, s, e, limit=10)
    profit_top = report_queries.profit_by_product(db, s, e, limit=10)

    inv_value = report_queries.inventory_value(db)
    low_rows = report_queries.inventory_snapshot(db, only_low=True)
    wallet_rows = wallet_breakdown(db, None, None)

    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "sales_sum": sales_sum,
            "sales_cash_in": sales_cash_in,
            "refunds_cash_out": refunds_cash_out,
            "net_collected": net_collected,
            "delivery_cash_out": delivery_cash_out,
            "cogs": cogs,
            "inv_purch": inv_purch,
            "expenses": expenses,
            "assets": assets,
            "consumables_period": consumables_period,
            "depreciation_period": depreciation_period,
            "operating_assets_expense": operating_assets_expense,
            "rec_monthly_total": rec_monthly_total,
            "rec_period_share": rec_period_share,
            "rec_breakdown": rec_breakdown,
            "period_days": period_days,
            "gross_profit": gross_profit,
            "net_profit": net_profit,
            "cashflow": cashflow,
            "sales_by_pm": sales_by_pm,
            "purch_by_pm": purch_by_pm,
            "exp_by_pm": exp_by_pm,
            "asset_by_pm": asset_by_pm,
            "exp_by_cat": exp_by_cat,
            "top": top,
            "profit_top": profit_top,
            "inv_value": inv_value,
            "low_rows": low_rows,
            "wallet_rows": wallet_rows,
        }
    )
    return templates.TemplateResponse("reports_comprehensive.html", ctx)


def _financial_operational_filter_qs(
    period: str,
    start: str | None,
    end: str | None,
    user_id: int | None,
    payment_method_id: int | None,
    customer_id: int | None,
) -> str:
    q: dict[str, str] = {"period": period}
    if period == "custom":
        if start:
            q["start"] = start
        if end:
            q["end"] = end
    if user_id is not None:
        q["user_id"] = str(user_id)
    if payment_method_id is not None:
        q["payment_method_id"] = str(payment_method_id)
    if customer_id is not None:
        q["customer_id"] = str(customer_id)
    return urlencode(q)


@router.get("/financial-operational", response_class=HTMLResponse)
def reports_financial_operational(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    user_id: str | None = Query(None),
    payment_method_id: str | None = Query(None),
    customer_id: str | None = Query(None),
):
    """تقرير مالي تشغيلي موثّق: استحقاق مبيعات + مرتجعات + تحصيل/ردود — دون مسودات ودون جدول قيود عام بعد."""
    period, s, e = _resolve_period(period, start, end)

    def _parse_opt_id(raw: str | None) -> int | None:
        if raw is None or str(raw).strip() == "":
            return None
        try:
            return int(str(raw).strip())
        except ValueError:
            return None

    uid = _parse_opt_id(user_id)
    pmid = _parse_opt_id(payment_method_id)
    cid = _parse_opt_id(customer_id)
    filters = report_queries.ReportFilters(
        user_id=uid,
        payment_method_id=pmid,
        customer_id=cid,
    )
    stmt = report_queries.financial_operational_statement(db, s, e, filters)
    logical = report_queries.financial_operational_logical_lines(stmt)
    users = list(db.scalars(select(User).order_by(User.username)).all())
    methods = list(
        db.scalars(select(PaymentMethod).order_by(PaymentMethod.sort_order, PaymentMethod.id)).all()
    )
    customers = list(
        db.scalars(select(Customer).order_by(Customer.phone).limit(500)).all()
    )
    filter_qs = _financial_operational_filter_qs(
        period, start, end, uid, pmid, cid
    )
    extra_bits = []
    if uid is not None:
        extra_bits.append(f"user_id={uid}")
    if pmid is not None:
        extra_bits.append(f"payment_method_id={pmid}")
    if cid is not None:
        extra_bits.append(f"customer_id={cid}")
    filter_extra = ("&" + "&".join(extra_bits)) if extra_bits else ""
    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "stmt": stmt,
            "logical": logical,
            "filters": filters,
            "users": users,
            "payment_methods": methods,
            "customers": customers,
            "filter_qs": filter_qs,
            "filter_extra": filter_extra,
            "nav_active": "financial_ops",
        }
    )
    return templates.TemplateResponse("reports_financial_operational.html", ctx)


@router.get("/export/financial-operational.csv")
def export_financial_operational_csv(
    db: DBSession,
    _: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    user_id: str | None = Query(None),
    payment_method_id: str | None = Query(None),
    customer_id: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)

    def _parse_opt_id(raw: str | None) -> int | None:
        if raw is None or str(raw).strip() == "":
            return None
        try:
            return int(str(raw).strip())
        except ValueError:
            return None

    uid = _parse_opt_id(user_id)
    pmid = _parse_opt_id(payment_method_id)
    cid = _parse_opt_id(customer_id)
    filters = report_queries.ReportFilters(
        user_id=uid,
        payment_method_id=pmid,
        customer_id=cid,
    )
    stmt = report_queries.financial_operational_statement(db, s, e, filters)
    logical = report_queries.financial_operational_logical_lines(stmt)
    headers = ["البند", "الاتجاه التوضيحي", "المبلغ", "مصدر البيانات / الملاحظة"]
    rows: list[list] = [
        ["الفترة", "", f"{s.strftime('%Y-%m-%d')} → {e.strftime('%Y-%m-%d')}", ""],
        ["فلاتر المستخدم/طريقة الدفع/العميل", "", str(filters), ""],
        [],
        ["عدد الفواتير المكتملة", "", stmt.invoice_count, "sales حيث status=COMPLETED"],
        ["عدد سندات المرتجع المرحّلة", "", stmt.return_count, "sale_returns حيث status=POSTED"],
        ["إجمالي مبيعات الاستحقاق", "", stmt.gross_revenue, "مجموع sales.total"],
        ["إجمالي المرتجعات", "", stmt.returns_total, "مجموع sale_returns.total"],
        ["صافي إيراد الاستحقاق", "", stmt.net_revenue, "فرق الإيراد عن المرتجعات — لا عد مزدوج"],
        ["مجموع التحصيل (دفعات الفواتير المفسرة)", "", stmt.collections_total, "sale_payments"],
        ["مجموع ردود المبالغ", "", stmt.refunds_payment_total, "refund_payments"],
        ["صافي أثر نقدي تشغيلي", "", stmt.net_cash_effect, "تحصيل − ردود"],
        ["ضرائب معروضة في التقرير", "", stmt.taxes_included, "غير مطبّقة في نموذج البيع"],
        ["خصومات سطرية معروضة", "", stmt.line_discounts_included, "غير مطبّقة في نموذج البيع"],
        [],
        ["--- قيد منطقي توضيحي (ليس مرحّلاً في دفتر أستاذ عام) ---", "", "", ""],
    ]
    for label, side, amt, note in logical:
        rows.append([label, side, amt, note])
    rows.append(
        [
            "مطابقة ميزان المراجعة",
            "",
            "غير متاحة حتى يُبنى جدول قيود يومية مرحّل",
            "انظر خطة ERP / gl_journal",
        ]
    )
    return csv_response(
        f"financial-operational-{_period_tag(period, s, e)}",
        headers,
        rows,
    )


@router.get("/break-even", response_class=HTMLResponse)
def reports_break_even(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    days: int = Query(30, ge=1, le=31),
):
    """تقرير تحليل التعادل (CVP / Break-Even Analysis):
    يحسب العبء اليومي ويُقارن إيرادات أيام الفترة بالعتبة، مع إبراز الأيام الرابحة والخاسرة.
    """
    period, s, e = _resolve_period(period, start, end)
    burden = compute_daily_burden(db, operating_days_per_month=days)

    # توليد سلسلة يومية لمقارنة كل يوم بالعتبة
    from modules.reporting.queries import cogs_summary, sales_summary

    from datetime import timedelta as _td

    daily_rows = []
    cur = s.replace(hour=0, minute=0, second=0, microsecond=0)
    profit_days = 0
    loss_days = 0
    total_above = Decimal("0")
    total_below = Decimal("0")
    while cur < e:
        nxt_day = (cur + _td(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        ss = sales_summary(db, cur, nxt_day)
        cg = cogs_summary(db, cur, nxt_day)
        gross = (ss.revenue - cg).quantize(Decimal("0.001"))
        net = (gross - burden.grand_daily).quantize(Decimal("0.001"))
        is_profit = net >= 0 and ss.revenue > 0
        if ss.revenue > 0:
            if is_profit:
                profit_days += 1
                total_above += net
            else:
                loss_days += 1
                total_below += -net
        daily_rows.append(
            {
                "date": cur.strftime("%Y-%m-%d"),
                "revenue": ss.revenue,
                "cogs": cg,
                "gross": gross,
                "net": net,
                "is_profit": is_profit,
                "no_sales": ss.revenue == 0,
            }
        )
        cur = nxt_day

    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "burden": burden,
            "daily_rows": daily_rows,
            "profit_days": profit_days,
            "loss_days": loss_days,
            "total_above": total_above.quantize(Decimal("0.001")),
            "total_below": total_below.quantize(Decimal("0.001")),
            "operating_days": days,
        }
    )
    return templates.TemplateResponse("reports_break_even.html", ctx)


@router.get("/accounting-help", response_class=HTMLResponse)
def reports_accounting_help(
    request: Request,
    _: User = Depends(_perm),
):
    """دليل المعالجة المحاسبية مع المراجع (IAS 1, IAS 2, IAS 7, IAS 16, CVP)."""
    return templates.TemplateResponse(
        "accounting_help.html",
        {"request": request},
    )


# =====================================================================
#                             تصدير CSV
# =====================================================================
# كل endpoint من نوع `/export/<name>.csv` يولّد ملفاً جاهزاً للتنزيل.
# الملف بترميز UTF-8 BOM ليعرض العربية بشكل صحيح في Excel على Windows.


def _period_tag(period: str, s: datetime, e: datetime) -> str:
    return f"{period}-{s.strftime('%Y%m%d')}-to-{e.strftime('%Y%m%d')}"


@router.get("/export/sales.csv")
def export_sales_csv(
    db: DBSession,
    _: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    """تصدير قائمة المبيعات + الإجمالي."""
    period, s, e = _resolve_period(period, start, end)
    summary = report_queries.sales_summary(db, s, e)
    by_pm = report_queries.sales_by_payment_method(db, s, e)
    sales_cash_in, refunds_cash_out, net_collected = _payment_flow_totals(by_pm)
    delivery_cash_out = delivery_fee_cash_out_total(db, start=s, end=e)

    headers = ["البند", "القيمة"]
    rows: list[list] = [
        ["الفترة", f"{s.strftime('%Y-%m-%d')} → {e.strftime('%Y-%m-%d')}"],
        ["عدد الفواتير", summary.invoice_count],
        ["عدد سندات المرتجع", summary.return_count],
        ["إجمالي المبيعات", summary.gross_revenue],
        ["المرتجعات", summary.returns_total],
        ["صافي المبيعات", summary.net_revenue],
        ["متوسط الفاتورة", summary.avg_basket],
        ["التحصيل خلال الفترة", sales_cash_in],
        ["رد المبالغ خلال الفترة", refunds_cash_out],
        ["صافي التحصيل", net_collected],
        ["أجرة التوصيل الخارجة", delivery_cash_out],
        [],
        ["تفصيل التحصيل والرد حسب طريقة الدفع", ""],
        ["طريقة الدفع", "عمليات التحصيل", "التحصيل", "عمليات الرد", "رد المبالغ", "الصافي"],
    ]
    for name, sales_cnt, sales_total, refund_cnt, refund_total, net_total in by_pm:
        rows.append([name, sales_cnt, sales_total, refund_cnt, refund_total, net_total])
    return csv_response(f"sales-{_period_tag(period, s, e)}", headers, rows)


@router.get("/export/sales-detailed.csv")
def export_sales_detailed_csv(
    db: DBSession,
    _: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    """تصدير كل بنود المبيعات (سطر لكل صنف في كل فاتورة)."""
    from sqlalchemy import select as _sel
    from modules.catalog.models import Product
    from modules.payments.models import PaymentMethod, SalePayment
    from modules.refunds.models import SaleReturn, SaleReturnLine, SaleReturnStatus
    from modules.sales.models import Sale, SaleLine, SaleStatus

    period, s, e = _resolve_period(period, start, end)
    rows = db.execute(
        _sel(
            Sale.id,
            Sale.created_at,
            Product.name_ar,
            SaleLine.quantity,
            SaleLine.unit_price,
            SaleLine.line_total,
        )
        .join(SaleLine, SaleLine.sale_id == Sale.id)
        .join(Product, Product.id == SaleLine.product_id)
        .where(
            Sale.status == SaleStatus.COMPLETED,
            Sale.created_at >= s,
            Sale.created_at < e,
        )
        .order_by(Sale.created_at, Sale.id)
    ).all()

    # طريقة الدفع لكل فاتورة (تجميع)
    pm_rows = db.execute(
        _sel(SalePayment.sale_id, PaymentMethod.name_ar)
        .join(PaymentMethod, PaymentMethod.id == SalePayment.payment_method_id)
        .where(SalePayment.sale_id.in_([r[0] for r in rows]) if rows else False)
    ).all()
    pm_by_sale: dict[int, list[str]] = {}
    for sid, name in pm_rows:
        pm_by_sale.setdefault(int(sid), []).append(str(name))

    headers = [
        "نوع الحركة",
        "رقم الفاتورة",
        "التاريخ",
        "الصنف",
        "الكمية",
        "سعر الوحدة",
        "إجمالي السطر",
        "طرق الدفع",
    ]
    out_rows = []
    for sid, ca, name, qty, price, total in rows:
        out_rows.append(
            [
                "بيع",
                sid,
                ca,
                name,
                qty,
                price,
                total,
                " + ".join(pm_by_sale.get(int(sid), [])),
            ]
        )
    refund_rows = db.execute(
        _sel(
            SaleReturn.id,
            SaleReturn.created_at,
            Product.name_ar,
            SaleReturnLine.quantity,
            SaleReturnLine.unit_price,
            SaleReturnLine.line_total,
        )
        .join(SaleReturnLine, SaleReturnLine.sale_return_id == SaleReturn.id)
        .join(Product, Product.id == SaleReturnLine.product_id)
        .where(
            SaleReturn.status == SaleReturnStatus.POSTED,
            SaleReturn.created_at >= s,
            SaleReturn.created_at < e,
        )
        .order_by(SaleReturn.created_at, SaleReturn.id)
    ).all()
    for rid, ca, name, qty, price, total in refund_rows:
        out_rows.append(
            [
                "مرتجع",
                rid,
                ca,
                name,
                -qty,
                price,
                -total,
                "",
            ]
        )
    return csv_response(
        f"sales-detailed-{_period_tag(period, s, e)}", headers, out_rows
    )


@router.get("/export/purchases.csv")
def export_purchases_csv(
    db: DBSession,
    _: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)
    items = list_purchases(db, s, e, kind=PurchaseKind.INVENTORY)
    headers = [
        "رقم الفاتورة",
        "التاريخ",
        "المورّد",
        "أسلوب الدفع",
        "الإجمالي",
        "ملاحظة",
    ]
    rows = [
        [
            p.id,
            p.created_at,
            p.supplier or "",
            p.method.name_ar if p.method else "",
            p.amount,
            p.note or "",
        ]
        for p in items
    ]
    return csv_response(
        f"purchases-{_period_tag(period, s, e)}", headers, rows
    )


@router.get("/export/expenses.csv")
def export_expenses_csv(
    db: DBSession,
    _: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)
    items = list_purchases(db, s, e, kind=PurchaseKind.EXPENSE)
    headers = [
        "التاريخ",
        "التصنيف",
        "أسلوب الدفع",
        "المبلغ",
        "ملاحظة",
    ]
    rows = [
        [
            p.created_at,
            p.expense_category or "—",
            p.method.name_ar if p.method else "",
            p.amount,
            p.note or "",
        ]
        for p in items
    ]
    return csv_response(
        f"expenses-{_period_tag(period, s, e)}", headers, rows
    )


@router.get("/export/assets.csv")
def export_assets_csv(
    db: DBSession,
    _: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)
    items = list_purchases(db, s, e, kind=PurchaseKind.ASSET)
    headers = [
        "التاريخ",
        "المورّد",
        "أسلوب الدفع",
        "الإجمالي",
        "ملاحظة",
    ]
    rows = [
        [
            p.created_at,
            p.supplier or "",
            p.method.name_ar if p.method else "",
            p.amount,
            p.note or "",
        ]
        for p in items
    ]
    return csv_response(
        f"assets-{_period_tag(period, s, e)}", headers, rows
    )


@router.get("/export/profit-by-product.csv")
def export_profit_by_product_csv(
    db: DBSession,
    _: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)
    items = report_queries.profit_by_product(db, s, e, limit=10000)
    headers = [
        "الصنف",
        "الكمية المباعة",
        "الإيراد",
        "متوسط التكلفة/وحدة",
        "تكلفة المباع",
        "الربح الإجمالي",
    ]
    rows = [
        [
            r.name_ar,
            r.qty_sold,
            r.revenue,
            r.avg_cost,
            r.cogs,
            r.gross_profit,
        ]
        for r in items
    ]
    return csv_response(
        f"profit-by-product-{_period_tag(period, s, e)}", headers, rows
    )


@router.get("/export/inventory.csv")
def export_inventory_csv(
    db: DBSession,
    _: User = Depends(_perm),
    only_low: int = Query(0, ge=0, le=1),
    q: str = Query(""),
    w: str | None = Query(None),
):
    from modules.inventory.service import get_main_warehouse, get_warehouse

    warehouse_id = get_main_warehouse(db).id
    if w:
        try:
            wid = int(w)
            if get_warehouse(db, wid) is not None:
                warehouse_id = wid
        except ValueError:
            pass
    rows_data = report_queries.inventory_snapshot(
        db, search=q or None, only_low=bool(only_low), warehouse_id=warehouse_id
    )
    headers = [
        "الصنف",
        "الوحدة",
        "الرصيد",
        "حد التنبيه",
        "متوسط التكلفة",
        "سعر البيع",
        "حالة",
    ]
    rows = [
        [
            r.name_ar,
            r.unit,
            r.quantity,
            r.reorder_level,
            r.avg_cost,
            r.sell_price if r.sell_price is not None else "",
            "منخفض" if r.is_low else "—",
        ]
        for r in rows_data
    ]
    suffix = "low" if only_low else "all"
    return csv_response(f"inventory-{suffix}", headers, rows)


@router.get("/export/comprehensive.csv")
def export_comprehensive_csv(
    db: DBSession,
    _: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    """تقرير شامل في ملف واحد: كل المؤشرات."""
    period, s, e = _resolve_period(period, start, end)
    sales_sum = report_queries.sales_summary(db, s, e)
    sales_by_pm = report_queries.sales_by_payment_method(db, s, e)
    sales_cash_in, refunds_cash_out, net_collected = _payment_flow_totals(sales_by_pm)
    delivery_cash_out = delivery_fee_cash_out_total(db, start=s, end=e)
    cogs = report_queries.cogs_summary(db, s, e)
    inv_purch = report_queries.inventory_purchases_summary(db, s, e)
    expenses = report_queries.expenses_summary(db, s, e)
    assets = report_queries.assets_summary(db, s, e)
    consumables_period = consumable_assets_total_in_period(db, s, e)
    depreciation_period = total_depreciation_in_period(db, s, e)
    rec_monthly_total, rec_period_share, period_days = recurring_costs_in_period(
        db, s, e
    )
    gross_profit = (sales_sum.revenue - cogs).quantize(Decimal("0.001"))
    net_profit = (
        gross_profit
        - expenses.total
        - rec_period_share
        - consumables_period
        - depreciation_period
    ).quantize(Decimal("0.001"))
    cashflow = (
        net_collected - delivery_cash_out - inv_purch.total - expenses.total - assets.total
    ).quantize(Decimal("0.001"))
    inv_value = report_queries.inventory_value(db)

    headers = ["البند", "القيمة (د.ل)"]
    rows = [
        ["الفترة", f"{s.strftime('%Y-%m-%d')} → {e.strftime('%Y-%m-%d')}"],
        ["عدد أيام الفترة", f"{float(period_days):.1f}"],
        [],
        ["━━━━━━ الحسبة (1): الفرق المباشر ━━━━━━", ""],
        ["+ إجمالي المبيعات", sales_sum.gross_revenue],
        ["− المرتجعات", sales_sum.returns_total],
        ["= صافي المبيعات", sales_sum.net_revenue],
        ["− تكلفة البضاعة المباعة (COGS)", cogs],
        ["= إجمالي الربح / هامش المساهمة", gross_profit],
        [],
        ["━━━━━━ الحسبة (2): صافي الربح بعد كل المصاريف ━━━━━━", ""],
        ["+ إجمالي الربح من (1)", gross_profit],
        ["− المصاريف التشغيلية", expenses.total],
        [
            f"− رواتب وإيجار وثابتة (حصة الفترة من {rec_monthly_total} شهرياً)",
            rec_period_share,
        ],
        ["− مستلزمات استهلاكية", consumables_period],
        ["− إهلاك الأصول الثابتة (IAS 16)", depreciation_period],
        ["= صافي الربح/الخسارة", net_profit],
        [],
        ["━━━━━━ التدفق النقدي ━━━━━━", ""],
        ["التحصيل الداخل", sales_cash_in],
        ["رد المبالغ", refunds_cash_out],
        ["صافي التحصيل", net_collected],
        ["أجرة التوصيل الخارجة", delivery_cash_out],
        ["شراء بضاعة (مخزون)", inv_purch.total],
        ["مصاريف", expenses.total],
        ["شراء أصول/أدوات (نقد)", assets.total],
        ["= صافي التدفق النقدي", cashflow],
        [],
        ["━━━━━━ المخزون ━━━━━━", ""],
        ["قيمة المخزون الحالية", inv_value],
        [
            "أصناف منخفضة المخزون",
            sum(
                1
                for r in report_queries.inventory_snapshot(db, only_low=True)
            ),
        ],
    ]
    return csv_response(
        f"comprehensive-{_period_tag(period, s, e)}", headers, rows
    )
