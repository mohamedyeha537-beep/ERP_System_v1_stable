"""تقرير الذمم المدينة — ديون العملاء والتحصيل."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import REPORTS_VIEW
from modules.payments.models import PaymentMethod
from modules.receivables.service import (
    InvoicePayStatus,
    ReceivablesError,
    build_receivable_rows,
    collect_sale_receivable,
    customer_debt_groups,
    debtor_title_from_key,
    get_receivable_invoice_detail,
    list_receive_methods_for_collection,
    parse_collection_amount,
    receivables_summary,
)

router = APIRouter(prefix="/reports", tags=["receivables"])
_perm = require_permission(REPORTS_VIEW)

_status_LABELS = {
    InvoicePayStatus.PAID: "مسدّد بالكامل",
    InvoicePayStatus.PARTIAL: "مسدّد جزئياً",
    InvoicePayStatus.UNPAID: "غير مدفوعة",
}


def _redirect_unless_pos_reports(request: Request, user: User):
    from modules.platform.business_domain import reports_show_pos_sections

    if not reports_show_pos_sections(user, request.session):
        return RedirectResponse("/reports/hotel-balances", status_code=302)
    return None


@router.get("/receivables", response_class=HTMLResponse)
def receivables_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    pay: str = Query("all"),
    context: str = Query("all"),
    balance_only: int = Query(1, ge=0, le=1),
):
    from modules.platform.business_domain import reports_show_pos_sections

    if not reports_show_pos_sections(user, request.session):
        return RedirectResponse("/reports/hotel-balances", status_code=302)

    pay_filter = (pay or "all").lower()
    if pay_filter not in ("all", "paid", "partial", "unpaid"):
        pay_filter = "all"
    ctx_filter = (context or "all").lower()
    only_bal = balance_only == 1

    summary = receivables_summary(db)
    rows = build_receivable_rows(
        db,
        pay_filter=pay_filter,
        context_filter=ctx_filter,
        only_with_balance=only_bal,
    )
    by_customer = customer_debt_groups(rows)

    return templates.TemplateResponse(
        "reports_receivables.html",
        {
            "request": request,
            "summary": summary,
            "rows": rows,
            "by_customer": by_customer,
            "pay_filter": pay_filter,
            "context_filter": ctx_filter,
            "balance_only": only_bal,
            "status_labels": _STATUS_LABELS,
            "context_options": [
                ("all", "كل السياقات"),
                ("TABLE", "طاولات"),
                ("ROOM", "غرف فندق"),
                ("EXTERNAL", "طلبات خارجية"),
            ],
        },
    )


@router.get("/receivables/debtor", response_class=HTMLResponse)
def receivables_debtor_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    key: str = Query(...),
    saved: str | None = Query(None),
):
    blocked = _redirect_unless_pos_reports(request, user)
    if blocked is not None:
        return blocked

    debt_key = (key or "").strip()
    if not debt_key:
        return RedirectResponse("/reports/receivables", status_code=302)
    try:
        title = debtor_title_from_key(db, debt_key)
    except ReceivablesError:
        return RedirectResponse(
            "/reports/receivables?err=" + quote("الزبون غير موجود."),
            status_code=302,
        )
    rows = build_receivable_rows(
        db, only_with_balance=True, debt_key_filter=debt_key
    )
    group_rows = customer_debt_groups(rows)
    group = group_rows[0] if group_rows else None
    return templates.TemplateResponse(
        "reports_receivables_debtor.html",
        {
            "request": request,
            "debt_key": debt_key,
            "party_title": title,
            "group": group,
            "rows": rows,
            "status_labels": _STATUS_LABELS,
            "saved": saved,
        },
    )


@router.get("/receivables/invoice/{sale_id}", response_class=HTMLResponse)
def receivables_invoice_page(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(_perm),
    saved: str | None = Query(None),
    error: str | None = Query(None),
):
    blocked = _redirect_unless_pos_reports(request, user)
    if blocked is not None:
        return blocked

    detail = get_receivable_invoice_detail(db, sale_id)
    if detail is None:
        return RedirectResponse(
            "/reports/receivables?err=" + quote("الفاتورة غير موجودة أو مسدّدة."),
            status_code=302,
        )
    methods = list_receive_methods_for_collection(db)
    pm_ids = {int(p.payment_method_id) for p in detail.payments}
    pm_ids.update(int(m.id) for m in methods)
    pm_names = {}
    if pm_ids:
        for pm in db.scalars(
            select(PaymentMethod).where(PaymentMethod.id.in_(pm_ids))
        ).all():
            pm_names[int(pm.id)] = pm.name_ar
    return templates.TemplateResponse(
        "reports_receivables_invoice.html",
        {
            "request": request,
            "detail": detail,
            "row": detail.row,
            "sale": detail.sale,
            "payments": detail.payments,
            "room_charge": detail.room_charge,
            "pay_methods": methods,
            "pm_names": pm_names,
            "status_labels": _STATUS_LABELS,
            "saved": saved,
            "error": error,
        },
    )


@router.post("/receivables/invoice/{sale_id}/collect", response_class=HTMLResponse)
async def receivables_collect_payment(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(_perm),
):
    blocked = _redirect_unless_pos_reports(request, user)
    if blocked is not None:
        return blocked

    form = await request.form()
    try:
        pm_id = int((form.get("payment_method_id") or "").strip())
        amount = parse_collection_amount(str(form.get("amount") or ""))
    except (ValueError, ReceivablesError) as exc:
        return RedirectResponse(
            f"/reports/receivables/invoice/{sale_id}?error={quote(str(exc))}",
            status_code=302,
        )
    detail_before = get_receivable_invoice_detail(db, sale_id)
    debt_key = detail_before.row.debt_key if detail_before else ""
    try:
        collect_sale_receivable(
            db,
            sale_id=sale_id,
            payment_method_id=pm_id,
            amount=amount,
            user_id=user.id,
            note=(form.get("note") or "").strip() or None,
        )
        db.commit()
    except ReceivablesError as exc:
        db.rollback()
        return RedirectResponse(
            f"/reports/receivables/invoice/{sale_id}?error={quote(str(exc))}",
            status_code=302,
        )
    detail = get_receivable_invoice_detail(db, sale_id)
    if detail is None or detail.row.outstanding <= 0:
        return RedirectResponse(
            f"/reports/receivables/debtor?key={quote(debt_key)}&saved=1",
            status_code=302,
        )
    return RedirectResponse(
        f"/reports/receivables/invoice/{sale_id}?saved=1",
        status_code=302,
    )
