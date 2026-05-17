"""تقرير الذمم المدينة — فواتير حسب حالة السداد."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import REPORTS_VIEW
from modules.receivables.service import (
    InvoicePayStatus,
    build_receivable_rows,
    customer_debt_groups,
    receivables_summary,
)

router = APIRouter(prefix="/reports", tags=["receivables"])
_perm = require_permission(REPORTS_VIEW)


@router.get("/receivables", response_class=HTMLResponse)
def receivables_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    pay: str = Query("all"),
    context: str = Query("all"),
    balance_only: int = Query(0, ge=0, le=1),
):
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

    status_labels = {
        InvoicePayStatus.PAID: "مدفوعة",
        InvoicePayStatus.PARTIAL: "مدفوعة جزئياً",
        InvoicePayStatus.UNPAID: "غير مدفوعة",
    }

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
            "status_labels": status_labels,
            "context_options": [
                ("all", "كل السياقات"),
                ("TABLE", "طاولات"),
                ("ROOM", "غرف فندق"),
                ("EXTERNAL", "طلبات خارجية"),
            ],
        },
    )
