from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from urllib.parse import quote
from sqlalchemy import select

from app.deps import DBSession, require_any_permission, require_permission
from app.datetime_local import format_local_dt
from app.jinja_env import templates
from modules.authz.capability import can_approve_treasury_handoff
from modules.authz.models import User
from modules.authz.permissions import (
    PAYMENTS_MANAGE,
    REPORTS_VIEW,
    TREASURY_HANDOFF_APPROVE,
)
from modules.authz.service import user_has_permission
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
from modules.reporting.exports import SheetSpec, csv_response, xlsx_response
from modules.reporting.profit_calc import calc_net_profit
from modules.customers.loyalty_shift_reports import loyalty_redeem_cost_in_period
from modules.reporting.unified_transactions import (
    KIND_OPTIONS,
    export_unified_rows,
    list_unified_transactions,
)

router = APIRouter(prefix="/reports", tags=["reports"])


def _wallet_csv_label(db, method) -> str:
    if method is None:
        return ""
    from modules.gl.wallet_labels import label_from_info_map, wallet_gl_info_map

    info = getattr(db, "_wallet_gl_info_csv", None)
    if info is None:
        info = wallet_gl_info_map(db)
        try:
            setattr(db, "_wallet_gl_info_csv", info)
        except Exception:
            pass
    return label_from_info_map(info, method)


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
        "start_str": start or format_local_dt(s, "%Y-%m-%d"),
        "end_str": end or (format_local_dt(e, "%Y-%m-%d") if e else ""),
    }


_perm = require_permission(REPORTS_VIEW)
_pay_manage = require_permission(PAYMENTS_MANAGE)
_handoff_approve = require_any_permission(TREASURY_HANDOFF_APPROVE, PAYMENTS_MANAGE)


def _payment_flow_totals(rows) -> tuple[Decimal, Decimal, Decimal]:
    from modules.reporting.queries import payment_flow_totals

    return payment_flow_totals(rows)


def payment_flow_totals(rows) -> tuple[Decimal, Decimal, Decimal]:
    return _payment_flow_totals(rows)


def _finance_domain_filter(request: Request, user: User):
    from modules.platform.business_domain import resolve_finance_domain

    return resolve_finance_domain(user, request.session)


@router.get("", response_class=HTMLResponse)
def reports_hub(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    from modules.authz.capability import is_treasury_clerk_user

    if is_treasury_clerk_user(user):
        return RedirectResponse("/pos/treasury/reports", 302)
    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)

    from modules.platform.business_domain import domain_label
    from modules.reporting.domain_financials import (
        build_profit_report_bundle,
        domain_sales_by_payment_method,
    )

    bundle = build_profit_report_bundle(db, s, e, domain=domain)
    sales_by_pm = domain_sales_by_payment_method(db, s, e, domain=domain)
    inv_value = report_queries.inventory_value(db)
    low_count = sum(1 for r in report_queries.inventory_snapshot(db, only_low=True))

    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(bundle)
    ctx.update(
        {
            "sales_by_pm": sales_by_pm,
            "inv_value": inv_value,
            "low_count": low_count,
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "الكل",
        }
    )
    return templates.TemplateResponse("reports_hub.html", ctx)


def _unified_export_query_params(
    period: str,
    start: str | None,
    end: str | None,
    kind: str,
    q: str,
) -> str:
    params: dict[str, str] = {"period": period, "kind": kind or "all"}
    if period == "custom":
        if start:
            params["start"] = start
        if end:
            params["end"] = end
    if q:
        params["q"] = q
    return urlencode(params)


@router.get("/transactions", response_class=HTMLResponse)
def reports_unified_transactions(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    kind: str = Query("all"),
    q: str = Query(""),
    page: int = Query(1, ge=1),
):
    """سجل موحّد: مبيعات + مقبوضات + مشتريات + مصروفات — الأحدث أولاً."""
    from modules.platform.business_domain import domain_label

    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    kind_f = (kind or "all").strip().lower()
    q_f = (q or "").strip()
    rows, summary, total, total_pages = list_unified_transactions(
        db,
        s,
        e,
        domain=domain,
        kind=kind_f,
        q=q_f,
        page=page,
    )
    export_qs = _unified_export_query_params(period, start, end, kind_f, q_f)
    from modules.platform.business_domain import is_system_admin

    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "rows": rows,
            "summary": summary,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "kind": kind_f,
            "kind_options": KIND_OPTIONS,
            "q": q_f,
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "الكل",
            "export_qs": export_qs,
            "can_edit_note": is_system_admin(user),
            "note_saved": request.query_params.get("saved") == "note",
            "note_error": request.query_params.get("note_err") or "",
        }
    )
    return templates.TemplateResponse("reports_transactions.html", ctx)


@router.get("/export/transactions.csv")
def export_unified_transactions_csv(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    kind: str = Query("all"),
    q: str = Query(""),
):
    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    rows = export_unified_rows(
        db, s, e, domain=domain, kind=kind, q=(q or "").strip()
    )
    headers = [
        "النوع",
        "التاريخ والوقت",
        "المرجع",
        "الاسم / الطرف",
        "الموظف",
        "المبلغ",
        "موجب/سالب",
        "طريقة الدفع",
        "البيان / الملاحظة",
    ]
    data = [
        [
            r.kind_label,
            format_local_dt(r.created_at, "%Y-%m-%d %H:%M:%S") if r.created_at else "",
            r.ref,
            r.party_name,
            r.employee_name,
            r.amount,
            r.signed_amount,
            r.method_name,
            r.note,
        ]
        for r in rows
    ]
    return csv_response("transactions", headers, data)


@router.get("/export/transactions.xlsx")
def export_unified_transactions_xlsx(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    kind: str = Query("all"),
    q: str = Query(""),
):
    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    rows = export_unified_rows(
        db, s, e, domain=domain, kind=kind, q=(q or "").strip()
    )
    headers = [
        "النوع",
        "التاريخ والوقت",
        "المرجع",
        "الاسم / الطرف",
        "الموظف",
        "المبلغ",
        "موجب/سالب",
        "طريقة الدفع",
        "البيان / الملاحظة",
    ]
    data = [
        [
            r.kind_label,
            format_local_dt(r.created_at, "%Y-%m-%d %H:%M:%S") if r.created_at else "",
            r.ref,
            r.party_name,
            r.employee_name,
            r.amount,
            r.signed_amount,
            r.method_name,
            r.note,
        ]
        for r in rows
    ]
    return xlsx_response(
        "transactions",
        [SheetSpec(name="العمليات", headers=headers, rows=data)],
    )


_TRANSFER_TYPE_AR = {
    "MANUAL": "تحويل بين الحسابات",
    "OWNER_DRAW": "سحب للمالك",
    "OWNER_CAPITAL": "إيداع رأس مال",
    "SHIFT_HANDOFF": "اعتماد جلسة",
    "REFUND_SETTLEMENT": "تسوية مرتجع",
    "SALE_PAYMENT_CORRECTION": "تصحيح دفعة",
}


def _safe_reports_next(next_url: str) -> str:
    path = (next_url or "").strip()
    if path.startswith("/reports/"):
        return path
    return "/reports/transactions"


@router.get("/transactions/transfer/{transfer_id}", response_class=HTMLResponse)
def reports_transfer_detail(
    request: Request,
    transfer_id: int,
    db: DBSession,
    user: User = Depends(_perm),
):
    from modules.gl.wallet_labels import label_from_info_map, wallet_gl_info_map
    from modules.payments.models import PaymentTransfer
    from modules.payments.treasury_service import _employee_name_for_user_id
    from modules.platform.business_domain import is_system_admin

    tf = db.get(PaymentTransfer, transfer_id)
    if tf is None:
        return RedirectResponse(
            "/reports/transactions?kind=transfer&note_err="
            + quote("التحويل غير موجود."),
            status_code=302,
        )
    gl_info = wallet_gl_info_map(db)
    from_name = label_from_info_map(gl_info, tf.from_method) if tf.from_method else "—"
    to_name = label_from_info_map(gl_info, tf.to_method) if tf.to_method else "—"
    tt = tf.transfer_type.value if getattr(tf.transfer_type, "value", None) else str(tf.transfer_type)
    return templates.TemplateResponse(
        "reports_transfer_detail.html",
        {
            "request": request,
            "tf": tf,
            "type_label": _TRANSFER_TYPE_AR.get(tt, tt),
            "from_name": from_name,
            "to_name": to_name,
            "employee_name": _employee_name_for_user_id(db, tf.created_by_id),
            "can_edit_note": is_system_admin(user),
            "note_saved": request.query_params.get("saved") == "note",
            "note_error": request.query_params.get("note_err") or "",
        },
    )


@router.post("/transactions/note", response_class=HTMLResponse)
def reports_transaction_note_save(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    source_kind: str = Form(...),
    source_id: str = Form(...),
    statement: str = Form(...),
    next: str = Form("/reports/transactions"),
):
    from modules.payments.service import PaymentsError
    from modules.payments.treasury_service import update_ledger_statement
    from modules.platform.business_domain import is_system_admin

    back = _safe_reports_next(next)
    sep = "&" if "?" in back else "?"
    if not is_system_admin(user):
        return RedirectResponse(
            back + sep + "note_err=" + quote("تحرير البيان للأدمن فقط."),
            status_code=302,
        )
    try:
        update_ledger_statement(
            db,
            source_kind=source_kind,
            source_id=int(source_id),
            statement=statement,
        )
        db.commit()
    except (PaymentsError, ValueError) as exc:
        db.rollback()
        return RedirectResponse(back + sep + "note_err=" + quote(str(exc)), status_code=302)
    return RedirectResponse(back + sep + "saved=note", status_code=302)


@router.get("/sales", response_class=HTMLResponse)
def reports_sales(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("day"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    from modules.platform.business_domain import BusinessDomain, domain_label

    if domain == BusinessDomain.HOTEL:
        from modules.hotel.revenue_stats import hotel_collections_by_payment_method
        from modules.reporting.domain_financials import build_domain_period_financials

        fin = build_domain_period_financials(db, s, e, domain=domain)
        sales_by_pm = hotel_collections_by_payment_method(db, s, e)
        sales_cash_in, refunds_cash_out, net_collected = _payment_flow_totals(sales_by_pm)
        shift_handoff = None
        handoff_pending_count = 0
        summary = fin.sales_sum
        top = []
        delivery_cash_out = fin.delivery_cash_out
        show_pos_sections = False
        revenue_source = "hotel"
    else:
        summary = report_queries.sales_summary(db, s, e)
        top = report_queries.top_products(db, s, e)
        sales_by_pm = report_queries.sales_by_payment_method(db, s, e)
        sales_cash_in, refunds_cash_out, net_collected = _payment_flow_totals(sales_by_pm)
        delivery_cash_out = delivery_fee_cash_out_total(db, start=s, end=e)
        from modules.payments.shift_handoff_service import (
            build_shift_handoff_panel,
            count_shifts_pending_handoff,
        )

        shift_handoff = build_shift_handoff_panel(db)
        handoff_pending_count = count_shifts_pending_handoff(db)
        show_pos_sections = True
        revenue_source = "pos"

    handoff_ok = request.query_params.get("handoff_ok")
    handoff_err = request.query_params.get("handoff_err")
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
            "shift_handoff": shift_handoff,
            "handoff_pending_count": handoff_pending_count,
            "can_approve_handoff": can_approve_treasury_handoff(user),
            "handoff_ok": handoff_ok,
            "handoff_err": handoff_err,
            "show_pos_sections": show_pos_sections,
            "revenue_source": revenue_source,
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else None,
        }
    )
    return templates.TemplateResponse("reports_sales.html", ctx)


@router.get("/product-invoices", response_class=HTMLResponse)
def reports_product_invoices(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    q: str | None = Query(None),
    product_id: str | None = Query(None),
):
    """بحث فواتير البيع التي ظهر فيها صنف معيّن — بالاسم مع فلترة زمنية."""
    from modules.catalog.models import Product
    from modules.platform.business_domain import BusinessDomain, domain_label

    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    search_q = (q or "").strip()
    pid: int | None = None
    if product_id and str(product_id).strip().isdigit():
        pid = int(str(product_id).strip())

    matched: list = []
    selected = None
    rows = []
    summary = report_queries.ProductInvoiceSummary(
        invoice_count=0,
        line_count=0,
        qty_total=Decimal("0"),
        amount_total=Decimal("0"),
    )
    need_pick = False
    notice = None

    if domain == BusinessDomain.HOTEL:
        notice = "تقرير فواتير الأصناف خاص بنقطة البيع (POS). بدّل نطاق العرض إلى المطعم/الكل."
    elif pid is not None:
        selected = db.get(Product, pid)
        if selected is None:
            notice = "الصنف غير موجود."
        else:
            matched = [selected]
            rows, summary = report_queries.product_sale_invoices(
                db, s, e, product_ids=[selected.id]
            )
    elif search_q:
        matched = report_queries.search_catalog_products(db, search_q)
        if not matched:
            notice = f"لا يوجد صنف يطابق «{search_q}»."
        elif len(matched) == 1:
            selected = matched[0]
            rows, summary = report_queries.product_sale_invoices(
                db, s, e, product_ids=[selected.id]
            )
        else:
            need_pick = True
            notice = f"وُجد {len(matched)} صنفاً — اختر صنفاً من القائمة لعرض فواتيره."
    else:
        notice = "ابحث باسم الصنف أو الباركود أو SKU، أو افتح الرابط من كرت الصنف."

    # معاملات إضافية لأزرار الفترة
    extra_parts: list[str] = []
    if search_q:
        extra_parts.append(f"&q={quote(search_q)}")
    if selected is not None:
        extra_parts.append(f"&product_id={selected.id}")
    elif pid is not None:
        extra_parts.append(f"&product_id={pid}")
    extra_params = "".join(extra_parts)

    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "search_q": search_q,
            "product_id": selected.id if selected is not None else pid,
            "selected_product": selected,
            "matched_products": matched,
            "need_pick": need_pick,
            "rows": rows,
            "summary": summary,
            "notice": notice,
            "extra_params": extra_params,
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else None,
        }
    )
    return templates.TemplateResponse("reports_product_invoices.html", ctx)


@router.get("/hotel-collections", response_class=HTMLResponse)
def reports_hotel_collections(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    from modules.hotel.revenue_stats import hotel_collections_detail
    from modules.platform.business_domain import domain_label, reports_show_hotel_sections

    if not reports_show_hotel_sections(user, request.session):
        return RedirectResponse("/reports/sales?period=month", status_code=302)

    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    rows, summary = hotel_collections_detail(db, s, e)
    from modules.hotel.revenue_stats import hotel_collections_by_payment_method

    sales_by_pm = hotel_collections_by_payment_method(db, s, e)
    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "rows": rows,
            "summary": summary,
            "sales_by_pm": sales_by_pm,
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "الكل",
        }
    )
    return templates.TemplateResponse("reports_hotel_collections.html", ctx)


@router.get("/hotel-bookings", response_class=HTMLResponse)
def reports_hotel_bookings(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    from modules.hotel.bookings_report import hotel_bookings_in_period
    from modules.platform.business_domain import domain_label, reports_show_hotel_sections

    if not reports_show_hotel_sections(user, request.session):
        return RedirectResponse("/reports/sales?period=month", status_code=302)

    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    rows, summary = hotel_bookings_in_period(db, s, e)
    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "rows": rows,
            "summary": summary,
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "الكل",
        }
    )
    return templates.TemplateResponse("reports_hotel_bookings.html", ctx)


@router.get("/hotel-balances", response_class=HTMLResponse)
def reports_hotel_balances(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
):
    from modules.hotel.booking_debts import open_debts_summary
    from modules.hotel.bookings_report import hotel_open_balances
    from modules.platform.business_domain import domain_label, reports_show_hotel_sections

    if not reports_show_hotel_sections(user, request.session):
        return RedirectResponse("/reports/receivables?balance_only=1", status_code=302)

    domain = _finance_domain_filter(request, user)
    rows, summary = hotel_open_balances(db)
    _, open_debts_total = open_debts_summary(db)
    from modules.hotel.booking_debts import list_open_debts

    open_debts = list_open_debts(db)
    ctx = {
        "request": request,
        "rows": rows,
        "summary": summary,
        "open_debts": open_debts,
        "open_debts_count": len(open_debts),
        "open_debts_total": open_debts_total,
        "finance_domain_filter": domain,
        "domain_label": domain_label(domain) if domain else "الكل",
        "as_of_date": format_local_dt(datetime.now(), "%Y-%m-%d"),
    }
    return templates.TemplateResponse("reports_hotel_balances.html", ctx)


@router.get("/hotel-daily-close", response_class=HTMLResponse)
def reports_hotel_daily_close(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    from modules.hotel.daily_close_report import hotel_daily_close_report
    from modules.hotel.finance_service import finance_enabled
    from modules.platform.business_domain import domain_label, reports_show_hotel_sections

    if not reports_show_hotel_sections(user, request.session):
        return RedirectResponse("/reports/sales?period=month", status_code=302)

    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    rows, summary = hotel_daily_close_report(db, s, e)
    hotel_gl_recon = None
    from modules.hotel.revenue_stats import hotel_cash_collected
    from modules.reporting.comprehensive_helpers import hotel_gl_reconciliation_for_period

    hotel_gl_recon = hotel_gl_reconciliation_for_period(
        db, s, e, operational_net=hotel_cash_collected(db, s, e)
    )
    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "rows": rows,
            "summary": summary,
            "finance_on": finance_enabled(db),
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "الكل",
            "hotel_gl_recon": hotel_gl_recon,
        }
    )
    return templates.TemplateResponse("reports_hotel_daily_close.html", ctx)


@router.get("/gl-reconciliation", response_class=HTMLResponse)
def reports_gl_reconciliation(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    from modules.platform.business_domain import domain_label
    from modules.reporting.gl_reconciliation_report import build_gl_reconciliation_report

    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    report = build_gl_reconciliation_report(db, s, e, domain=domain)
    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "report": report,
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "الكل",
        }
    )
    return templates.TemplateResponse("reports_gl_reconciliation.html", ctx)


@router.get("/revenue-matrix", response_class=HTMLResponse)
def reports_revenue_matrix(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("year"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    bucket: str = Query("month"),
    measure: str = Query("net"),
    segment: str = Query("all"),
):
    from modules.platform.business_domain import (
        BusinessDomain,
        domain_label,
        reports_show_hotel_sections,
        reports_show_pos_sections,
    )
    from modules.reporting.revenue_matrix import (
        GRANULARITY_LABELS,
        MEASURE_LABELS,
        build_revenue_matrix_report,
        resolve_segment_filter,
    )

    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    report = build_revenue_matrix_report(
        db,
        s,
        e,
        granularity=bucket,
        measure=measure,
        domain=domain,
        segment=segment,
    )
    show_restaurant, show_hotel = resolve_segment_filter(segment, domain)
    show_segment_filter = (
        domain is None
        and reports_show_hotel_sections(user, request.session)
        and reports_show_pos_sections(user, request.session)
    )
    ctx = _common_ctx(request, period, s, e, start, end)
    matrix_qs_parts = [
        f"period={period}",
        f"bucket={report.granularity}",
        f"measure={report.measure}",
    ]
    if segment and segment != "all":
        matrix_qs_parts.append(f"segment={segment}")
    if period == "custom" and start and end:
        matrix_qs_parts.append(f"start={start}")
        matrix_qs_parts.append(f"end={end}")
    matrix_qs = "&".join(matrix_qs_parts)
    matrix_filter_extra = (
        f"&bucket={report.granularity}&measure={report.measure}"
        + (f"&segment={segment}" if segment and segment != "all" else "")
    )
    ctx.update(
        {
            "report": report,
            "granularity_labels": GRANULARITY_LABELS,
            "measure_labels": MEASURE_LABELS,
            "segment": segment,
            "show_segment_filter": show_segment_filter,
            "matrix_qs": matrix_qs,
            "matrix_filter_extra": matrix_filter_extra,
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "الكل",
        }
    )
    return templates.TemplateResponse("reports_revenue_matrix.html", ctx)


@router.get("/pos-daily-close", response_class=HTMLResponse)
def reports_pos_daily_close(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    from modules.platform.business_domain import (
        BusinessDomain,
        domain_label,
        reports_show_hotel_sections,
        reports_show_pos_sections,
    )
    from modules.reporting.comprehensive_helpers import restaurant_gl_reconciliation_for_period
    from modules.reporting.domain_financials import build_domain_period_financials
    from modules.reporting.pos_daily_close_report import pos_daily_close_report

    if not reports_show_pos_sections(user, request.session):
        return RedirectResponse("/reports/hotel-daily-close?period=month", status_code=302)

    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    rows, summary = pos_daily_close_report(db, s, e)
    pos_fin = build_domain_period_financials(
        db, s, e, domain=BusinessDomain.RESTAURANT
    )
    restaurant_gl_recon = restaurant_gl_reconciliation_for_period(
        db, s, e, operational_net=pos_fin.net_collected
    )
    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "rows": rows,
            "summary": summary,
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "المطعم",
            "restaurant_gl_recon": restaurant_gl_recon,
            "show_hotel_hint": reports_show_hotel_sections(user, request.session),
        }
    )
    return templates.TemplateResponse("reports_pos_daily_close.html", ctx)


@router.post("/sales/shift-handoff", response_class=HTMLResponse)
def reports_sales_shift_handoff(
    request: Request,
    db: DBSession,
    user: User = Depends(_handoff_approve),
    shift_id: int = Form(...),
    period: str = Form("day"),
    start: str = Form(""),
    end: str = Form(""),
    handoff_cash: str = Form(""),
    handoff_bank: str = Form(""),
    handoff_note: str = Form(""),
):
    from modules.payments.shift_handoff_service import (
        ShiftHandoffError,
        approve_shift_handoff,
        parse_handoff_amount,
    )

    back = f"/reports/sales?period={quote(period)}"
    if period == "custom" and start:
        back += f"&start={quote(start)}&end={quote(end)}"
    try:
        approve_shift_handoff(
            db,
            shift_id=shift_id,
            user_id=user.id,
            admin_username=user.username,
            handoff_cash=parse_handoff_amount(handoff_cash),
            handoff_bank=parse_handoff_amount(handoff_bank),
            handoff_note=handoff_note,
        )
        db.commit()
        return RedirectResponse(back + "&handoff_ok=1", status_code=302)
    except ShiftHandoffError as exc:
        db.rollback()
        return RedirectResponse(
            back + "&handoff_err=" + quote(str(exc)),
            status_code=302,
        )
    except Exception:
        db.rollback()
        return RedirectResponse(
            back + "&handoff_err=" + quote("تعذّر تنفيذ الاعتماد والتحويل."),
            status_code=302,
        )


@router.get("/purchases", response_class=HTMLResponse)
def reports_purchases(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    summary = report_queries.inventory_purchases_summary(db, s, e, domain=domain)
    by_pm = report_queries.purchases_by_payment_method(
        db, s, e, kind=PurchaseKind.INVENTORY, domain=domain
    )
    items = list_purchases(db, s, e, kind=PurchaseKind.INVENTORY, domain=domain)
    pay_filter = (request.query_params.get("pay") or "all").lower()
    if pay_filter not in ("all", "paid", "partial", "unpaid"):
        pay_filter = "all"
    balance_only = request.query_params.get("balance_only") == "1"
    from modules.payables.service import build_payable_rows, payables_summary
    from modules.platform.business_domain import domain_label

    ap_summary = payables_summary(db, kind=PurchaseKind.INVENTORY, domain=domain)
    pay_rows = build_payable_rows(
        db,
        start=s,
        end=e,
        kind=PurchaseKind.INVENTORY,
        pay_filter=pay_filter,
        only_with_balance=balance_only,
        domain=domain,
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
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "الكل",
        }
    )
    return templates.TemplateResponse("reports_purchases.html", ctx)


@router.get("/expenses", response_class=HTMLResponse)
def reports_expenses(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    from modules.platform.business_domain import domain_label

    summary = report_queries.expenses_summary(db, s, e, domain=domain)
    by_pm = report_queries.purchases_by_payment_method(
        db, s, e, kind=PurchaseKind.EXPENSE, domain=domain
    )
    by_cat = report_queries.expenses_by_category(db, s, e, domain=domain)
    items = list_purchases(db, s, e, kind=PurchaseKind.EXPENSE, domain=domain)
    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "summary": summary,
            "by_pm": by_pm,
            "by_cat": by_cat,
            "items": items,
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "الكل",
        }
    )
    return templates.TemplateResponse("reports_expenses.html", ctx)


@router.get("/profit", response_class=HTMLResponse)
def reports_profit(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    from modules.platform.business_domain import domain_label
    from modules.reporting.domain_financials import build_profit_report_bundle

    bundle = build_profit_report_bundle(db, s, e, domain=domain)
    from modules.reporting.domain_financials import domain_sales_by_payment_method

    sales_by_pm = domain_sales_by_payment_method(db, s, e, domain=domain)
    by_product = (
        report_queries.profit_by_product(db, s, e)
        if bundle.get("show_product_profit")
        else []
    )

    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(bundle)
    ctx.update(
        {
            "sales_by_pm": sales_by_pm,
            "by_product": by_product,
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "الكل",
        }
    )
    return templates.TemplateResponse("reports_profit.html", ctx)


@router.get("/inventory", response_class=HTMLResponse)
def reports_inventory(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    q: str = Query(""),
    only_low: int = Query(0, ge=0, le=1),
    w: str | None = Query(None),
):
    from modules.platform.business_domain import reports_show_pos_sections

    if not reports_show_pos_sections(user, request.session):
        return RedirectResponse("/reports/purchases?period=month", status_code=302)

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
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    from modules.platform.business_domain import domain_label

    summary = report_queries.assets_summary(db, s, e, domain=domain)
    by_pm = report_queries.purchases_by_payment_method(
        db, s, e, kind=PurchaseKind.ASSET, domain=domain
    )
    items = list_purchases(db, s, e, kind=PurchaseKind.ASSET, domain=domain)
    consumables_total = consumable_assets_total_in_period(db, s, e, domain=domain)
    depreciation_total = total_depreciation_in_period(db, s, e)
    from modules.gl.role_maps import asset_roles_gl_summary

    gl_asset = asset_roles_gl_summary(db, s, e)
    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "summary": summary,
            "by_pm": by_pm,
            "items": items,
            "consumables_total": consumables_total,
            "depreciation_total": depreciation_total,
            "gl_asset": gl_asset,
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "الكل",
        }
    )
    return templates.TemplateResponse("reports_assets.html", ctx)


@router.get("/fixed-assets", response_class=HTMLResponse)
def reports_fixed_assets(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    """سجل الأصول الثابتة وحسابات الإهلاك (Fixed Assets Register)."""
    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    from modules.platform.business_domain import domain_label

    lines = list_fixed_asset_lines(db, domain=domain)
    snapshots = []
    period_depr_total = Decimal("0")
    for ln in lines:
        snap = snapshot_at(ln, e)
        from modules.payments.depreciation import depreciation_in_period

        period_depr = depreciation_in_period(ln, s, e)
        period_depr_total += period_depr
        snapshots.append({"snap": snap, "period_depr": period_depr})
    summary = fixed_assets_summary(db, e, domain=domain)
    consumables_total = consumable_assets_total_in_period(db, s, e, domain=domain)
    from modules.gl.role_maps import asset_roles_gl_summary

    gl_asset = asset_roles_gl_summary(db, s, e)
    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "snapshots": snapshots,
            "summary": summary,
            "period_depr_total": period_depr_total.quantize(Decimal("0.001")),
            "consumables_total": consumables_total,
            "gl_asset": gl_asset,
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "الكل",
        }
    )
    return templates.TemplateResponse("reports_fixed_assets.html", ctx)


@router.get("/comprehensive", response_class=HTMLResponse)
def reports_comprehensive(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)

    from modules.platform.business_domain import (
        BusinessDomain,
        domain_label,
        reports_show_hotel_sections,
        reports_show_pos_sections,
    )
    from modules.reporting.domain_financials import build_profit_report_bundle

    bundle = build_profit_report_bundle(db, s, e, domain=domain)
    from modules.reporting.domain_financials import domain_sales_by_payment_method

    sales_by_pm = domain_sales_by_payment_method(db, s, e, domain=domain)
    purch_by_pm = report_queries.purchases_by_payment_method(
        db, s, e, kind=PurchaseKind.INVENTORY, domain=domain
    )
    exp_by_pm = report_queries.purchases_by_payment_method(
        db, s, e, kind=PurchaseKind.EXPENSE, domain=domain
    )
    asset_by_pm = report_queries.purchases_by_payment_method(
        db, s, e, kind=PurchaseKind.ASSET, domain=domain
    )
    exp_by_cat = report_queries.expenses_by_category(db, s, e, domain=domain)
    show_pos = reports_show_pos_sections(user, request.session)
    show_hotel = reports_show_hotel_sections(user, request.session)
    top = (
        report_queries.top_products(db, s, e, limit=10)
        if bundle.get("show_product_profit")
        else []
    )
    profit_top = (
        report_queries.profit_by_product(db, s, e, limit=10)
        if bundle.get("show_product_profit")
        else []
    )
    inv_value = Decimal("0")
    low_rows: list = []
    wallet_rows: list = []
    if show_pos:
        inv_value = report_queries.inventory_value(db)
        low_rows = report_queries.inventory_snapshot(db, only_low=True)
        wallet_rows = wallet_breakdown(db, None, None)

    hotel_supplements: dict = {}
    restaurant_gl_recon = None
    if show_hotel and domain in (None, BusinessDomain.HOTEL):
        from modules.hotel.revenue_stats import hotel_cash_collected
        from modules.reporting.comprehensive_helpers import hotel_comprehensive_supplements

        hotel_op = (
            bundle["net_collected"]
            if domain == BusinessDomain.HOTEL
            else hotel_cash_collected(db, s, e)
        )
        hotel_supplements = hotel_comprehensive_supplements(
            db, s, e, operational_net=hotel_op
        )
    if show_pos and domain in (None, BusinessDomain.RESTAURANT):
        from modules.reporting.comprehensive_helpers import restaurant_gl_reconciliation_for_period
        from modules.reporting.domain_financials import build_domain_period_financials

        pos_net = bundle["net_collected"]
        if domain is None:
            pos_fin = build_domain_period_financials(
                db, s, e, domain=BusinessDomain.RESTAURANT
            )
            pos_net = pos_fin.net_collected
        restaurant_gl_recon = restaurant_gl_reconciliation_for_period(
            db, s, e, operational_net=pos_net
        )

    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(bundle)
    ctx.update(
        {
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
            "show_pos_sections": show_pos,
            "show_hotel_sections": show_hotel,
            "restaurant_gl_recon": restaurant_gl_recon,
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "الكل",
            **hotel_supplements,
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
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    user_id: str | None = Query(None),
    payment_method_id: str | None = Query(None),
    customer_id: str | None = Query(None),
):
    """تقرير مالي تشغيلي موثّق: استحقاق مبيعات + مرتجعات + تحصيل/ردود — دون مسودات ودون جدول قيود عام بعد."""
    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    from modules.platform.business_domain import BusinessDomain, domain_label, payment_method_visible_for_domain

    def _parse_opt_id(raw: str | None) -> int | None:
        if raw is None or str(raw).strip() == "":
            return None
        try:
            return int(str(raw).strip())
        except ValueError:
            return None

    uid = _parse_opt_id(user_id)
    pmid = _parse_opt_id(payment_method_id)
    cid = _parse_opt_id(customer_id) if domain != BusinessDomain.HOTEL else None
    filters = report_queries.ReportFilters(
        user_id=uid,
        payment_method_id=pmid,
        customer_id=cid,
    )
    stmt, statement_source = report_queries.financial_operational_statement_for_domain(
        db, s, e, filters, domain=domain
    )
    logical = report_queries.financial_operational_logical_lines(
        stmt, source=statement_source
    )
    users = list(db.scalars(select(User).order_by(User.username)).all())
    methods = [
        pm
        for pm in db.scalars(
            select(PaymentMethod).order_by(PaymentMethod.sort_order, PaymentMethod.id)
        ).all()
        if payment_method_visible_for_domain(pm.business_domain, filter_domain=domain)
    ]
    customers = (
        list(db.scalars(select(Customer).order_by(Customer.phone).limit(500)).all())
        if domain != BusinessDomain.HOTEL
        else []
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
            "statement_source": statement_source,
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "الكل",
        }
    )
    return templates.TemplateResponse("reports_financial_operational.html", ctx)


@router.get("/export/financial-operational.csv")
def export_financial_operational_csv(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    user_id: str | None = Query(None),
    payment_method_id: str | None = Query(None),
    customer_id: str | None = Query(None),
):
    from modules.platform.business_domain import BusinessDomain, domain_label

    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)

    def _parse_opt_id(raw: str | None) -> int | None:
        if raw is None or str(raw).strip() == "":
            return None
        try:
            return int(str(raw).strip())
        except ValueError:
            return None

    uid = _parse_opt_id(user_id)
    pmid = _parse_opt_id(payment_method_id)
    cid = _parse_opt_id(customer_id) if domain != BusinessDomain.HOTEL else None
    filters = report_queries.ReportFilters(
        user_id=uid,
        payment_method_id=pmid,
        customer_id=cid,
    )
    stmt, statement_source = report_queries.financial_operational_statement_for_domain(
        db, s, e, filters, domain=domain
    )
    logical = report_queries.financial_operational_logical_lines(
        stmt, source=statement_source
    )
    dom_tag = domain_label(domain) if domain else "الكل"
    count_label = (
        "عدد التحصيلات (فندق)"
        if statement_source == "hotel"
        else "عدد الفواتير المكتملة"
    )
    headers = ["البند", "الاتجاه التوضيحي", "المبلغ", "مصدر البيانات / الملاحظة"]
    rows: list[list] = [
        ["الفترة", "", f"{format_local_dt(s, '%Y-%m-%d')} → {format_local_dt(e, '%Y-%m-%d')}", ""],
        ["مجال التقرير", "", dom_tag, ""],
        ["مصدر البيانات", "", statement_source, ""],
        ["فلاتر المستخدم/طريقة الدفع/العميل", "", str(filters), ""],
        [],
        [count_label, "", stmt.invoice_count, ""],
        ["عدد سندات المرتجع / مرتجعات التحصيل", "", stmt.return_count, ""],
        ["إجمالي الإيراد التشغيلي", "", stmt.gross_revenue, ""],
        ["إجمالي المرتجعات", "", stmt.returns_total, ""],
        ["صافي الإيراد", "", stmt.net_revenue, ""],
        ["مجموع التحصيل", "", stmt.collections_total, ""],
        ["مجموع ردود المبالغ", "", stmt.refunds_payment_total, ""],
        ["صافي أثر نقدي تشغيلي", "", stmt.net_cash_effect, ""],
        ["ضرائب معروضة في التقرير", "", stmt.taxes_included, "غير مطبّقة"],
        ["خصومات سطرية معروضة", "", stmt.line_discounts_included, "غير مطبّقة"],
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
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    days: int = Query(30, ge=1, le=31),
):
    """تقرير تحليل التعادل (CVP / Break-Even Analysis):
    يحسب العبء اليومي ويُقارن إيرادات أيام الفترة بالعتبة، مع إبراز الأيام الرابحة والخاسرة.
    """
    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    burden = compute_daily_burden(db, operating_days_per_month=days, domain=domain)

    from datetime import timedelta as _td

    from modules.platform.business_domain import BusinessDomain

    daily_rows = []
    cur = s.replace(hour=0, minute=0, second=0, microsecond=0)
    profit_days = 0
    loss_days = 0
    total_above = Decimal("0")
    total_below = Decimal("0")
    while cur < e:
        nxt_day = (cur + _td(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        if domain == BusinessDomain.HOTEL:
            from modules.hotel.revenue_stats import hotel_day_financials

            rev, var, gross = hotel_day_financials(db, cur, nxt_day)
        elif domain == BusinessDomain.RESTAURANT:
            from modules.reporting.queries import cogs_summary, sales_summary

            ss = sales_summary(db, cur, nxt_day)
            var = cogs_summary(db, cur, nxt_day)
            rev = ss.revenue
            gross = (rev - var).quantize(Decimal("0.001"))
        else:
            from modules.hotel.revenue_stats import hotel_day_financials
            from modules.reporting.queries import cogs_summary, sales_summary

            ss = sales_summary(db, cur, nxt_day)
            pos_var = cogs_summary(db, cur, nxt_day)
            h_rev, h_var, _ = hotel_day_financials(db, cur, nxt_day)
            rev = (ss.revenue + h_rev).quantize(Decimal("0.001"))
            var = (pos_var + h_var).quantize(Decimal("0.001"))
            gross = (rev - var).quantize(Decimal("0.001"))
        net = (gross - burden.grand_daily).quantize(Decimal("0.001"))
        is_profit = net >= 0 and rev > 0
        if rev > 0:
            if is_profit:
                profit_days += 1
                total_above += net
            else:
                loss_days += 1
                total_below += -net
        daily_rows.append(
            {
                "date": format_local_dt(cur, "%Y-%m-%d"),
                "revenue": rev,
                "cogs": var,
                "gross": gross,
                "net": net,
                "is_profit": is_profit,
                "no_sales": rev == 0,
            }
        )
        cur = nxt_day

    daily_rows.reverse()
    from modules.platform.business_domain import domain_label

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
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "الكل",
            "hotel_break_even_note": False,
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
    return f"{period}-{format_local_dt(s, '%Y%m%d')}-to-{format_local_dt(e, '%Y%m%d')}"


@router.get("/export/sales.csv")
def export_sales_csv(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    """تصدير قائمة المبيعات / تحصيلات الفندق + الإجمالي."""
    from modules.platform.business_domain import BusinessDomain, domain_label
    from modules.reporting.domain_financials import (
        build_domain_period_financials,
        domain_sales_by_payment_method,
    )

    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    summary = build_domain_period_financials(db, s, e, domain=domain).sales_sum
    by_pm = domain_sales_by_payment_method(db, s, e, domain=domain)
    sales_cash_in, refunds_cash_out, net_collected = _payment_flow_totals(by_pm)
    delivery_cash_out = (
        Decimal("0")
        if domain == BusinessDomain.HOTEL
        else delivery_fee_cash_out_total(db, start=s, end=e)
    )
    dom_tag = domain_label(domain) if domain else "الكل"

    headers = ["البند", "القيمة"]
    rows: list[list] = [
        ["الفترة", f"{format_local_dt(s, '%Y-%m-%d')} → {format_local_dt(e, '%Y-%m-%d')}"],
        ["مجال التقرير", dom_tag],
    ]
    if domain == BusinessDomain.HOTEL:
        rows.extend(
            [
                ["عدد التحصيلات", summary.invoice_count],
                ["إيراد الإقامة (صافي)", summary.net_revenue],
                ["متوسط التحصيل", summary.avg_basket],
                ["التحصيل خلال الفترة", sales_cash_in],
                ["مرتجعات التحصيل", refunds_cash_out],
                ["صافي التحصيل", net_collected],
            ]
        )
    else:
        rows.extend(
            [
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
            ]
        )
    rows.extend(
        [
            [],
            ["تفصيل التحصيل والرد حسب طريقة الدفع", ""],
            ["طريقة الدفع", "عمليات التحصيل", "التحصيل", "عمليات الرد", "رد المبالغ", "الصافي"],
        ]
    )
    for name, sales_cnt, sales_total, refund_cnt, refund_total, net_total in by_pm:
        rows.append([name, sales_cnt, sales_total, refund_cnt, refund_total, net_total])
    return csv_response(f"sales-{_period_tag(period, s, e)}", headers, rows)


@router.get("/export/sales-detailed.csv")
def export_sales_detailed_csv(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    """تصدير كل بنود المبيعات (سطر لكل صنف في كل فاتورة) — POS فقط."""
    from modules.platform.business_domain import BusinessDomain

    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    if domain == BusinessDomain.HOTEL:
        headers = ["ملاحظة"]
        rows = [["تصدير بنود POS غير متاح في وضع الفندق — استخدم تقرير المبيعات أو تحصيلات الحجز."]]
        return csv_response(f"sales-detailed-{_period_tag(period, s, e)}", headers, rows)

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


@router.get("/export/product-invoices.csv")
def export_product_invoices_csv(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    q: str | None = Query(None),
    product_id: str | None = Query(None),
):
    from modules.platform.business_domain import BusinessDomain

    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    headers = [
        "رقم الفاتورة",
        "التاريخ",
        "الصنف",
        "الكمية",
        "سعر الوحدة",
        "إجمالي السطر",
        "إجمالي الفاتورة",
        "المصدر",
        "السياق",
        "العميل",
        "رابط الفاتورة",
    ]
    if domain == BusinessDomain.HOTEL:
        return csv_response(
            f"product-invoices-{_period_tag(period, s, e)}",
            headers,
            [["—", "غير متاح في وضع الفندق", "", "", "", "", "", "", "", "", ""]],
        )

    search_q = (q or "").strip()
    pid: int | None = None
    if product_id and str(product_id).strip().isdigit():
        pid = int(str(product_id).strip())

    product_ids: list[int] = []
    if pid is not None:
        product_ids = [pid]
    elif search_q:
        matched = report_queries.search_catalog_products(db, search_q)
        if len(matched) == 1:
            product_ids = [matched[0].id]
        elif len(matched) > 1:
            return csv_response(
                f"product-invoices-{_period_tag(period, s, e)}",
                headers,
                [
                    [
                        "—",
                        f"نتائج متعددة ({len(matched)}) — حدّد product_id",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                    ]
                ],
            )

    rows, _summary = report_queries.product_sale_invoices(
        db, s, e, product_ids=product_ids
    )
    out = []
    for r in rows:
        out.append(
            [
                r.sale_id,
                format_local_dt(r.sale_at, "%Y-%m-%d %H:%M"),
                r.product_name,
                r.quantity,
                r.unit_price,
                r.line_total,
                r.sale_total,
                r.source,
                r.context_type,
                r.customer_name or "",
                f"/pos/receipt/{r.sale_id}",
            ]
        )
    tag = _period_tag(period, s, e)
    name_bit = f"p{product_ids[0]}" if len(product_ids) == 1 else "search"
    return csv_response(f"product-invoices-{name_bit}-{tag}", headers, out)


@router.get("/export/hotel-collections.csv")
def export_hotel_collections_csv(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    from modules.hotel.revenue_stats import hotel_collections_detail
    from modules.platform.business_domain import domain_label, reports_show_hotel_sections

    period, s, e = _resolve_period(period, start, end)
    if not reports_show_hotel_sections(user, request.session):
        return csv_response(
            f"hotel-collections-{_period_tag(period, s, e)}",
            ["ملاحظة"],
            [["تصدير تحصيلات الفندق غير متاح في وضع المطعم."]],
        )

    domain = _finance_domain_filter(request, user)
    detail_rows, summary = hotel_collections_detail(db, s, e)
    dom_tag = domain_label(domain) if domain else "الكل"
    headers = [
        "نوع الحركة",
        "رقم الحركة",
        "مرجع الحجز",
        "رقم الحجز",
        "النزيل",
        "التاريخ",
        "المبلغ",
        "طريقة الدفع",
        "عربون",
        "المستلم",
        "ملاحظة",
    ]
    rows: list[list] = [
        ["الفترة", f"{format_local_dt(s, '%Y-%m-%d')} → {format_local_dt(e, '%Y-%m-%d')}"],
        ["مجال التقرير", dom_tag],
        ["عدد التحصيلات", summary.payment_count],
        ["عدد المرتجعات", summary.refund_count],
        ["إجمالي التحصيل", summary.collected_total],
        ["إجمالي المرتجعات", summary.refunded_total],
        ["صافي التحصيل", summary.net_total],
        [],
    ]
    for r in detail_rows:
        rows.append(
            [
                r.movement_type,
                r.movement_id,
                r.booking_ref,
                r.booking_id,
                r.guest_name,
                r.created_at,
                r.amount,
                r.payment_method,
                "نعم" if r.is_deposit else "لا",
                r.received_by,
                r.note,
            ]
        )
    return csv_response(
        f"hotel-collections-{_period_tag(period, s, e)}", headers, rows
    )


@router.get("/export/hotel-bookings.csv")
def export_hotel_bookings_csv(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    from modules.hotel.bookings_report import hotel_bookings_in_period
    from modules.platform.business_domain import domain_label, reports_show_hotel_sections

    period, s, e = _resolve_period(period, start, end)
    if not reports_show_hotel_sections(user, request.session):
        return csv_response(
            f"hotel-bookings-{_period_tag(period, s, e)}",
            ["ملاحظة"],
            [["تصدير حجوزات الفندق غير متاح في وضع المطعم."]],
        )

    domain = _finance_domain_filter(request, user)
    detail_rows, summary = hotel_bookings_in_period(db, s, e)
    dom_tag = domain_label(domain) if domain else "الكل"
    headers = [
        "الشقة",
        "مرجع الحجز",
        "رقم الحجز",
        "النزيل",
        "دخول",
        "خروج",
        "ليالي",
        "حالة الحجز",
        "حالة الدفع",
        "القيمة المالية",
        "استحقاق الإقامة",
        "المدفوع",
        "المتبقي",
    ]
    rows: list[list] = [
        ["الفترة", f"{format_local_dt(s, '%Y-%m-%d')} → {format_local_dt(e, '%Y-%m-%d')}"],
        ["مجال التقرير", dom_tag],
        ["شقق مؤجّرة", summary.room_count],
        ["عدد الحجوزات", summary.booking_count],
        ["إجمالي الليالي", summary.nights_total],
        ["القيمة المالية", summary.folio_total],
        ["إجمالي استحقاق الإقامة", summary.accommodation_total],
        ["إجمالي المدفوع", summary.paid_total],
        ["إجمالي المتبقي", summary.balance_total],
        ["مسكّن حالياً", summary.checked_in_count],
        ["مغادر", summary.checked_out_count],
        [],
    ]
    for r in detail_rows:
        rows.append(
            [
                r.room_label or "",
                r.reference,
                r.booking_id,
                r.guest_name,
                r.check_in,
                r.check_out,
                r.nights,
                r.booking_status_label,
                r.payment_status_label,
                r.folio_total,
                r.accommodation_total,
                r.paid_amount,
                r.balance,
            ]
        )
    return csv_response(
        f"hotel-bookings-{_period_tag(period, s, e)}", headers, rows
    )


@router.get("/export/hotel-balances.csv")
def export_hotel_balances_csv(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
):
    from modules.hotel.bookings_report import hotel_open_balances
    from modules.platform.business_domain import domain_label, reports_show_hotel_sections

    if not reports_show_hotel_sections(user, request.session):
        return csv_response(
            "hotel-balances",
            ["ملاحظة"],
            [["تصدير ذمم الحجز غير متاح في وضع المطعم."]],
        )

    domain = _finance_domain_filter(request, user)
    detail_rows, summary = hotel_open_balances(db)
    dom_tag = domain_label(domain) if domain else "الكل"
    as_of = format_local_dt(datetime.now(), "%Y-%m-%d")
    headers = [
        "مرجع الحجز",
        "رقم الحجز",
        "النزيل",
        "دخول",
        "خروج",
        "حالة الحجز",
        "حالة الدفع",
        "استحقاق الإقامة",
        "المدفوع",
        "الرصيد",
    ]
    rows: list[list] = [
        ["تاريخ التقرير", as_of],
        ["مجال التقرير", dom_tag],
        ["عدد الحجوزات ذات الرصيد", summary.booking_count],
        ["إجمالي الرصيد", summary.balance_total],
        ["مسكّن — عدد", summary.checked_in_count],
        ["مسكّن — رصيد", summary.checked_in_balance],
        ["متأخر — عدد", summary.overdue_count],
        ["متأخر — رصيد", summary.overdue_balance],
        [],
    ]
    for r in detail_rows:
        rows.append(
            [
                r.reference,
                r.booking_id,
                r.guest_name,
                r.check_in,
                r.check_out,
                r.booking_status_label,
                r.payment_status_label,
                r.accommodation_total,
                r.paid_amount,
                r.balance,
            ]
        )
    return csv_response(f"hotel-balances-{as_of}", headers, rows)


@router.get("/export/hotel-daily-close.csv")
def export_hotel_daily_close_csv(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    from modules.hotel.daily_close_report import hotel_daily_close_report
    from modules.platform.business_domain import domain_label, reports_show_hotel_sections

    period, s, e = _resolve_period(period, start, end)
    if not reports_show_hotel_sections(user, request.session):
        return csv_response(
            f"hotel-daily-close-{_period_tag(period, s, e)}",
            ["ملاحظة"],
            [["تصدير الإقفال اليومي غير متاح في وضع المطعم."]],
        )

    domain = _finance_domain_filter(request, user)
    detail_rows, summary = hotel_daily_close_report(db, s, e)
    dom_tag = domain_label(domain) if domain else "الكل"
    from modules.hotel.revenue_stats import hotel_cash_collected
    from modules.reporting.comprehensive_helpers import hotel_gl_reconciliation_for_period

    gl = hotel_gl_reconciliation_for_period(
        db, s, e, operational_net=hotel_cash_collected(db, s, e)
    )
    rows: list[list] = [
        ["الفترة", f"{format_local_dt(s, '%Y-%m-%d')} → {format_local_dt(e, '%Y-%m-%d')}"],
        ["مجال التقرير", dom_tag],
        ["عدد الأيام", summary.day_count],
        ["أيام مُقفلة", summary.closed_days],
        ["أيام غير مُقفلة", summary.open_days],
        ["إجمالي التحصيل", summary.total_revenue],
        ["إجمالي العربونات", summary.total_deposits],
        ["متوسط الإشغال %", summary.avg_occupancy],
    ]
    if gl is not None:
        rows.extend(
            [
                ["أيام بفارق GL", summary.days_with_gl_mismatch],
                ["مجموع فوارق GL", summary.gl_gap_total],
                [],
                ["━━━━━━ مطابقة GL 4150 (الفترة) ━━━━━━", ""],
                ["تحصيل تشغيلي", gl.operational_net],
                ["صافي دائن GL", gl.gl_revenue_net],
                ["الفارق", gl.gap],
            ]
        )
    else:
        rows.append([])
    headers = [
        "التاريخ",
        "مُقفَل",
        "حجوزات جديدة",
        "تسكين",
        "مغادرة",
        "إلغاء",
        "لم يحضر",
        "صافي التحصيل",
        "GL 4150",
        "فارق GL",
        "عربونات",
        "إشغال %",
        "غرف مشغولة",
        "إجمالي الغرف",
        "أُقفِل بواسطة",
        "ملاحظات",
    ]
    for r in detail_rows:
        rows.append(
            [
                r.day,
                "نعم" if r.is_closed else "لا",
                r.bookings_new,
                r.check_ins,
                r.check_outs,
                r.cancellations,
                r.no_shows,
                r.revenue_net,
                r.gl_revenue_net if r.gl_revenue_net is not None else "",
                r.gl_gap if r.gl_gap is not None else "",
                r.deposits_total,
                r.occupancy_rate,
                r.occupied,
                r.total_rooms,
                r.closed_by or "",
                r.notes or "",
            ]
        )
    return csv_response(
        f"hotel-daily-close-{_period_tag(period, s, e)}", headers, rows
    )


@router.get("/export/gl-reconciliation.csv")
def export_gl_reconciliation_csv(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    from modules.platform.business_domain import domain_label
    from modules.reporting.gl_reconciliation_report import (
        build_gl_reconciliation_report,
        gl_reconciliation_csv_rows,
    )

    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    dom_tag = domain_label(domain) if domain else "الكل"
    report = build_gl_reconciliation_report(db, s, e, domain=domain)
    rows = gl_reconciliation_csv_rows(report, dom_tag, s, e)
    headers = ["البند", "القيمة"]
    return csv_response(
        f"gl-reconciliation-{_period_tag(period, s, e)}", headers, rows
    )


@router.get("/export/revenue-matrix.csv")
def export_revenue_matrix_csv(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("year"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    bucket: str = Query("month"),
    measure: str = Query("net"),
    segment: str = Query("all"),
):
    from modules.platform.business_domain import domain_label
    from modules.reporting.revenue_matrix import build_revenue_matrix_report, revenue_matrix_csv_rows

    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    report = build_revenue_matrix_report(
        db,
        s,
        e,
        granularity=bucket,
        measure=measure,
        domain=domain,
        segment=segment,
    )
    headers, rows = revenue_matrix_csv_rows(report)
    dom_tag = domain_label(domain) if domain else "الكل"
    meta_rows = [
        ["الفترة", f"{format_local_dt(s, '%Y-%m-%d')} → {format_local_dt(e, '%Y-%m-%d')}"],
        ["التجميع", report.granularity_label],
        ["المقياس", report.measure_label],
        ["المجال", dom_tag],
        [],
    ]
    return csv_response(
        f"revenue-matrix-{_period_tag(period, s, e)}",
        headers,
        meta_rows + rows,
    )


@router.get("/export/pos-daily-close.csv")
def export_pos_daily_close_csv(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    from modules.platform.business_domain import domain_label, reports_show_pos_sections
    from modules.reporting.comprehensive_helpers import restaurant_gl_reconciliation_for_period
    from modules.reporting.domain_financials import build_domain_period_financials
    from modules.platform.business_domain import BusinessDomain
    from modules.reporting.pos_daily_close_report import pos_daily_close_report

    period, s, e = _resolve_period(period, start, end)
    if not reports_show_pos_sections(user, request.session):
        return csv_response(
            f"pos-daily-close-{_period_tag(period, s, e)}",
            ["ملاحظة"],
            [["تصدير إقفال المطعم غير متاح في وضع الفندق."]],
        )

    domain = _finance_domain_filter(request, user)
    dom_tag = domain_label(domain) if domain else "المطعم"
    detail_rows, summary = pos_daily_close_report(db, s, e)
    pos_fin = build_domain_period_financials(
        db, s, e, domain=BusinessDomain.RESTAURANT
    )
    gl = restaurant_gl_reconciliation_for_period(
        db, s, e, operational_net=pos_fin.net_collected
    )
    rows: list[list] = [
        ["الفترة", f"{format_local_dt(s, '%Y-%m-%d')} → {format_local_dt(e, '%Y-%m-%d')}"],
        ["مجال التقرير", dom_tag],
        ["عدد الأيام", summary.day_count],
        ["إجمالي التحصيل", summary.total_revenue],
        ["فواتير مكتملة", summary.total_sales],
        ["جلسات مفتوحة", summary.total_shifts_opened],
        ["جلسات مُغلقة", summary.total_shifts_closed],
        ["أيام بفارق GL", summary.days_with_gl_mismatch],
    ]
    if gl is not None:
        rows.extend(
            [
                [],
                ["—— مطابقة GL 4100 ——", ""],
                ["صافي تحصيل POS", gl.operational_net],
                ["صافي دائن GL", gl.gl_revenue_net],
                ["الفارق", gl.gap],
            ]
        )
    rows.extend([[], ["—— تفصيل يومي ——", ""]])
    headers = [
        "التاريخ",
        "تحصيل",
        "فواتير",
        "جلسات فُتحت",
        "جلسات أُغلقت",
        "GL 4100",
        "فارق",
    ]
    rows.append(headers)
    for r in detail_rows:
        rows.append(
            [
                r.day,
                r.revenue_net,
                r.sales_count,
                r.shifts_opened,
                r.shifts_closed,
                r.gl_revenue_net if r.gl_revenue_net is not None else "",
                r.gl_gap if r.gl_gap is not None else "",
            ]
        )
    return csv_response(
        f"pos-daily-close-{_period_tag(period, s, e)}", headers, rows
    )


@router.get("/export/purchases.csv")
def export_purchases_csv(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    items = list_purchases(db, s, e, kind=PurchaseKind.INVENTORY, domain=domain)
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
            _wallet_csv_label(db, p.method),
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
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    items = list_purchases(db, s, e, kind=PurchaseKind.EXPENSE, domain=domain)
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
            _wallet_csv_label(db, p.method),
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
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    items = list_purchases(db, s, e, kind=PurchaseKind.ASSET, domain=domain)
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
            _wallet_csv_label(db, p.method),
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
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    from modules.platform.business_domain import BusinessDomain
    from modules.reporting.domain_financials import build_domain_period_financials

    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    fin = build_domain_period_financials(db, s, e, domain=domain)
    headers = [
        "الصنف",
        "الكمية المباعة",
        "الإيراد",
        "متوسط التكلفة/وحدة",
        "تكلفة المباع",
        "الربح الإجمالي",
    ]
    if domain == BusinessDomain.HOTEL or not fin.show_product_profit:
        return csv_response(
            f"profit-by-product-{_period_tag(period, s, e)}",
            headers,
            [],
        )

    items = report_queries.profit_by_product(db, s, e, limit=10000)
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
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    only_low: int = Query(0, ge=0, le=1),
    q: str = Query(""),
    w: str | None = Query(None),
):
    from modules.platform.business_domain import reports_show_pos_sections

    if not reports_show_pos_sections(user, request.session):
        return csv_response(
            "inventory-blocked",
            ["ملاحظة"],
            [["تصدير المخزون غير متاح في وضع الفندق."]],
        )

    headers, rows, suffix = _inventory_report_export_table(
        db, only_low=bool(only_low), q=q, w=w
    )
    return csv_response(f"inventory-{suffix}", headers, rows)


@router.get("/export/inventory.xlsx")
def export_inventory_xlsx(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    only_low: int = Query(0, ge=0, le=1),
    q: str = Query(""),
    w: str | None = Query(None),
):
    from modules.platform.business_domain import reports_show_pos_sections
    from modules.reporting.exports import SheetSpec, xlsx_response

    if not reports_show_pos_sections(user, request.session):
        return xlsx_response(
            "inventory-blocked",
            [SheetSpec(name="ملاحظة", headers=["البند"], rows=[["تصدير المخزون غير متاح في وضع الفندق."]])],
        )

    headers, rows, suffix = _inventory_report_export_table(
        db, only_low=bool(only_low), q=q, w=w
    )
    return xlsx_response(
        f"inventory-{suffix}",
        [SheetSpec(name="المخزون", headers=headers, rows=rows)],
    )


def _inventory_report_export_table(
    db: DBSession,
    *,
    only_low: bool,
    q: str,
    w: str | None,
) -> tuple[list[str], list[list], str]:
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
        db, search=q or None, only_low=only_low, warehouse_id=warehouse_id
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
    return headers, rows, suffix


@router.get("/export/comprehensive.csv")
def export_comprehensive_csv(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Query("month"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    """تقرير شامل في ملف واحد: كل المؤشرات."""
    from modules.reporting.comprehensive_helpers import build_comprehensive_csv_rows
    from modules.reporting.domain_financials import build_profit_report_bundle

    period, s, e = _resolve_period(period, start, end)
    domain = _finance_domain_filter(request, user)
    bundle = build_profit_report_bundle(db, s, e, domain=domain)
    dom_tag = domain_label(domain) if domain else "الكل"
    headers = ["البند", "القيمة (د.ل)"]
    rows = build_comprehensive_csv_rows(db, s, e, domain, bundle)
    tag = _period_tag(period, s, e)
    if domain:
        tag = f"{domain.value}-{tag}"
    return csv_response(f"comprehensive-{tag}", headers, rows)


@router.get("/export/shifts.csv")
def reports_shifts_export_csv(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    status: str = Query("all"),
):
    from modules.platform.business_domain import reports_show_pos_sections

    if not reports_show_pos_sections(user, request.session):
        return csv_response(
            "pos-shifts-blocked",
            ["ملاحظة"],
            [["تصدير جلسات الكاشير غير متاح في وضع الفندق."]],
        )

    from modules.pos_shifts.shift_reports import list_shifts_index, shift_index_csv_rows

    status_f = (status or "all").lower()
    if status_f not in ("all", "open", "closed"):
        status_f = "all"
    rows, _ = list_shifts_index(db, status_filter=status_f, limit=5000, offset=0)
    headers = [
        "رقم الجلسة",
        "الحالة",
        "الكاشير",
        "المستخدم",
        "فتح",
        "إغلاق",
        "عدد الفواتير",
        "إجمالي المبيعات",
        "كاش متوقع",
        "مصرف متوقع",
        "كاش معدود",
        "مصرف معدود",
        "فرق كاش",
        "فرق مصرف",
        "عجز",
        "فائض",
        "اعتماد الخزينة",
    ]
    return csv_response(
        f"pos-shifts-{status_f}",
        headers,
        shift_index_csv_rows(rows),
    )
