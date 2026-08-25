from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import SALES_REFUND, SALES_REFUND_OVERRIDE, SALES_EDIT_INVOICE
from modules.authz.service import user_has_permission
from modules.payments.service import list_payment_methods
from modules.refunds.service import (
    RefundsError,
    create_sale_return,
    get_refundable_sale_summary,
    get_sale_return,
    list_recent_sale_returns,
    sale_remaining_total,
    search_completed_sales,
)

router = APIRouter(prefix="/refunds", tags=["refunds"])
_refund_perm = require_permission(SALES_REFUND)
log = logging.getLogger("refunds.web")


def _parse_decimal(raw: str | None) -> Decimal:
    try:
        return Decimal(str(raw or "").strip() or "0")
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _sale_error_redirect(sale_id: int, message: object) -> RedirectResponse:
    return RedirectResponse(
        f"/refunds/sale/{sale_id}?error={quote(str(message))}",
        status_code=302,
    )


@router.get("", response_class=HTMLResponse)
def refunds_index(
    request: Request,
    db: DBSession,
    user: User = Depends(_refund_perm),
    q: str = Query(""),
):
    sales = search_completed_sales(db, q=q, limit=100)
    rows = [
        {
            "sale": sale,
            "net_total": sale_remaining_total(db, sale.id),
        }
        for sale in sales
    ]
    recent_returns = list_recent_sale_returns(db, limit=40)
    return templates.TemplateResponse(
        "refunds_index.html",
        {
            "request": request,
            "rows": rows,
            "recent_returns": recent_returns,
            "q": q,
            "user": user,
            "error": request.query_params.get("error"),
            "can_edit_invoice": user_has_permission(user, SALES_EDIT_INVOICE),
        },
    )


@router.post("/sale/{sale_id}/otp-request", response_class=HTMLResponse)
def refunds_otp_request(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(_refund_perm),
):
    from modules.platform.business_domain import BusinessDomain
    from modules.security.supervisor_otp import (
        PURPOSE_POS_REFUND,
        SupervisorOtpError,
        request_refund_otp,
    )

    try:
        request_refund_otp(
            db,
            purpose=PURPOSE_POS_REFUND,
            domain=BusinessDomain.RESTAURANT,
            ref_type="sale",
            ref_id=sale_id,
            ref_label=f"فاتورة مطعم #{sale_id}",
            requested_by_user_id=getattr(user, "id", None),
        )
    except SupervisorOtpError as exc:
        return _sale_error_redirect(sale_id, exc)
    return RedirectResponse(
        f"/refunds/sale/{sale_id}?saved=otp_sent",
        status_code=302,
    )


@router.get("/sale/{sale_id}", response_class=HTMLResponse)
def refunds_sale_detail(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(_refund_perm),
):
    try:
        summary = get_refundable_sale_summary(db, sale_id)
    except RefundsError as exc:
        return RedirectResponse(f"/refunds?error={quote(str(exc))}", status_code=302)
    except Exception:
        log.exception("refund sale detail failed for sale_id=%s", sale_id)
        return RedirectResponse(
            "/refunds?error="
            + quote("تعذّر فتح صفحة الاسترداد لهذه الفاتورة. راجع سجل الأخطاء."),
            status_code=302,
        )
    methods = list_payment_methods(db, only_active=True)
    can_override = user_has_permission(user, SALES_REFUND_OVERRIDE)
    can_edit_invoice = user_has_permission(user, SALES_EDIT_INVOICE)
    from modules.security.supervisor_otp import PURPOSE_POS_REFUND, otp_purpose_required

    pos_refund_otp_required = otp_purpose_required(db, PURPOSE_POS_REFUND)
    return templates.TemplateResponse(
        "refunds_sale_detail.html",
        {
            "request": request,
            "summary": summary,
            "methods": methods,
            "can_override": can_override,
            "can_edit_invoice": can_edit_invoice,
            "pos_refund_otp_required": pos_refund_otp_required,
            "saved_return_id": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/sale/{sale_id}", response_class=HTMLResponse)
async def refunds_create(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(_refund_perm),
):
    form = await request.form()
    from modules.platform.business_domain import BusinessDomain, is_system_admin
    from modules.security.supervisor_otp import (
        PURPOSE_POS_REFUND,
        SupervisorOtpError,
        pos_refund_session_ok,
        verify_refund_otp,
        grant_pos_refund_session,
    )

    if not is_system_admin(user) and not pos_refund_session_ok(request.session, sale_id):
        from modules.security.supervisor_otp import otp_purpose_required

        if otp_purpose_required(db, PURPOSE_POS_REFUND):
            otp_code = str(form.get("supervisor_otp") or "").strip()
            if not otp_code:
                return _sale_error_redirect(
                    sale_id,
                    "أدخل رمز اعتماد مشرف المطعم (OTP واتساب) أو اطلبه أولاً من شاشة الكاشير.",
                )
            try:
                verify_refund_otp(
                    db,
                    purpose=PURPOSE_POS_REFUND,
                    domain=BusinessDomain.RESTAURANT,
                    ref_type="sale",
                    ref_id=sale_id,
                    code=otp_code,
                )
                grant_pos_refund_session(request.session, sale_id)
            except SupervisorOtpError as exc:
                return _sale_error_redirect(sale_id, exc)

    items: list[tuple[int, Decimal]] = []
    line_restock: dict[int, bool] = {}
    for key, value in form.items():
        sk = str(key)
        if sk.startswith("skip_restock_"):
            tail = sk[len("skip_restock_") :]
            if tail.isdigit() and str(value).strip().lower() in ("1", "on", "true", "yes"):
                line_restock[int(tail)] = False
            continue
        if not sk.startswith("qty_"):
            continue
        sale_line_id = sk.replace("qty_", "", 1)
        if not sale_line_id.isdigit():
            continue
        qty = _parse_decimal(str(value))
        if qty > 0:
            items.append((int(sale_line_id), qty))

    try:
        refund_method_raw = str(form.get("refund_payment_method_id") or "").strip()
        refund_method_id = int(refund_method_raw) if refund_method_raw else None
    except ValueError:
        return _sale_error_redirect(sale_id, "أسلوب رد المبلغ غير صالح.")

    try:
        sale_return = create_sale_return(
            db,
            sale_id=sale_id,
            lines=items,
            created_by_id=user.id,
            reason=str(form.get("reason") or "").strip() or None,
            note=str(form.get("note") or "").strip() or None,
            refund_payment_method_id=refund_method_id,
            allow_payment_override=user_has_permission(user, SALES_REFUND_OVERRIDE),
            approved_by_id=user.id
            if user_has_permission(user, SALES_REFUND_OVERRIDE)
            else None,
            line_restock=line_restock or None,
        )
        db.commit()
    except RefundsError as exc:
        db.rollback()
        return _sale_error_redirect(sale_id, exc)
    except Exception:
        db.rollback()
        log.exception("refund create failed for sale_id=%s", sale_id)
        return _sale_error_redirect(
            sale_id,
            "تعذّر تسجيل الاسترداد بسبب خطأ غير متوقع. لم يتم حفظ العملية، حاول مرة أخرى أو راجع سجل الأخطاء.",
        )

    return RedirectResponse(
        f"/refunds/sale/{sale_id}?saved={sale_return.id}",
        status_code=302,
    )


@router.get("/receipt/{sale_return_id}", response_class=HTMLResponse)
def refund_receipt(
    request: Request,
    sale_return_id: int,
    db: DBSession,
    user: User = Depends(_refund_perm),
    autoprint: int = Query(0, ge=0, le=1),
):
    sale_return = get_sale_return(db, sale_return_id)
    if sale_return is None:
        return RedirectResponse("/refunds?error=سند الاسترداد غير موجود.", status_code=302)
    return templates.TemplateResponse(
        "refund_receipt.html",
        {
            "request": request,
            "sale_return": sale_return,
            "autoprint": autoprint,
            "can_override": user_has_permission(user, SALES_REFUND_OVERRIDE),
        },
    )
