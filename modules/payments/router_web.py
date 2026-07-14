from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.deps import DBSession, require_any_permission, require_permission
from app.datetime_local import local_day_start_utc, local_period_bounds, now_local
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import (
    PAYMENTS_MANAGE,
    PURCHASE_INVOICES_MANAGE,
    PURCHASES_MANAGE,
    SALES_CREATE,
)
from modules.catalog.models import Product
from modules.catalog.service import list_stockable_products
from modules.catalog.uploads import (
    save_purchase_bank_payment_receipt,
    save_purchase_invoice_image,
    save_sale_payment_proof,
)
from modules.inventory.service import InsufficientStock
from modules.payments.asset_queries import list_asset_purchases_in_period
from modules.payments.models import (
    OWNER_EQUITY_PM_NAME,
    PaymentMethod,
    PaymentMethodKind,
    Purchase,
    PurchaseKind,
    PurchasePayment,
)
from modules.payments.service import (
    PaymentsError,
    create_payment_method,
    delete_payment_method,
    payment_method_allow_delete,
    delete_purchase,
    ensure_supplier_credit_payment_method,
    is_supplier_credit_payment_method,
    list_payment_methods,
    list_payment_methods_for_pay,
    list_payment_methods_for_purchase_term,
    list_payment_methods_for_purchase_term_custody,
    list_payment_methods_main_treasury_for_pay,
    list_payment_methods_purchase_custody_for_pay,
    assert_purchase_custody_payment_method,
    assert_purchase_term_payment_method,
    purchase_user_limited_to_custody,
    list_payment_methods_for_receive,
    list_purchases,
    purchase_outstanding,
    record_asset_purchase,
    record_consumable_purchase,
    record_expense,
    record_inventory_purchase,
    record_purchase_payment,
    record_sale_payment,
    sum_purchase_payments,
    apply_payment_method_icon,
    update_payment_method,
)
from modules.sales.models import Sale
from modules.sales.receipt_layout import sort_sale_lines_for_display
from modules.sales.service import (
    SalesError,
    complete_sale,
    create_draft_sale,
    get_draft_sale,
    load_sale_with_lines,
    table_assignment_required,
)
from modules.pos_shifts.service import require_open_pos_shift

# ============================================================
# Admin: Payment methods
# ============================================================
router = APIRouter(prefix="/admin/payment-methods", tags=["payments-admin"])
_admin_pm = require_permission(PAYMENTS_MANAGE)

_KIND_LABELS = {
    PaymentMethodKind.CASH: "كاش",
    PaymentMethodKind.BANK: "مصرف",
    PaymentMethodKind.OTHER: "أخرى",
}


def _static_root() -> Path:
    return Path(__file__).resolve().parents[2] / "app" / "static"


@router.get("", response_class=HTMLResponse)
def admin_payment_methods(
    request: Request,
    db: DBSession,
    user: User = Depends(_admin_pm),
):
    from modules.gl.service import is_gl_enabled

    domain = _finance_domain_filter(request, user)
    methods = list_payment_methods(db, only_active=False, domain=domain)
    gl_enabled = is_gl_enabled(db)
    return templates.TemplateResponse(
        "admin_payment_methods.html",
        {
            "request": request,
            "methods": methods,
            "kinds": [(k.value, lbl) for k, lbl in _KIND_LABELS.items()],
            "domain_choices": _domain_choices(),
            "finance_domain_filter": domain,
            "payment_method_allow_delete": payment_method_allow_delete,
            "owner_equity_name": OWNER_EQUITY_PM_NAME,
            "error_message": None,
            "gl_enabled": gl_enabled,
        },
    )


def _parse_kind(raw: str) -> PaymentMethodKind:
    try:
        return PaymentMethodKind(raw)
    except ValueError:
        return PaymentMethodKind.BANK


def _parse_business_domain(raw: str) -> "PaymentMethodDomain":
    from modules.payments.models import PaymentMethodDomain

    try:
        return PaymentMethodDomain((raw or "shared").strip().lower())
    except ValueError:
        return PaymentMethodDomain.SHARED


def _finance_domain_filter(request: Request, user: User):
    from modules.platform.business_domain import resolve_finance_domain

    return resolve_finance_domain(user, request.session)


def _domain_choices():
    from modules.payments.models import PaymentMethodDomain
    from modules.platform.business_domain import domain_label

    return [
        (d.value, domain_label(d.value))
        for d in PaymentMethodDomain
    ]


def _render_pm_page(request: Request, db, user: User, error: str, status_code: int = 400):
    from modules.gl.service import is_gl_enabled

    domain = _finance_domain_filter(request, user)
    methods = list_payment_methods(db, only_active=False, domain=domain)
    return templates.TemplateResponse(
        "admin_payment_methods.html",
        {
            "request": request,
            "methods": methods,
            "kinds": [(k.value, lbl) for k, lbl in _KIND_LABELS.items()],
            "domain_choices": _domain_choices(),
            "finance_domain_filter": domain,
            "payment_method_allow_delete": payment_method_allow_delete,
            "owner_equity_name": OWNER_EQUITY_PM_NAME,
            "error_message": error,
            "gl_enabled": is_gl_enabled(db),
        },
        status_code=status_code,
    )


@router.post("/add", response_class=HTMLResponse)
async def admin_payment_methods_add(
    request: Request,
    db: DBSession,
    user: User = Depends(_admin_pm),
    name_ar: str = Form(...),
    kind: str = Form("BANK"),
    sort_order: str = Form("0"),
    business_domain: str = Form("shared"),
    can_receive: str = Form(""),
    can_pay: str = Form(""),
    can_fund: str = Form(""),
    show_on_dashboard: str = Form(""),
    icon_file: UploadFile | None = File(None),
):
    from modules.gl.service import is_gl_enabled

    if is_gl_enabled(db):
        return _render_pm_page(
            request,
            db,
            user,
            "تم إيقاف إنشاء محافظ جديدة — أنشئ الحساب من شجرة الحسابات (GL) واربطه بجلسة البيع.",
            status_code=403,
        )
    try:
        pk = _parse_kind(kind)
        pm = create_payment_method(
            db,
            name_ar,
            pk,
            int(sort_order.strip() or "0"),
            can_receive=(can_receive == "on"),
            can_pay=(can_pay == "on"),
            can_fund=(can_fund == "on"),
            show_on_dashboard=(
                (show_on_dashboard == "on")
                if pk in (PaymentMethodKind.CASH, PaymentMethodKind.BANK)
                else False
            ),
            business_domain=_parse_business_domain(business_domain),
        )
        if icon_file is not None and icon_file.filename:
            apply_payment_method_icon(
                db, pm, _static_root(), upload=icon_file
            )
        db.commit()
    except (PaymentsError, ValueError) as e:
        db.rollback()
        return _render_pm_page(request, db, user, str(e))
    return RedirectResponse("/admin/payment-methods", status_code=302)


@router.post("/{pm_id}/update", response_class=HTMLResponse)
async def admin_payment_methods_update(
    request: Request,
    pm_id: int,
    db: DBSession,
    user: User = Depends(_admin_pm),
    name_ar: str = Form(...),
    kind: str = Form("BANK"),
    sort_order: str = Form("0"),
    business_domain: str = Form("shared"),
    is_active: str = Form(""),
    can_receive: str = Form(""),
    can_pay: str = Form(""),
    can_fund: str = Form(""),
    show_on_dashboard: str = Form(""),
    icon_file: UploadFile | None = File(None),
    clear_icon: str = Form(""),
):
    try:
        existing = db.get(PaymentMethod, pm_id)
        upd: dict = dict(
            name_ar=name_ar,
            kind=_parse_kind(kind),
            sort_order=int(sort_order.strip() or "0"),
            is_active=(is_active == "on"),
            can_receive=(can_receive == "on"),
            can_pay=(can_pay == "on"),
            can_fund=(can_fund == "on"),
        )
        if (
            existing is not None
            and existing.kind in (PaymentMethodKind.CASH, PaymentMethodKind.BANK)
            and not existing.is_system
        ):
            upd["show_on_dashboard"] = show_on_dashboard == "on"
        upd["business_domain"] = _parse_business_domain(business_domain)
        pm = update_payment_method(db, pm_id, **upd)
        apply_payment_method_icon(
            db,
            pm,
            _static_root(),
            upload=icon_file,
            clear=(clear_icon == "on"),
        )
        db.commit()
    except (PaymentsError, ValueError) as e:
        db.rollback()
        return _render_pm_page(request, db, user, str(e))
    return RedirectResponse("/admin/payment-methods", status_code=302)


@router.post("/{pm_id}/delete", response_class=HTMLResponse)
def admin_payment_methods_delete(
    request: Request,
    pm_id: int,
    db: DBSession,
    user: User = Depends(_admin_pm),
):
    try:
        delete_payment_method(db, pm_id)
        db.commit()
    except PaymentsError as e:
        db.rollback()
        return _render_pm_page(request, db, user, str(e))
    return RedirectResponse("/admin/payment-methods", status_code=302)


# ============================================================
# POS: checkout (payment selection at sale time)
# ============================================================
checkout_router = APIRouter(prefix="/pos", tags=["pos-checkout"])
_PAYMENTS_WEB_STATIC = Path(__file__).resolve().parents[2] / "app" / "static"


def _unlink_static_relative(static_root: Path, relative: str | None) -> None:
    rel = (relative or "").strip()
    if not rel:
        return
    fp = static_root.joinpath(*rel.split("/"))
    if fp.is_file():
        try:
            fp.unlink()
        except OSError:
            pass
_pos_perm = require_permission(SALES_CREATE)


def _session_pos_shift_id_checkout(request: Request) -> int | None:
    raw = request.session.get("pos_shift_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _get_draft_for_checkout(request: Request, db, user: User) -> Sale | None:
    raw = request.session.get("draft_sale_id")
    if raw is None:
        return None
    try:
        return get_draft_sale(db, int(raw))
    except (TypeError, ValueError):
        return None


def _ensure_sale_referral_from_online(db, sale: Sale) -> None:
    """يربط إحالة المتجر/الشات بالفاتورة قبل شاشة الدفع إن وُجدت في الجلسة."""
    if sale.referrer_customer_id:
        return
    from modules.messaging.chat_order_service import (
        is_online_guest_sale,
        try_apply_session_referral_to_sale,
        web_chat_session_for_sale,
    )

    if not is_online_guest_sale(sale):
        return
    sess = web_chat_session_for_sale(db, sale.id)
    if sess is None:
        return
    try_apply_session_referral_to_sale(db, sess, sale)


def _sale_referral_checkout_info(db, sale: Sale) -> dict:
    from modules.customers.models import Customer
    from modules.customers.referral_service import find_customer_by_referral_code
    from modules.messaging.chat_order_service import load_order_data, web_chat_session_for_sale

    code = (sale.referral_code_used or "").strip()
    referrer_id = sale.referrer_customer_id
    if not code:
        sess = web_chat_session_for_sale(db, sale.id)
        if sess is not None:
            code = (load_order_data(sess).get("referral_code") or "").strip()
    referrer = db.get(Customer, int(referrer_id)) if referrer_id else None
    if referrer is None and code:
        referrer = find_customer_by_referral_code(db, code)
    label = ""
    if referrer is not None:
        parts = [(referrer.name or "").strip(), (referrer.phone or "").strip()]
        label = " — ".join(p for p in parts if p)
    return {
        "referral_code_prefill": code,
        "referral_referrer_label": label,
        "referral_on_sale": bool(referrer_id),
        "referral_from_online": bool(code),
    }


def _checkout_loyalty_context(db, sale: Sale, user: User) -> dict:
    """سياق العميل ونقاط الولاء لصفحة الدفع."""
    from modules.authz.permissions import LOYALTY_REDEEM, SALES_CREATE
    from modules.authz.service import user_has_permission
    from modules.customers.service import (
        get_customer,
        loyalty_redeem_quote,
        loyalty_settings,
        points_to_dinars,
    )

    ctx = sale.context_type.value if sale.context_type else "TABLE"
    customer = None
    customer_points_value = None
    loyalty_quote = None
    loyalty = loyalty_settings(db)
    can_redeem_loyalty = loyalty["enabled"] and (
        user_has_permission(user, LOYALTY_REDEEM)
        or user_has_permission(user, SALES_CREATE)
    )
    if sale.customer_id:
        customer = get_customer(db, sale.customer_id)
        if customer and Decimal(str(customer.points_balance or 0)) > 0:
            customer_points_value = points_to_dinars(db, customer.points_balance)
        if customer and loyalty["enabled"]:
            loyalty_quote = loyalty_redeem_quote(
                db, customer=customer, sale_total=sale.total
            )
    referral_blocked = False
    messaging_profile = None
    if customer is not None:
        from modules.customers.referral_service import buyer_can_use_referral
        from modules.messaging.consent import get_profile

        referral_blocked = not buyer_can_use_referral(
            db, customer, exclude_sale_id=sale.id
        )
        messaging_profile = get_profile(db, customer.id)
    return {
        "ctx": ctx,
        "customer": customer,
        "customer_points_value": customer_points_value,
        "loyalty": loyalty,
        "loyalty_quote": loyalty_quote,
        "can_redeem_loyalty": can_redeem_loyalty,
        "referral_blocked": referral_blocked,
        "messaging_profile": messaging_profile,
        **_sale_referral_checkout_info(db, sale),
    }


def _checkout_financial_preview(loyalty_ctx: dict, sale: Sale) -> dict:
    loyalty = loyalty_ctx.get("loyalty") or {}
    quote = loyalty_ctx.get("loyalty_quote") or {}
    can_redeem = bool(quote.get("can_redeem") and loyalty_ctx.get("can_redeem_loyalty"))
    invoice_total = Decimal(str(sale.total or 0)).quantize(Decimal("0.001"))
    redeem_discount = (
        Decimal(str(quote.get("max_discount") or 0)).quantize(Decimal("0.001"))
        if can_redeem
        else Decimal("0")
    )
    redeem_points = (
        Decimal(str(quote.get("max_points") or 0)).quantize(Decimal("0.001"))
        if can_redeem
        else Decimal("0")
    )
    amount_due = (invoice_total - redeem_discount).quantize(Decimal("0.001"))
    if amount_due < 0:
        amount_due = Decimal("0")
    can_earn = bool(loyalty.get("enabled") and loyalty_ctx.get("customer"))
    earn_rate = Decimal(str(loyalty.get("earn_per_dinar") or 0)) if can_earn else Decimal("0")
    earned_without_redeem = (invoice_total * earn_rate).quantize(Decimal("0.001"))
    earned_with_redeem = (amount_due * earn_rate).quantize(Decimal("0.001"))
    return {
        "invoice_total": invoice_total,
        "redeem_discount": redeem_discount,
        "redeem_points": redeem_points,
        "amount_due": amount_due,
        "earned_without_redeem": earned_without_redeem,
        "earned_with_redeem": earned_with_redeem,
        "can_redeem": can_redeem,
    }


def _bind_checkout_customer(
    db,
    sale: Sale,
    *,
    phone: str,
    name: str,
) -> None:
    """يربط الطلب بعميل معروف عند إدخال الهاتف (كما في طلب التوصيل)."""
    from modules.customers.service import attach_customer_to_sale, normalize_phone

    if not normalize_phone(phone):
        sale.customer_id = None
        db.flush()
        return
    attach_customer_to_sale(db, sale, phone=phone, name=name or None)


def _checkout_gate_message(db, sale) -> str | None:
    """رسالة منع الدفع — None إذا مسموح."""
    from modules.sales.models import ExternalOrderType, SaleContext
    from modules.sales.order_policy import load_order_policy, payment_block_reason
    from modules.sales.service import table_assignment_required

    if not sale.lines:
        return "الطلب الحالي فارغ. أضف أصنافاً أو اختر طلباً آخر."
    if sale.context_type == SaleContext.EXTERNAL and not sale.customer_id:
        return "رقم هاتف العميل مطلوب للطلب الخارجي قبل الدفع."
    if (
        sale.context_type == SaleContext.EXTERNAL
        and sale.external_order_type == ExternalOrderType.DELIVERY
        and not sale.delivery_zone_id
    ):
        return "اختر منطقة التوصيل قبل إتمام البيع."
    if sale.context_type == SaleContext.ROOM:
        return (
            "طلب الشقة يُكمَل من زر «قيد على حساب الشقة وطباعة» في الفاتورة، "
            "وليس من شاشة الدفع."
        )
    if table_assignment_required(sale):
        return "اختر الطاولة أولاً قبل إتمام البيع، أو بدّل الطلب إلى «خارجي»."
    policy = load_order_policy(db)
    return payment_block_reason(db, sale, policy)


def _build_checkout_template_ctx(
    request: Request,
    db,
    user: User,
    sale,
    *,
    error: str | None = None,
    ok: str | None = None,
    checkout_modal: bool = False,
    open_pos_shift=None,
) -> dict:
    from modules.delivery.service import get_default_cash_method
    from modules.delivery.drivers_service import get_handoff, list_recent_drivers
    from modules.hr.meal_allowance import checkout_employee_options, current_period_label
    from modules.payments.service import list_pos_sale_payment_methods
    from modules.sales.order_pipeline import sale_is_delivery

    methods = list_pos_sale_payment_methods(db, only_active=True)
    meal_period = current_period_label()
    sorted_lines = sort_sale_lines_for_display(db, list(sale.lines))
    loyalty_ctx = _checkout_loyalty_context(db, sale, user)
    financial_preview = _checkout_financial_preview(loyalty_ctx, sale)
    ctx = loyalty_ctx["ctx"]
    delivery_cash_method = None
    if (
        ctx == "EXTERNAL"
        and getattr(sale, "external_order_type", None) is not None
        and sale.external_order_type.value == "DELIVERY"
    ):
        delivery_cash_method = get_default_cash_method(db)
    show_delivery_driver_button = sale_is_delivery(sale) and bool(sale.lines)
    op_name = None
    if open_pos_shift and getattr(open_pos_shift, "employee", None) is not None:
        op_name = open_pos_shift.employee.full_name_ar
    return {
        "request": request,
        "sale": sale,
        "cart_lines": sorted_lines,
        "methods": methods,
        "employee_meal_options": checkout_employee_options(db, meal_period),
        "employee_meal_period": meal_period,
        "error": error,
        "ok": ok,
        "ctx": ctx,
        "pre_room": None,
        "pre_room_guest": "",
        "checkout_modal": checkout_modal,
        **loyalty_ctx,
        "financial_preview": financial_preview,
        "delivery_cash_method": delivery_cash_method,
        "show_delivery_driver_button": show_delivery_driver_button,
        "delivery_handoff": (
            get_handoff(db, sale.id) if show_delivery_driver_button else None
        ),
        "saved_drivers": (
            list_recent_drivers(db) if show_delivery_driver_button else []
        ),
        "open_pos_shift": open_pos_shift,
        "pos_operator_name": op_name,
    }


def _checkout_modal_back(
    sale_id: int, *, error: str | None = None, ok: str | None = None
) -> str:
    q = f"open_checkout={sale_id}"
    if error:
        q += "&checkout_error=" + quote(error)
    if ok:
        q += "&checkout_ok=" + quote(ok)
    return "/pos?" + q


@checkout_router.get("/checkout/modal", response_class=HTMLResponse)
def pos_checkout_modal(
    request: Request,
    db: DBSession,
    user: User = Depends(_pos_perm),
    sale_id: int = Query(...),
    error: str | None = Query(None),
    ok: str | None = Query(None),
):
    from modules.sales.models import SaleStatus
    from modules.sales.service import load_sale_with_lines

    gr = require_open_pos_shift(request, db, user)
    if isinstance(gr, RedirectResponse):
        return HTMLResponse(
            '<div class="alert">يجب فتح وردية نقطة البيع أولاً.</div>',
            status_code=200,
        )
    loaded = load_sale_with_lines(db, sale_id)
    if loaded is None or loaded.status != SaleStatus.DRAFT:
        return HTMLResponse(
            '<div class="alert">الطلب غير متاح للتحصيل.</div>',
            status_code=200,
        )
    sale = loaded
    request.session["draft_sale_id"] = sale.id
    _ensure_sale_referral_from_online(db, sale)
    db.commit()
    sale = load_sale_with_lines(db, sale.id) or sale
    gate = _checkout_gate_message(db, sale)
    if gate:
        return HTMLResponse(f'<div class="alert">{gate}</div>', status_code=200)
    ctx = _build_checkout_template_ctx(
        request,
        db,
        user,
        sale,
        error=error,
        ok=ok,
        checkout_modal=True,
        open_pos_shift=gr,
    )
    return templates.TemplateResponse("pos_checkout_modal.html", ctx)


@checkout_router.get("/checkout", response_class=HTMLResponse)
def pos_checkout_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_pos_perm),
    error: str | None = Query(None),
    ok: str | None = Query(None),
):
    gr = require_open_pos_shift(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    open_pos_shift = gr
    sale = _get_draft_for_checkout(request, db, user)
    if sale is None:
        return RedirectResponse(
            "/pos?ctx_err=" + quote("اختر طلباً من القائمة أو أنشئ طلباً جديداً."),
            status_code=302,
        )
    loaded = load_sale_with_lines(db, sale.id)
    if loaded:
        sale = loaded
    _ensure_sale_referral_from_online(db, sale)
    db.commit()
    loaded = load_sale_with_lines(db, sale.id)
    if loaded:
        sale = loaded
    gate = _checkout_gate_message(db, sale)
    if gate:
        return RedirectResponse(
            "/pos?ctx_err=" + quote(gate),
            status_code=302,
        )
    ctx = _build_checkout_template_ctx(
        request,
        db,
        user,
        sale,
        error=error,
        ok=ok,
        checkout_modal=False,
        open_pos_shift=open_pos_shift,
    )
    return templates.TemplateResponse("pos_checkout.html", ctx)


@checkout_router.post("/checkout/bind-customer", response_class=HTMLResponse)
def checkout_bind_customer(
    request: Request,
    db: DBSession,
    user: User = Depends(_pos_perm),
    checkout_phone: str = Form(""),
    checkout_name: str = Form(""),
):
    """التعرّف على عميل مسجّل بالهاتف وتعبئة الاسم (HTMX من شاشة الدفع)."""
    from modules.sales.service import load_sale_with_lines

    gr = require_open_pos_shift(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    sale = _get_draft_for_checkout(request, db, user)
    if sale is None:
        return HTMLResponse("", status_code=204)
    _bind_checkout_customer(
        db,
        sale,
        phone=checkout_phone,
        name=checkout_name,
    )
    db.commit()
    loaded = load_sale_with_lines(db, sale.id) or sale
    loyalty_ctx = _checkout_loyalty_context(db, loaded, user)
    financial_preview = _checkout_financial_preview(loyalty_ctx, loaded)
    return templates.TemplateResponse(
        "_checkout_bind_customer.html",
        {
            "request": request,
            "sale": loaded,
            **loyalty_ctx,
            "financial_preview": financial_preview,
        },
    )


@checkout_router.post("/checkout", response_class=HTMLResponse)
async def pos_checkout_submit(
    request: Request,
    db: DBSession,
    user: User = Depends(_pos_perm),
):
    """دفع فوري + (إن كان EXTERNAL وللعميل هاتف) منح نقاط ولاء. قيد الشقة من شاشة POS فقط."""
    from modules.delivery.service import DeliveryError, record_delivery_cash_settlement
    from modules.sales.models import ExternalOrderType, SaleContext

    gr = require_open_pos_shift(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    active_shift = gr

    form = await request.form()
    payment_method_id = str(form.get("payment_method_id") or "")
    pay_mode = str(form.get("pay_mode") or "now")
    hotel_room_id = str(form.get("hotel_room_id") or "")
    hotel_guest_name = str(form.get("hotel_guest_name") or "")
    hotel_note = str(form.get("hotel_note") or "")
    checkout_phone = str(form.get("checkout_phone") or "").strip()
    checkout_name = str(form.get("checkout_name") or "").strip()
    checkout_party = str(form.get("checkout_party") or "customer").strip()
    employee_meal_employee_id = str(form.get("employee_meal_employee_id") or "").strip()
    employee_meal_payment = str(form.get("employee_meal_payment") or "personal").strip()
    use_loyalty_redeem = str(form.get("use_loyalty_redeem") or "") in (
        "on",
        "1",
        "true",
        "yes",
    )
    split_payment = str(form.get("split_payment") or "") in (
        "1",
        "on",
        "true",
        "yes",
    )
    employee_checkout = checkout_party == "employee"
    employee_wallet_checkout = employee_checkout and employee_meal_payment == "wallet"
    if employee_wallet_checkout:
        use_loyalty_redeem = False
    back_modal = str(form.get("return_to") or "").strip().lower() == "modal"

    sale = _get_draft_for_checkout(request, db, user)
    if sale is None:
        return RedirectResponse(
            "/pos?ctx_err=" + quote("لا يوجد طلب نشط."),
            status_code=302,
        )
    loaded_chk = load_sale_with_lines(db, sale.id)
    if loaded_chk:
        sale = loaded_chk

    def checkout_fail(msg: str) -> RedirectResponse:
        if back_modal:
            return RedirectResponse(
                _checkout_modal_back(sale.id, error=msg),
                status_code=302,
            )
        return RedirectResponse(
            "/pos/checkout?error=" + quote(msg),
            status_code=302,
        )

    if not sale.lines:
        return RedirectResponse(
            "/pos?ctx_err=" + quote("الطلب فارغ."),
            status_code=302,
        )
    if sale.context_type == SaleContext.EXTERNAL and not sale.customer_id:
        return RedirectResponse(
            "/pos?ctx_err="
            + quote("رقم هاتف العميل مطلوب للطلب الخارجي قبل الدفع."),
            status_code=302,
        )

    ctx = sale.context_type.value if sale.context_type else "TABLE"

    if pay_mode == "room" or sale.context_type == SaleContext.ROOM:
        return checkout_fail(
            "قيد الشقة يتم من نقطة البيع عند إعداد الطلب، وليس من شاشة إتمام البيع."
        )

    if table_assignment_required(sale):
        return RedirectResponse(
            "/pos?ctx_err=يجب اختيار الطاولة التي سينزل عليها الطلب. وإذا لم تكن طاولة فاختر «خارجي».",
            status_code=302,
        )

    from modules.sales.order_policy import load_order_policy, payment_block_reason

    policy = load_order_policy(db)
    pay_block = payment_block_reason(db, sale, policy)
    if pay_block:
        return RedirectResponse(
            "/pos?ctx_err=" + quote(pay_block),
            status_code=302,
        )

    employee_for_meal = None
    if employee_checkout:
        from modules.hr.models import Employee, EmployeeStatus

        if not employee_meal_employee_id.isdigit():
            return checkout_fail("اختر الموظف قبل إتمام وجبة الموظف.")
        employee_for_meal = db.get(Employee, int(employee_meal_employee_id))
        if employee_for_meal is None or employee_for_meal.status != EmployeeStatus.ACTIVE:
            return checkout_fail("الموظف غير موجود أو غير نشط.")
        if employee_meal_payment not in ("personal", "wallet"):
            return checkout_fail("طريقة دفع وجبة الموظف غير صالحة.")

    # ===== دفع فوري =====
    pm_id: int | None = None
    pm: PaymentMethod | None = None
    if (payment_method_id or "").strip() and not split_payment:
        try:
            pm_id = int(payment_method_id)
        except (TypeError, ValueError):
            return checkout_fail("أسلوب الدفع غير صالح.")
        pm = db.get(PaymentMethod, pm_id)
        if pm is None or not pm.is_active:
            return checkout_fail("أسلوب الدفع غير صالح.")

    proof_fn: str | None = None
    proof_upload = form.get("bank_transfer_proof")
    if pm is not None and pm.kind == PaymentMethodKind.BANK and not split_payment:
        if isinstance(proof_upload, UploadFile) and proof_upload.filename:
            try:
                proof_fn = save_sale_payment_proof(
                    proof_upload, _PAYMENTS_WEB_STATIC
                )
            except ValueError as exc:
                return checkout_fail(str(exc))

    from modules.customers.service import CustomersError
    from modules.hr.meal_allowance import EmployeeMealError

    try:
        from modules.authz.permissions import LOYALTY_REDEEM, SALES_CREATE
        from modules.authz.service import user_has_permission
        from modules.customers.service import (
            attach_customer_to_sale,
            get_customer,
            grant_loyalty_after_sale,
            loyalty_settings,
            redeem_points_for_sale,
        )

        customer_phone = checkout_phone or None
        customer_name = checkout_name or None
        if employee_checkout and not employee_wallet_checkout and employee_for_meal is not None:
            customer_phone = (employee_for_meal.phone or "").strip() or None
            customer_name = employee_for_meal.full_name_ar
            if customer_phone is None:
                return checkout_fail("لا يوجد رقم هاتف للموظف لاحتساب نقاط الولاء.")
        cust = attach_customer_to_sale(db, sale, phone=customer_phone, name=customer_name)
        referral_code = str(form.get("referral_code") or "").strip()
        if not referral_code:
            referral_code = (sale.referral_code_used or "").strip()
        if referral_code and cust is not None and not sale.referrer_customer_id:
            from modules.customers.referral_service import (
                ReferralError,
                apply_referral_to_sale,
            )

            try:
                apply_referral_to_sale(
                    db, sale, code=referral_code, buyer=cust
                )
            except ReferralError as exc:
                db.rollback()
                _unlink_static_relative(_PAYMENTS_WEB_STATIC, proof_fn)
                return checkout_fail(str(exc))
        complete_sale(db, sale.id, user.id, pos_shift_id=active_shift.id)

        loyalty_discount = Decimal("0")
        redeem_pts = Decimal("0")
        if (
            cust is not None
            and use_loyalty_redeem
            and loyalty_settings(db)["enabled"]
            and (
                user_has_permission(user, LOYALTY_REDEEM)
                or user_has_permission(user, SALES_CREATE)
            )
        ):
            redeem_pts, loyalty_discount = redeem_points_for_sale(
                db,
                customer=cust,
                sale_id=sale.id,
                sale_total=sale.total,
                user_id=user.id,
            )

        amount_due = (
            Decimal(str(sale.total or 0)) - loyalty_discount
        ).quantize(Decimal("0.001"))
        if amount_due < 0:
            amount_due = Decimal("0")

        employee_meal_cover = Decimal("0")
        if employee_wallet_checkout and employee_for_meal is not None:
            from modules.hr.meal_allowance import (
                consume_wallet_for_sale,
                ensure_employee_meal_expense_for_sale,
                ensure_staff_meal_payment_method,
            )

            _, employee_meal_cover, amount_due = consume_wallet_for_sale(
                db,
                employee_id=employee_for_meal.id,
                sale_id=sale.id,
                sale_total=amount_due,
                requested_cover=amount_due,
                created_by_id=user.id,
            )
            if employee_meal_cover > 0:
                staff_pm = ensure_staff_meal_payment_method(db)
                record_sale_payment(
                    db,
                    sale.id,
                    staff_pm.id,
                    employee_meal_cover,
                )
                ensure_employee_meal_expense_for_sale(
                    db,
                    employee=employee_for_meal,
                    sale_id=sale.id,
                    covered_amount=employee_meal_cover,
                    user_id=user.id,
                )
            if amount_due > 0:
                employee_phone = (employee_for_meal.phone or "").strip()
                if not employee_phone:
                    raise CustomersError(
                        "تبقى فرق على الموظف، ولا يوجد رقم هاتف للموظف لاحتساب نقاط الولاء."
                    )
                cust = attach_customer_to_sale(
                    db,
                    sale,
                    phone=employee_phone,
                    name=employee_for_meal.full_name_ar,
                )

        if amount_due > 0:
            if split_payment:
                split_rows: list[tuple[int, Decimal]] = []
                for key, value in form.multi_items():
                    sk = str(key)
                    if not sk.startswith("split_amount_"):
                        continue
                    raw_id = sk.replace("split_amount_", "", 1)
                    if not raw_id.isdigit():
                        continue
                    try:
                        amount = Decimal(str(value or "0").strip() or "0").quantize(
                            Decimal("0.001")
                        )
                    except (InvalidOperation, ValueError):
                        raise PaymentsError("أحد مبالغ الدفع المقسّم غير صالح.")
                    if amount < 0:
                        raise PaymentsError("لا يمكن إدخال مبلغ سالب في الدفع المقسّم.")
                    if amount > 0:
                        split_rows.append((int(raw_id), amount))
                if not split_rows:
                    raise PaymentsError("أدخل مبلغاً واحداً على الأقل في الدفع المقسّم.")
                split_total = sum((amount for _mid, amount in split_rows), Decimal("0")).quantize(
                    Decimal("0.001")
                )
                if split_total != amount_due:
                    raise PaymentsError(
                        f"مجموع الدفع المقسّم ({split_total}) يجب أن يساوي المبلغ المطلوب ({amount_due})."
                    )
                for split_method_id, split_amount in split_rows:
                    record_sale_payment(
                        db,
                        sale.id,
                        split_method_id,
                        split_amount,
                    )
            else:
                if pm_id is None or pm is None:
                    return checkout_fail("اختر وسيلة الدفع المناسبة")
                record_sale_payment(
                    db,
                    sale.id,
                    pm_id,
                    amount_due,
                    payment_proof_image_filename=proof_fn,
                )

        if (
            sale.context_type == SaleContext.EXTERNAL
            and sale.external_order_type == ExternalOrderType.DELIVERY
            and sale.delivery_fee > 0
        ):
            record_delivery_cash_settlement(
                db,
                sale_id=sale.id,
                amount=sale.delivery_fee,
                zone_id=sale.delivery_zone_id,
                user_id=user.id,
                note=f"أجرة توصيل للطلب الخارجي #{sale.id}",
            )

        earned_pts = Decimal("0")
        if cust is not None:
            from modules.messaging.service import save_checkout_consent

            messaging_opt_in = str(form.get("messaging_opt_in") or "") == "on"
            messaging_channel = str(form.get("messaging_channel") or "whatsapp").strip()
            save_checkout_consent(
                db,
                customer_id=cust.id,
                opt_in=messaging_opt_in,
                preferred_channel=messaging_channel,
            )

        if sale.customer_id:
            cust_pay = get_customer(db, sale.customer_id)
            if cust_pay is not None:
                earned_pts = grant_loyalty_after_sale(
                    db,
                    customer=cust_pay,
                    sale_id=sale.id,
                    paid_total=amount_due,
                    user_id=user.id,
                    skip_notification=True,
                )
                from modules.customers.referral_service import (
                    ReferralError,
                    grant_referral_after_sale,
                )

                try:
                    grant_referral_after_sale(
                        db,
                        sale_id=sale.id,
                        paid_total=amount_due,
                        user_id=user.id,
                    )
                except ReferralError as exc:
                    db.rollback()
                    _unlink_static_relative(_PAYMENTS_WEB_STATIC, proof_fn)
                    return checkout_fail(str(exc))

        db.commit()

        try:
            from modules.messaging.chat_order_service import notify_web_chat_checkout_complete

            notify_web_chat_checkout_complete(db, sale.id)
            db.commit()
        except Exception:
            db.rollback()

        if cust is not None:
            cust_final = get_customer(db, cust.id)
            if cust_final is not None:
                from modules.messaging.service import notify_loyalty_checkout

                if redeem_pts > 0 or earned_pts > 0:
                    notify_loyalty_checkout(
                        db,
                        customer=cust_final,
                        sale_id=sale.id,
                        redeemed=redeem_pts,
                        earned=earned_pts,
                    )
    except InsufficientStock as e:
        db.rollback()
        _unlink_static_relative(_PAYMENTS_WEB_STATIC, proof_fn)
        return checkout_fail(str(e))
    except (SalesError, PaymentsError, DeliveryError, CustomersError, EmployeeMealError) as e:
        db.rollback()
        _unlink_static_relative(_PAYMENTS_WEB_STATIC, proof_fn)
        return checkout_fail(str(e))
    except Exception as e:  # noqa: BLE001
        db.rollback()
        _unlink_static_relative(_PAYMENTS_WEB_STATIC, proof_fn)
        import logging

        logging.getLogger("pos.checkout").exception("checkout failed: %s", e)
        msg = str(e).strip() or "تعذّر إتمام البيع. راجع السجل أو أعد المحاولة."
        if len(msg) > 180:
            msg = "تعذّر إتمام البيع. راجع السجل أو أعد المحاولة."
        return checkout_fail(msg)

    # توجيه طلبات الأقسام (شاشة المطبخ / واتساب / طباعة) — خارج المعاملة المالية
    # حالة WhatsApp: تُحفظ التذكرة بحالة PENDING_SEND ثم تُرسل في خيط خلفية
    sale_id_for_bg = sale.id
    needs_wa_dispatch = False
    try:
        from modules.kds.service import create_tickets_for_sale

        tickets = create_tickets_for_sale(db, sale.id)
        db.commit()
        needs_wa_dispatch = any(
            t.delivery_status == "PENDING_SEND" for t in tickets
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        import logging

        logging.getLogger("kds").warning("ticket creation failed: %s", exc)

    if needs_wa_dispatch:
        # إرسال WhatsApp في الخلفية حتى لا يتأخر الكاشير
        from infra.background import run_in_background
        from modules.kds.service import dispatch_pending_whatsapp_tickets

        run_in_background(
            dispatch_pending_whatsapp_tickets,
            sale_id_for_bg,
            name="kds-whatsapp",
        )

    # تنبيه نفاد المخزون يُرسل في الخلفية أيضاً
    try:
        from modules.settings.service import get_bool

        if get_bool(db, "alerts_enabled", False) or get_bool(
            db, "messaging_enabled", False
        ):
            from infra.background import run_in_background, with_db
            from modules.alerts.service import send_low_stock_alert

            def _bg_alert():
                with with_db() as bg_db:
                    send_low_stock_alert(bg_db, all_warehouses=False)
                    from modules.alerts.service import send_product_expiry_alert

                    send_product_expiry_alert(bg_db)
                    bg_db.commit()

            run_in_background(_bg_alert, name="low-stock-alert")
    except Exception:
        pass

    receipt_id = sale.id
    request.session.pop("draft_sale_id", None)
    psid = _session_pos_shift_id_checkout(request)
    new_sale = create_draft_sale(db, user.id, pos_shift_id=psid)
    request.session["draft_sale_id"] = new_sale.id
    db.commit()
    from modules.printing.service import checkout_receipt_redirect

    return RedirectResponse(
        checkout_receipt_redirect(db, receipt_id),
        status_code=302,
    )


# ============================================================
# Helpers
# ============================================================
def _month_default_range() -> tuple[datetime, datetime]:
    return local_period_bounds("month")


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        from datetime import date

        return local_day_start_utc(date.fromisoformat(raw.strip()[:10]))
    except ValueError:
        return None


def _parse_datetime_local(raw: str) -> datetime | None:
    if not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _purchase_term_methods(db: DBSession, user: User):
    from modules.payments.models import PaymentMethodDomain

    if purchase_user_limited_to_custody(user):
        return list_payment_methods_for_purchase_term_custody(
            db, only_active=True, domain=PaymentMethodDomain.RESTAURANT
        )
    return list_payment_methods_for_purchase_term(db, only_active=True)


def _purchase_pay_methods(db: DBSession, user: User):
    from modules.payments.models import PaymentMethodDomain

    if purchase_user_limited_to_custody(user):
        return list_payment_methods_purchase_custody_for_pay(
            db, only_active=True, domain=PaymentMethodDomain.RESTAURANT
        )
    out: list = []
    seen: set[int] = set()
    for m in list_payment_methods_main_treasury_for_pay(db, only_active=True):
        if m.id not in seen:
            out.append(m)
            seen.add(m.id)
    for m in list_payment_methods_purchase_custody_for_pay(db, only_active=True):
        if m.id not in seen:
            out.append(m)
            seen.add(m.id)
    return out


def _assert_purchase_term_pm(db: DBSession, user: User, pm_id: int):
    if purchase_user_limited_to_custody(user):
        return assert_purchase_custody_payment_method(db, pm_id)
    return assert_purchase_term_payment_method(db, pm_id)


# ============================================================
# Admin: Inventory purchase invoices (with line items)
# ============================================================
purchases_router = APIRouter(prefix="/admin/purchases", tags=["purchases"])
_purchases_perm = require_any_permission(PURCHASE_INVOICES_MANAGE, PURCHASES_MANAGE)
_purchase_finance_perm = require_permission(PURCHASES_MANAGE)


@purchases_router.get("", response_class=HTMLResponse)
def purchases_list(
    request: Request,
    db: DBSession,
    user: User = Depends(_purchases_perm),
    start: str | None = Query(None),
    end: str | None = Query(None),
    suppliers: list[str] | None = Query(None),
    pay: str | None = Query(None),
    error: str | None = Query(None),
    saved: int = Query(0),
):
    from modules.payables.service import (
        build_inventory_purchase_list_views,
        list_inventory_supplier_names,
    )
    from modules.platform.business_domain import domain_label, purchase_domain_db_values

    s, e = _month_default_range()
    s_user = _parse_date(start)
    e_user = _parse_date(end)
    if s_user is not None:
        s = s_user
    if e_user is not None:
        e = e_user
    supplier_filter = [x.strip() for x in (suppliers or []) if (x or "").strip()]
    pay_filter = (pay or "all").strip().lower()
    domain = _finance_domain_filter(request, user)

    stmt = (
        select(Purchase)
        .where(
            Purchase.created_at >= s,
            Purchase.created_at < e,
            Purchase.kind == PurchaseKind.INVENTORY,
        )
        .options(
            selectinload(Purchase.lines),
            selectinload(Purchase.method),
            selectinload(Purchase.warehouse),
        )
        .order_by(Purchase.id.desc())
        .limit(500)
    )
    domain_vals = purchase_domain_db_values(domain)
    if domain_vals is not None:
        stmt = stmt.where(Purchase.business_domain.in_(domain_vals))
    if supplier_filter:
        stmt = stmt.where(Purchase.supplier.in_(supplier_filter))
    items = list(db.scalars(stmt).all())
    rows, summary = build_inventory_purchase_list_views(
        db, items, pay_filter=pay_filter
    )
    all_suppliers = list_inventory_supplier_names(db)
    return templates.TemplateResponse(
        "admin_purchases_list.html",
        {
            "request": request,
            "rows": rows,
            "summary": summary,
            "all_suppliers": all_suppliers,
            "selected_suppliers": supplier_filter,
            "pay_filter": pay_filter,
            "start": s,
            "end": e,
            "error": error,
            "saved": bool(saved),
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "الكل",
        },
    )


@purchases_router.get("/new", response_class=HTMLResponse)
def purchases_new_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_purchases_perm),
    error: str | None = Query(None),
):
    from modules.inventory.service import get_main_warehouse, list_warehouses

    methods = _purchase_term_methods(db, user)
    products = list_stockable_products(db)
    warehouses = list_warehouses(db)
    main_wh = get_main_warehouse(db)
    return templates.TemplateResponse(
        "admin_purchase_new.html",
        {
            "request": request,
            "methods": methods,
            "products": products,
            "warehouses": warehouses,
            "default_warehouse_id": main_wh.id,
            "error": error,
            "today": now_local().strftime("%Y-%m-%dT%H:%M"),
        },
    )


@purchases_router.post("/new", response_class=HTMLResponse)
async def purchases_create(
    request: Request,
    db: DBSession,
    user: User = Depends(_purchases_perm),
):
    form = await request.form()
    pm_raw = (form.get("payment_method_id") or "").strip()
    supplier = (form.get("supplier") or "").strip()
    supplier_phone = (form.get("supplier_phone") or "").strip()
    note = (form.get("note") or "").strip()
    purchase_date_raw = (form.get("purchase_date") or "").strip()
    supplier_invoice_ref = (form.get("supplier_invoice_ref") or "").strip()

    try:
        pm_id = int(pm_raw)
    except (TypeError, ValueError):
        return RedirectResponse(
            "/admin/purchases/new?error=" + quote("أسلوب الدفع غير صالح."),
            status_code=302,
        )

    try:
        _assert_purchase_term_pm(db, user, pm_id)
    except PaymentsError as e:
        return RedirectResponse(
            "/admin/purchases/new?error=" + quote(str(e)),
            status_code=302,
        )

    pm = db.get(PaymentMethod, pm_id)

    invoice_image_filename: str | None = None
    inv_upload = form.get("invoice_image")
    if isinstance(inv_upload, UploadFile) and inv_upload.filename:
        try:
            invoice_image_filename = save_purchase_invoice_image(
                inv_upload, _PAYMENTS_WEB_STATIC
            )
        except ValueError as exc:
            return RedirectResponse(
                f"/admin/purchases/new?error={quote(str(exc))}",
                status_code=302,
            )

    payment_proof_image_filename: str | None = None
    proof_upload = form.get("payment_proof_image")
    if isinstance(proof_upload, UploadFile) and proof_upload.filename:
        if pm.kind != PaymentMethodKind.BANK:
            if invoice_image_filename:
                _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
            return RedirectResponse(
                "/admin/purchases/new?error="
                + quote(
                    "إيصال إثبات الدفع يُرفق فقط عند اختيار محفظة من نوع مصرف."
                ),
                status_code=302,
            )
        try:
            payment_proof_image_filename = save_purchase_bank_payment_receipt(
                proof_upload, _PAYMENTS_WEB_STATIC
            )
        except ValueError as exc:
            if invoice_image_filename:
                _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
            return RedirectResponse(
                f"/admin/purchases/new?error={quote(str(exc))}",
                status_code=302,
            )

    product_ids = form.getlist("product_id")
    qtys = form.getlist("quantity")
    costs = form.getlist("unit_cost")
    prod_dates = form.getlist("expiry_production_date")
    exp_dates = form.getlist("expiry_date")
    line_kinds = form.getlist("line_kind")
    item_names = form.getlist("item_name")
    item_units = form.getlist("item_unit")
    useful_lives = form.getlist("useful_life_months")
    salvage_values = form.getlist("salvage_value")
    from modules.catalog.models import Product
    from modules.inventory.lots import parse_lot_dates_from_form
    from modules.payments.service import MixedPurchaseLineIn

    def _clean_files_and_redirect(msg: str):
        if invoice_image_filename:
            _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
        if payment_proof_image_filename:
            _unlink_static_relative(
                _PAYMENTS_WEB_STATIC, payment_proof_image_filename
            )
        return RedirectResponse(
            "/admin/purchases/new?error=" + quote(msg),
            status_code=302,
        )

    n = max(len(qtys), len(costs), len(line_kinds), len(product_ids), 0)
    lines: list[MixedPurchaseLineIn] = []
    for i in range(n):
        kind = (
            (line_kinds[i] if i < len(line_kinds) else "PRODUCT") or "PRODUCT"
        ).strip().upper()
        raw_qty = qtys[i] if i < len(qtys) else ""
        raw_cost = costs[i] if i < len(costs) else ""
        try:
            qty = Decimal((raw_qty or "0").strip() or "0")
            cost = Decimal((raw_cost or "0").strip() or "0")
        except (InvalidOperation, ValueError):
            return _clean_files_and_redirect("بيانات بند غير صالحة.")
        if qty <= 0:
            continue

        if kind == "PRODUCT":
            raw_pid = product_ids[i] if i < len(product_ids) else ""
            if not (raw_pid or "").strip():
                continue
            try:
                pid = int(raw_pid)
            except (TypeError, ValueError):
                return _clean_files_and_redirect("بيانات بند غير صالحة.")
            product = db.get(Product, pid)
            if product is None:
                return _clean_files_and_redirect("صنف غير موجود.")
            prod_raw = prod_dates[i] if i < len(prod_dates) else ""
            exp_raw = exp_dates[i] if i < len(exp_dates) else ""
            try:
                lot_dates = parse_lot_dates_from_form(
                    prod_raw,
                    exp_raw,
                    product_name=product.name_ar,
                    required=bool(product.expiry_tracked),
                )
            except Exception as exc:
                from modules.catalog.service import CatalogError

                if not isinstance(exc, CatalogError):
                    raise
                return _clean_files_and_redirect(str(exc))
            lines.append(
                MixedPurchaseLineIn(
                    kind="PRODUCT",
                    product_id=pid,
                    quantity=qty,
                    unit_cost=cost,
                    production_date=lot_dates.production_date,
                    expiry_date=lot_dates.expiry_date,
                )
            )
        elif kind == "FIXED_ASSET":
            nm = (item_names[i] if i < len(item_names) else "") or ""
            unit = (item_units[i] if i < len(item_units) else "") or ""
            life_raw = useful_lives[i] if i < len(useful_lives) else "12"
            salv_raw = salvage_values[i] if i < len(salvage_values) else "0"
            try:
                life = int((life_raw or "0").strip() or "0")
                salv = Decimal((salv_raw or "0").strip() or "0")
            except (InvalidOperation, ValueError):
                return _clean_files_and_redirect("بيانات أصل ثابت غير صالحة.")
            lines.append(
                MixedPurchaseLineIn(
                    kind="FIXED_ASSET",
                    item_name=nm.strip(),
                    unit=unit.strip() or None,
                    quantity=qty,
                    unit_cost=cost,
                    useful_life_months=life,
                    salvage_value=salv,
                )
            )
        elif kind == "CONSUMABLE":
            nm = (item_names[i] if i < len(item_names) else "") or ""
            unit = (item_units[i] if i < len(item_units) else "") or ""
            lines.append(
                MixedPurchaseLineIn(
                    kind="CONSUMABLE",
                    item_name=nm.strip(),
                    unit=unit.strip() or None,
                    quantity=qty,
                    unit_cost=cost,
                )
            )
        else:
            return _clean_files_and_redirect(f"نوع بند غير معروف: {kind}")

    if not lines:
        return _clean_files_and_redirect("أضف بنداً واحداً على الأقل.")

    has_product = any(ln.kind == "PRODUCT" for ln in lines)
    wh_raw = (form.get("warehouse_id") or "").strip()
    warehouse_id: int | None = None
    if has_product or wh_raw:
        try:
            warehouse_id = int(wh_raw)
        except (TypeError, ValueError):
            if has_product:
                return _clean_files_and_redirect(
                    "اختر المخزن الذي تُضاف إليه البضاعة."
                )

    pay_now_amount: Decimal | None = None
    pay_now_method_id: int | None = None
    pay_now_raw = (form.get("pay_now_amount") or "").strip()
    if pay_now_raw:
        try:
            pay_now_amount = Decimal(pay_now_raw)
        except InvalidOperation:
            pay_now_amount = None
    pay_now_pm_raw = (form.get("pay_now_method_id") or "").strip()
    if pay_now_pm_raw:
        try:
            pay_now_method_id = int(pay_now_pm_raw)
        except ValueError:
            pay_now_method_id = None

    created_at = _parse_datetime_local(purchase_date_raw)
    finance_domain = _finance_domain_filter(request, user)
    try:
        record_inventory_purchase(
            db,
            payment_method_id=pm_id,
            supplier=supplier,
            supplier_phone=supplier_phone or None,
            note=note,
            lines=lines,
            user_id=user.id,
            warehouse_id=warehouse_id,
            created_at=created_at,
            supplier_invoice_ref=supplier_invoice_ref or None,
            invoice_image_filename=invoice_image_filename,
            payment_proof_image_filename=payment_proof_image_filename,
            pay_now_amount=pay_now_amount,
            pay_now_method_id=pay_now_method_id,
            filter_domain=finance_domain,
            business_domain=(form.get("business_domain") or "").strip() or None,
            purchase_custody_only=purchase_user_limited_to_custody(user),
        )
        db.commit()
    except PaymentsError as e:
        db.rollback()
        if invoice_image_filename:
            _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
        if payment_proof_image_filename:
            _unlink_static_relative(
                _PAYMENTS_WEB_STATIC, payment_proof_image_filename
            )
        return RedirectResponse(
            f"/admin/purchases/new?error={quote(str(e))}",
            status_code=302,
        )
    return RedirectResponse("/admin/purchases?saved=1", status_code=302)


@purchases_router.get("/{pid}", response_class=HTMLResponse)
def purchases_detail(
    request: Request,
    pid: int,
    db: DBSession,
    user: User = Depends(_purchases_perm),
):
    p = db.execute(
        select(Purchase)
        .where(Purchase.id == pid)
        .options(
            selectinload(Purchase.lines),
            selectinload(Purchase.method),
            selectinload(Purchase.warehouse),
            selectinload(Purchase.payments).selectinload(PurchasePayment.method),
        )
    ).scalar_one_or_none()
    if p is None or p.kind != PurchaseKind.INVENTORY:
        return RedirectResponse("/admin/purchases", status_code=302)
    products_by_id = {
        prod.id: prod for prod in db.scalars(select(Product)).all()
    }
    paid = sum_purchase_payments(db, p.id)
    outstanding = purchase_outstanding(db, p)
    pay_methods = _purchase_pay_methods(db, user)
    from modules.payments.cost_reference import (
        effective_purchase_amount,
        is_cost_reference_purchase,
    )

    saved = request.query_params.get("saved")
    error = request.query_params.get("error")
    cost_saved = request.query_params.get("cost_saved") == "1"
    saved_edit = request.query_params.get("saved_edit") == "1"
    return templates.TemplateResponse(
        "admin_purchase_detail.html",
        {
            "request": request,
            "purchase": p,
            "products_by_id": products_by_id,
            "paid_total": paid,
            "outstanding": outstanding,
            "pay_methods": pay_methods,
            "saved_pay": saved,
            "saved_edit": saved_edit,
            "cost_saved": cost_saved,
            "error": error,
            "is_cost_reference": is_cost_reference_purchase(p),
            "display_total": effective_purchase_amount(p),
        },
    )


@purchases_router.post("/{pid}/pay", response_class=HTMLResponse)
async def purchases_record_payment(
    request: Request,
    pid: int,
    db: DBSession,
    user: User = Depends(_purchases_perm),
):
    form = await request.form()
    try:
        pm_id = int((form.get("payment_method_id") or "").strip())
        amount = Decimal((form.get("amount") or "0").strip())
    except (ValueError, InvalidOperation):
        return RedirectResponse(
            f"/admin/purchases/{pid}?error=" + quote("بيانات الدفع غير صالحة."),
            status_code=302,
        )
    proof_filename: str | None = None
    proof_upload = form.get("payment_proof_image")
    if isinstance(proof_upload, UploadFile) and proof_upload.filename:
        try:
            proof_filename = save_purchase_bank_payment_receipt(
                proof_upload, _PAYMENTS_WEB_STATIC
            )
        except ValueError as exc:
            return RedirectResponse(
                f"/admin/purchases/{pid}?error={quote(str(exc))}",
                status_code=302,
            )
    try:
        record_purchase_payment(
            db,
            purchase_id=pid,
            payment_method_id=pm_id,
            amount=amount,
            user_id=user.id,
            note=(form.get("note") or "").strip() or None,
            payment_proof_image_filename=proof_filename,
            purchase_custody_only=purchase_user_limited_to_custody(user),
        )
        db.commit()
    except PaymentsError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/purchases/{pid}?error={quote(str(e))}",
            status_code=302,
        )
    return RedirectResponse(f"/admin/purchases/{pid}?saved=1", status_code=302)


@purchases_router.post("/{pid}/edit-cost-reference", response_class=HTMLResponse)
async def purchases_edit_cost_reference(
    request: Request,
    pid: int,
    db: DBSession,
    _: User = Depends(_purchases_perm),
):
    from modules.payments.cost_reference import (
        CostReferenceError,
        edit_cost_reference_lines,
        parse_line_costs_from_form,
    )

    form = await request.form()
    try:
        line_costs = parse_line_costs_from_form(list(form.multi_items()))
        edit_cost_reference_lines(db, pid, line_costs=line_costs)
        db.commit()
    except CostReferenceError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/purchases/{pid}?error={quote(str(exc))}",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/purchases/{pid}?cost_saved=1",
        status_code=302,
    )


@purchases_router.post("/{pid}/delete", response_class=HTMLResponse)
def purchases_delete(
    request: Request,
    pid: int,
    db: DBSession,
    _: User = Depends(_purchases_perm),
):
    try:
        delete_purchase(db, pid)
        db.commit()
    except InsufficientStock as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/purchases?error={e}", status_code=302
        )
    return RedirectResponse("/admin/purchases", status_code=302)


# ============================================================
# Admin: Expenses (no inventory impact)
# ============================================================
expenses_router = APIRouter(prefix="/admin/expenses", tags=["expenses"])


@expenses_router.get("", response_class=HTMLResponse)
def expenses_list(
    request: Request,
    db: DBSession,
    user: User = Depends(_purchase_finance_perm),
    start: str | None = Query(None),
    end: str | None = Query(None),
    error: str | None = Query(None),
    saved: int = Query(0),
):
    s, e = _month_default_range()
    s_user = _parse_date(start)
    e_user = _parse_date(end)
    if s_user is not None:
        s = s_user
    if e_user is not None:
        e = e_user
    domain = _finance_domain_filter(request, user)
    methods = list_payment_methods_for_pay(db, only_active=True, domain=domain)
    items = list_purchases(db, s, e, kind=PurchaseKind.EXPENSE, domain=domain)
    total = sum((p.amount for p in items), Decimal("0"))
    from modules.platform.business_domain import domain_label

    return templates.TemplateResponse(
        "admin_expenses.html",
        {
            "request": request,
            "methods": methods,
            "items": items,
            "total": total,
            "start": s,
            "end": e,
            "error": error,
            "saved": bool(saved),
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "الكل",
            "domain_choices": _domain_choices(),
        },
    )


@expenses_router.post("/add", response_class=HTMLResponse)
async def expenses_add(
    request: Request,
    db: DBSession,
    user: User = Depends(_purchase_finance_perm),
):
    form = await request.form()
    payment_method_id = (form.get("payment_method_id") or "").strip()
    amount = (form.get("amount") or "").strip()
    expense_category = (form.get("expense_category") or "").strip()
    supplier = (form.get("supplier") or "").strip()
    note = (form.get("note") or "").strip()
    purchase_date = (form.get("purchase_date") or "").strip()
    supplier_invoice_ref = (form.get("supplier_invoice_ref") or "").strip()

    invoice_image_filename: str | None = None
    inv_upload = form.get("invoice_image")
    if isinstance(inv_upload, UploadFile) and inv_upload.filename:
        try:
            invoice_image_filename = save_purchase_invoice_image(
                inv_upload, _PAYMENTS_WEB_STATIC
            )
        except ValueError as exc:
            return RedirectResponse(
                "/admin/expenses?error=" + quote(str(exc)),
                status_code=302,
            )

    try:
        amt = Decimal(amount)
    except (InvalidOperation, AttributeError):
        if invoice_image_filename:
            _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
        return RedirectResponse(
            "/admin/expenses?error=" + quote("المبلغ غير صالح."),
            status_code=302,
        )
    try:
        pm_id = int(payment_method_id)
    except (TypeError, ValueError):
        if invoice_image_filename:
            _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
        return RedirectResponse(
            "/admin/expenses?error=" + quote("أسلوب الدفع غير صالح."),
            status_code=302,
        )
    domain = _finance_domain_filter(request, user)
    if domain is not None:
        allowed_method_ids = {
            m.id for m in list_payment_methods_for_pay(db, only_active=True, domain=domain)
        }
        if pm_id not in allowed_method_ids:
            if invoice_image_filename:
                _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
            return RedirectResponse(
                "/admin/expenses?error="
                + quote("اختر خزينة تابعة لنفس مجال العمل الحالي."),
                status_code=302,
            )
    created_at = _parse_datetime_local(purchase_date)
    try:
        record_expense(
            db,
            payment_method_id=pm_id,
            amount=amt,
            expense_category=expense_category,
            supplier=supplier,
            note=note,
            user_id=user.id,
            created_at=created_at,
            supplier_invoice_ref=supplier_invoice_ref or None,
            invoice_image_filename=invoice_image_filename,
            filter_domain=domain,
            business_domain=(form.get("business_domain") or "").strip() or None,
        )
        db.commit()
    except PaymentsError as e:
        db.rollback()
        if invoice_image_filename:
            _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
        return RedirectResponse(
            "/admin/expenses?error=" + quote(str(e)),
            status_code=302,
        )
    return RedirectResponse("/admin/expenses?saved=1", status_code=302)


@expenses_router.post("/{pid}/delete", response_class=HTMLResponse)
def expenses_delete(
    pid: int,
    db: DBSession,
    _: User = Depends(_purchase_finance_perm),
):
    delete_purchase(db, pid)
    db.commit()
    return RedirectResponse("/admin/expenses", status_code=302)


# ============================================================
# Admin: Assets / Tools (consumed by company, not for sale)
# ============================================================
assets_router = APIRouter(prefix="/admin/assets", tags=["assets"])


@assets_router.get("", response_class=HTMLResponse)
def assets_list(
    request: Request,
    db: DBSession,
    _: User = Depends(_purchase_finance_perm),
    start: str | None = Query(None),
    end: str | None = Query(None),
    error: str | None = Query(None),
    saved: int = Query(0),
):
    s, e = _month_default_range()
    s_user = _parse_date(start)
    e_user = _parse_date(end)
    if s_user is not None:
        s = s_user
    if e_user is not None:
        e = e_user
    items = list_asset_purchases_in_period(
        db, s, e, fixed_only=True
    )
    total = sum((p.amount for p in items), Decimal("0"))
    from modules.dashboard_notify.constants import ASSETS
    from modules.dashboard_notify.service import resolve_activity

    resolve_activity(db, ASSETS)
    db.commit()
    return templates.TemplateResponse(
        "admin_assets_list.html",
        {
            "request": request,
            "items": items,
            "total": total,
            "start": s,
            "end": e,
            "error": error,
            "saved": bool(saved),
        },
    )


@assets_router.get("/new", response_class=HTMLResponse)
def assets_new_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_purchase_finance_perm),
    error: str | None = Query(None),
):
    methods = list_payment_methods_main_treasury_for_pay(db, only_active=True)
    return templates.TemplateResponse(
        "admin_asset_new.html",
        {
            "request": request,
            "methods": methods,
            "error": error,
            "today": now_local().strftime("%Y-%m-%dT%H:%M"),
        },
    )


@assets_router.post("/new", response_class=HTMLResponse)
async def assets_create(
    request: Request,
    db: DBSession,
    user: User = Depends(_purchase_finance_perm),
):
    form = await request.form()
    pm_raw = (form.get("payment_method_id") or "").strip()
    supplier = (form.get("supplier") or "").strip()
    note = (form.get("note") or "").strip()
    purchase_date_raw = (form.get("purchase_date") or "").strip()
    supplier_invoice_ref = (form.get("supplier_invoice_ref") or "").strip()

    invoice_image_filename: str | None = None
    inv_upload = form.get("invoice_image")
    if isinstance(inv_upload, UploadFile) and inv_upload.filename:
        try:
            invoice_image_filename = save_purchase_invoice_image(
                inv_upload, _PAYMENTS_WEB_STATIC
            )
        except ValueError as exc:
            return RedirectResponse(
                "/admin/assets/new?error=" + quote(str(exc)),
                status_code=302,
            )

    try:
        pm_id = int(pm_raw)
    except (TypeError, ValueError):
        if invoice_image_filename:
            _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
        return RedirectResponse(
            "/admin/assets/new?error=" + "أسلوب الدفع غير صالح.",
            status_code=302,
        )

    names = form.getlist("item_name")
    units = form.getlist("unit")
    qtys = form.getlist("quantity")
    costs = form.getlist("unit_cost")
    lives = form.getlist("useful_life_months")
    salvages = form.getlist("salvage_value")
    lines: list[tuple[str, str | None, Decimal, Decimal, int, Decimal]] = []
    for nm, un, raw_qty, raw_cost, raw_life, raw_salvage in zip(
        names, units, qtys, costs, lives, salvages
    ):
        if not (nm or "").strip():
            continue
        try:
            qty = Decimal((raw_qty or "0").strip() or "0")
            cost = Decimal((raw_cost or "0").strip() or "0")
            salvage = Decimal((raw_salvage or "0").strip() or "0")
        except (InvalidOperation, ValueError):
            if invoice_image_filename:
                _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
            return RedirectResponse(
                "/admin/assets/new?error=" + "بيانات بند غير صالحة.",
                status_code=302,
            )
        try:
            life = int((raw_life or "0").strip() or "0")
        except ValueError:
            life = 0
        if life < 0:
            life = 0
        if qty > 0:
            if life <= 0:
                if invoice_image_filename:
                    _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
                return RedirectResponse(
                    "/admin/assets/new?error="
                    + quote("الأصول الثابتة تتطلب عمراً إنتاجياً — للمستلزمات استخدم «أدوات واستهلاكات»."),
                    status_code=302,
                )
            lines.append(
                (nm.strip(), (un or "").strip() or None, qty, cost, life, salvage)
            )

    if not lines:
        if invoice_image_filename:
            _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
        return RedirectResponse(
            "/admin/assets/new?error=" + "أضف بنداً واحداً على الأقل.",
            status_code=302,
        )

    created_at = _parse_datetime_local(purchase_date_raw)
    try:
        record_asset_purchase(
            db,
            payment_method_id=pm_id,
            supplier=supplier,
            note=note,
            lines=lines,
            user_id=user.id,
            created_at=created_at,
            supplier_invoice_ref=supplier_invoice_ref or None,
            invoice_image_filename=invoice_image_filename,
        )
        db.commit()
    except PaymentsError as e:
        db.rollback()
        if invoice_image_filename:
            _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
        return RedirectResponse(
            "/admin/assets/new?error=" + quote(str(e)),
            status_code=302,
        )
    return RedirectResponse("/admin/assets?saved=1", status_code=302)


@assets_router.get("/{pid}", response_class=HTMLResponse)
def assets_detail(
    request: Request,
    pid: int,
    db: DBSession,
    _: User = Depends(_purchase_finance_perm),
):
    p = db.execute(
        select(Purchase)
        .where(Purchase.id == pid)
        .options(selectinload(Purchase.lines))
    ).scalar_one_or_none()
    if p is None or p.kind != PurchaseKind.ASSET:
        return RedirectResponse("/admin/assets", status_code=302)
    from modules.dashboard_notify.constants import ASSETS
    from modules.dashboard_notify.service import resolve_activity

    resolve_activity(db, ASSETS, ref_id=pid)
    db.commit()
    return templates.TemplateResponse(
        "admin_asset_detail.html",
        {"request": request, "purchase": p},
    )


@assets_router.post("/{pid}/delete", response_class=HTMLResponse)
def assets_delete(
    pid: int,
    db: DBSession,
    _: User = Depends(_purchase_finance_perm),
):
    delete_purchase(db, pid)
    db.commit()
    return RedirectResponse("/admin/assets", status_code=302)


# ============================================================
# Admin: Consumables / tools (مناديل، منظفات، أدوات مكتبية...)
# ============================================================
consumables_router = APIRouter(prefix="/admin/consumables", tags=["consumables"])


@consumables_router.get("", response_class=HTMLResponse)
def consumables_list(
    request: Request,
    db: DBSession,
    user: User = Depends(_purchase_finance_perm),
    start: str | None = Query(None),
    end: str | None = Query(None),
    category: str | None = Query(None),
    error: str | None = Query(None),
    saved: int = Query(0),
):
    from modules.payments.asset_queries import (
        list_asset_purchases_in_period,
        summarize_consumables_by_category,
    )
    from modules.payments.consumable_categories import (
        active_consumable_categories,
        consumable_category_label,
    )
    from modules.platform.business_domain import domain_label

    finance_domain = _finance_domain_filter(request, user)
    s, e = _month_default_range()
    s_user = _parse_date(start)
    e_user = _parse_date(end)
    if s_user is not None:
        s = s_user
    if e_user is not None:
        e = e_user
    cat_key = (category or "").strip()
    cat_label = None
    if cat_key:
        try:
            cat_label = consumable_category_label(db, cat_key)
        except ValueError:
            cat_key = ""
    all_items = list_asset_purchases_in_period(
        db, s, e, consumable_only=True, filter_domain=finance_domain
    )
    items = (
        list_asset_purchases_in_period(
            db,
            s,
            e,
            consumable_only=True,
            consumable_category=cat_label,
            filter_domain=finance_domain,
        )
        if cat_label
        else all_items
    )
    total = sum((p.amount for p in items), Decimal("0"))
    category_totals = summarize_consumables_by_category(all_items)
    return templates.TemplateResponse(
        "admin_consumables_list.html",
        {
            "request": request,
            "items": items,
            "total": total,
            "category_totals": category_totals,
            "categories": active_consumable_categories(db),
            "selected_category": cat_key,
            "start": s,
            "end": e,
            "error": error,
            "saved": bool(saved),
            "finance_domain_filter": finance_domain,
            "domain_label": domain_label(finance_domain) if finance_domain else "الكل",
        },
    )


@consumables_router.get("/new", response_class=HTMLResponse)
def consumables_new_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_purchase_finance_perm),
    error: str | None = Query(None),
):
    from modules.payments.consumable_categories import active_consumable_categories

    methods = list_payment_methods_main_treasury_for_pay(db, only_active=True)
    categories = active_consumable_categories(db)
    return templates.TemplateResponse(
        "admin_consumable_new.html",
        {
            "request": request,
            "methods": methods,
            "categories": categories,
            "error": error,
            "today": now_local().strftime("%Y-%m-%dT%H:%M"),
        },
    )


@consumables_router.post("/new", response_class=HTMLResponse)
async def consumables_create(
    request: Request,
    db: DBSession,
    user: User = Depends(_purchase_finance_perm),
):
    from modules.payments.consumable_categories import consumable_category_label

    form = await request.form()
    pm_raw = (form.get("payment_method_id") or "").strip()
    supplier = (form.get("supplier") or "").strip()
    note = (form.get("note") or "").strip()
    purchase_date_raw = (form.get("purchase_date") or "").strip()
    supplier_invoice_ref = (form.get("supplier_invoice_ref") or "").strip()
    category_key = (form.get("consumable_category") or "").strip()

    invoice_image_filename: str | None = None
    inv_upload = form.get("invoice_image")
    if isinstance(inv_upload, UploadFile) and inv_upload.filename:
        try:
            invoice_image_filename = save_purchase_invoice_image(
                inv_upload, _PAYMENTS_WEB_STATIC
            )
        except ValueError as exc:
            return RedirectResponse(
                "/admin/consumables/new?error=" + quote(str(exc)),
                status_code=302,
            )

    try:
        pm_id = int(pm_raw)
    except (TypeError, ValueError):
        if invoice_image_filename:
            _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
        return RedirectResponse(
            "/admin/consumables/new?error=" + "أسلوب الدفع غير صالح.",
            status_code=302,
        )

    try:
        cat_label = consumable_category_label(db, category_key)
    except ValueError as exc:
        if invoice_image_filename:
            _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
        return RedirectResponse(
            "/admin/consumables/new?error=" + quote(str(exc)),
            status_code=302,
        )

    names = form.getlist("item_name")
    units = form.getlist("unit")
    qtys = form.getlist("quantity")
    costs = form.getlist("unit_cost")
    lines: list[tuple[str, str | None, Decimal, Decimal]] = []
    for nm, un, raw_qty, raw_cost in zip(names, units, qtys, costs):
        if not (nm or "").strip():
            continue
        try:
            qty = Decimal((raw_qty or "0").strip() or "0")
            cost = Decimal((raw_cost or "0").strip() or "0")
        except (InvalidOperation, ValueError):
            if invoice_image_filename:
                _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
            return RedirectResponse(
                "/admin/consumables/new?error=" + "بيانات بند غير صالحة.",
                status_code=302,
            )
        if qty > 0:
            lines.append((nm.strip(), (un or "").strip() or None, qty, cost))

    if not lines:
        if invoice_image_filename:
            _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
        return RedirectResponse(
            "/admin/consumables/new?error=" + "أضف بنداً واحداً على الأقل.",
            status_code=302,
        )

    created_at = _parse_datetime_local(purchase_date_raw)
    from modules.platform.business_domain import resolve_record_business_domain

    finance_domain = _finance_domain_filter(request, user)
    business_domain = resolve_record_business_domain(
        finance_domain, None, allow_shared=False
    )
    try:
        record_consumable_purchase(
            db,
            payment_method_id=pm_id,
            supplier=supplier,
            note=note,
            consumable_category=cat_label,
            lines=lines,
            user_id=user.id,
            created_at=created_at,
            supplier_invoice_ref=supplier_invoice_ref or None,
            invoice_image_filename=invoice_image_filename,
            business_domain=business_domain,
        )
        db.commit()
    except PaymentsError as e:
        db.rollback()
        if invoice_image_filename:
            _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
        return RedirectResponse(
            "/admin/consumables/new?error=" + quote(str(e)),
            status_code=302,
        )
    return RedirectResponse("/admin/consumables?saved=1", status_code=302)


@consumables_router.get("/{pid}", response_class=HTMLResponse)
def consumables_detail(
    request: Request,
    pid: int,
    db: DBSession,
    _: User = Depends(_purchase_finance_perm),
):
    from modules.payments.asset_queries import purchase_is_consumable_only

    p = db.execute(
        select(Purchase)
        .where(Purchase.id == pid)
        .options(selectinload(Purchase.lines))
    ).scalar_one_or_none()
    if p is None or p.kind != PurchaseKind.ASSET or not purchase_is_consumable_only(p):
        return RedirectResponse("/admin/consumables", status_code=302)
    return templates.TemplateResponse(
        "admin_asset_detail.html",
        {"request": request, "purchase": p, "is_consumable": True},
    )


@consumables_router.post("/{pid}/delete", response_class=HTMLResponse)
def consumables_delete(
    pid: int,
    db: DBSession,
    _: User = Depends(_purchase_finance_perm),
):
    delete_purchase(db, pid)
    db.commit()
    return RedirectResponse("/admin/consumables", status_code=302)


# ============================================================
# Admin: Recurring fixed costs (لتحليل التعادل CVP)
# ============================================================
recurring_router = APIRouter(prefix="/admin/recurring-costs", tags=["recurring-costs"])


@recurring_router.get("", response_class=HTMLResponse)
def recurring_list(
    request: Request,
    db: DBSession,
    user: User = Depends(_purchase_finance_perm),
    error: str | None = Query(None),
    saved: int = Query(0),
):
    from modules.payments.daily_burden import (
        category_label,
        compute_daily_burden,
        list_recurring_costs,
    )
    from modules.payments.models import RecurringCostCategory
    from modules.platform.business_domain import domain_label

    domain = _finance_domain_filter(request, user)
    items = list_recurring_costs(db, only_active=False, domain=domain)
    burden = compute_daily_burden(db, domain=domain)
    categories = [
        (c.value, category_label(c)) for c in RecurringCostCategory
    ]
    return templates.TemplateResponse(
        "admin_recurring_costs.html",
        {
            "request": request,
            "items": items,
            "burden": burden,
            "categories": categories,
            "category_label": category_label,
            "error": error,
            "saved": bool(saved),
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "الكل",
            "domain_choices": _domain_choices(),
        },
    )


@recurring_router.post("/save", response_class=HTMLResponse)
async def recurring_save(
    request: Request,
    db: DBSession,
    user: User = Depends(_purchase_finance_perm),
):
    from modules.payments.daily_burden import upsert_recurring_cost
    from modules.platform.business_domain import BusinessDomain

    form = await request.form()
    rc_raw = (form.get("id") or "").strip()
    name = (form.get("name_ar") or "").strip()
    cat = (form.get("category") or "OTHER").strip()
    amt_raw = (form.get("monthly_amount") or "0").strip()
    is_active = (form.get("is_active") or "1") in ("1", "on", "true")
    notes = (form.get("notes") or "").strip()
    domain_raw = (form.get("business_domain") or "").strip().lower()
    filter_domain = _finance_domain_filter(request, user)
    if filter_domain is not None:
        business_domain = filter_domain.value
    elif domain_raw in ("restaurant", "hotel", "shared"):
        business_domain = domain_raw
    else:
        business_domain = BusinessDomain.RESTAURANT.value
    rc_id: int | None = None
    if rc_raw:
        try:
            rc_id = int(rc_raw)
        except ValueError:
            rc_id = None
    try:
        amt = Decimal(amt_raw or "0")
    except (InvalidOperation, ValueError):
        return RedirectResponse(
            "/admin/recurring-costs?error=" + "المبلغ غير صحيح", status_code=302
        )
    try:
        upsert_recurring_cost(
            db,
            rc_id=rc_id,
            name_ar=name,
            category=cat,
            monthly_amount=amt,
            is_active=is_active,
            notes=notes,
            business_domain=business_domain,
        )
        db.commit()
    except ValueError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/recurring-costs?error={e}", status_code=302
        )
    return RedirectResponse("/admin/recurring-costs?saved=1", status_code=302)


@recurring_router.post("/{rc_id}/delete", response_class=HTMLResponse)
def recurring_delete(
    rc_id: int,
    db: DBSession,
    _: User = Depends(_purchase_finance_perm),
):
    from modules.payments.daily_burden import delete_recurring_cost

    delete_recurring_cost(db, rc_id)
    db.commit()
    return RedirectResponse("/admin/recurring-costs", status_code=302)
