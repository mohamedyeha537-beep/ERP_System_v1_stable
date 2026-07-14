"""تقرير الذمم الدائنة — ديون الموردين."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import REPORTS_VIEW
from modules.payables.service import (
    PurchasePayStatus,
    build_payable_rows,
    payables_summary,
    supplier_debt_groups,
)
from modules.payments.models import PurchaseKind

router = APIRouter(prefix="/reports", tags=["payables"])
_perm = require_permission(REPORTS_VIEW)


@router.get("/payables", response_class=HTMLResponse)
def payables_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    pay: str = Query("all"),
    kind: str = Query("inventory"),
    balance_only: int = Query(0, ge=0, le=1),
):
    from modules.platform.business_domain import domain_label, resolve_finance_domain

    domain = resolve_finance_domain(user, request.session)
    pay_filter = (pay or "all").lower()
    if pay_filter not in ("all", "paid", "partial", "unpaid"):
        pay_filter = "all"
    kind_map = {
        "inventory": PurchaseKind.INVENTORY,
        "expense": PurchaseKind.EXPENSE,
        "asset": PurchaseKind.ASSET,
        "all": None,
    }
    pk = kind_map.get((kind or "inventory").lower(), PurchaseKind.INVENTORY)
    only_bal = balance_only == 1

    summary = payables_summary(db, kind=pk, domain=domain)
    rows = build_payable_rows(
        db,
        kind=pk,
        pay_filter=pay_filter,
        only_with_balance=only_bal,
        domain=domain,
    )
    by_supplier = supplier_debt_groups(rows)

    return templates.TemplateResponse(
        "reports_payables.html",
        {
            "request": request,
            "summary": summary,
            "rows": rows,
            "by_supplier": by_supplier,
            "pay_filter": pay_filter,
            "kind_filter": kind,
            "balance_only": only_bal,
            "domain_label": domain_label(domain) if domain else "الكل",
            "status_labels": {
                PurchasePayStatus.PAID: "مدفوعة",
                PurchasePayStatus.PARTIAL: "مدفوعة جزئياً",
                PurchasePayStatus.UNPAID: "غير مدفوعة",
            },
        },
    )
