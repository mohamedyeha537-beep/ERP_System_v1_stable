from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from starlette.datastructures import UploadFile

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import (
    PAYMENTS_MANAGE,
    PURCHASES_MANAGE,
    SALES_CREATE,
)
from modules.catalog.models import Product
from modules.catalog.service import list_stockable_products
from modules.catalog.uploads import (
    save_purchase_bank_payment_receipt,
    save_purchase_invoice_image,
)
from modules.inventory.service import InsufficientStock
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
    list_payment_methods_for_receive,
    list_purchases,
    purchase_outstanding,
    record_asset_purchase,
    record_expense,
    record_inventory_purchase,
    record_purchase_payment,
    record_sale_payment,
    sum_purchase_payments,
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


@router.get("", response_class=HTMLResponse)
def admin_payment_methods(
    request: Request,
    db: DBSession,
    _: User = Depends(_admin_pm),
):
    methods = list_payment_methods(db, only_active=False)
    return templates.TemplateResponse(
        "admin_payment_methods.html",
        {
            "request": request,
            "methods": methods,
            "kinds": [(k.value, lbl) for k, lbl in _KIND_LABELS.items()],
            "payment_method_allow_delete": payment_method_allow_delete,
            "owner_equity_name": OWNER_EQUITY_PM_NAME,
            "error_message": None,
        },
    )


def _parse_kind(raw: str) -> PaymentMethodKind:
    try:
        return PaymentMethodKind(raw)
    except ValueError:
        return PaymentMethodKind.BANK


def _render_pm_page(request: Request, db, error: str, status_code: int = 400):
    methods = list_payment_methods(db, only_active=False)
    return templates.TemplateResponse(
        "admin_payment_methods.html",
        {
            "request": request,
            "methods": methods,
            "kinds": [(k.value, lbl) for k, lbl in _KIND_LABELS.items()],
            "payment_method_allow_delete": payment_method_allow_delete,
            "owner_equity_name": OWNER_EQUITY_PM_NAME,
            "error_message": error,
        },
        status_code=status_code,
    )


@router.post("/add", response_class=HTMLResponse)
def admin_payment_methods_add(
    request: Request,
    db: DBSession,
    _: User = Depends(_admin_pm),
    name_ar: str = Form(...),
    kind: str = Form("BANK"),
    sort_order: str = Form("0"),
    can_receive: str = Form(""),
    can_pay: str = Form(""),
    can_fund: str = Form(""),
    show_on_dashboard: str = Form(""),
):
    try:
        pk = _parse_kind(kind)
        create_payment_method(
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
        )
        db.commit()
    except PaymentsError as e:
        db.rollback()
        return _render_pm_page(request, db, str(e))
    except ValueError:
        db.rollback()
        return _render_pm_page(request, db, "ترتيب غير صالح.")
    return RedirectResponse("/admin/payment-methods", status_code=302)


@router.post("/{pm_id}/update", response_class=HTMLResponse)
def admin_payment_methods_update(
    request: Request,
    pm_id: int,
    db: DBSession,
    _: User = Depends(_admin_pm),
    name_ar: str = Form(...),
    kind: str = Form("BANK"),
    sort_order: str = Form("0"),
    is_active: str = Form(""),
    can_receive: str = Form(""),
    can_pay: str = Form(""),
    can_fund: str = Form(""),
    show_on_dashboard: str = Form(""),
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
        update_payment_method(db, pm_id, **upd)
        db.commit()
    except PaymentsError as e:
        db.rollback()
        return _render_pm_page(request, db, str(e))
    except ValueError:
        db.rollback()
        return _render_pm_page(request, db, "قيم غير صالحة.")
    return RedirectResponse("/admin/payment-methods", status_code=302)


@router.post("/{pm_id}/delete", response_class=HTMLResponse)
def admin_payment_methods_delete(
    request: Request,
    pm_id: int,
    db: DBSession,
    _: User = Depends(_admin_pm),
):
    try:
        delete_payment_method(db, pm_id)
        db.commit()
    except PaymentsError as e:
        db.rollback()
        return _render_pm_page(request, db, str(e))
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


@checkout_router.get("/checkout", response_class=HTMLResponse)
def pos_checkout_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_pos_perm),
    error: str | None = Query(None),
):
    gr = require_open_pos_shift(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    sale = _get_draft_for_checkout(request, db, user)
    if sale is None:
        return RedirectResponse(
            "/pos?ctx_err=" + quote("اختر طلباً من القائمة أو أنشئ طلباً جديداً."),
            status_code=302,
        )
    loaded = load_sale_with_lines(db, sale.id)
    if loaded:
        sale = loaded
    if not sale.lines:
        return RedirectResponse(
            "/pos?ctx_err=" + quote("الطلب الحالي فارغ. أضف أصنافاً أو اختر طلباً آخر."),
            status_code=302,
        )
    from modules.sales.models import SaleContext

    if sale.context_type == SaleContext.EXTERNAL and not sale.customer_id:
        return RedirectResponse(
            "/pos?ctx_err="
            + quote("رقم هاتف العميل مطلوب للطلب الخارجي قبل الدفع."),
            status_code=302,
        )
    if (sale.context_type == SaleContext.TABLE) and (not sale.table_id):
        return RedirectResponse(
            "/pos?ctx_err=اختر الطاولة أولاً قبل إتمام البيع، أو بدّل الطلب إلى «خارجي».",
            status_code=302,
        )
    methods = list_payment_methods_for_receive(db, only_active=True)
    sorted_lines = sort_sale_lines_for_display(db, list(sale.lines))

    # خيارات حساب الغرف للفنادق (للسماح بالاختيار من checkout أيضاً)
    from modules.authz.permissions import HOTEL_CHARGE
    from modules.authz.service import user_has_permission
    from modules.customers.service import get_customer
    from modules.delivery.service import get_default_cash_method
    from modules.hotel.service import get_room, list_rooms

    can_charge_room = user_has_permission(user, HOTEL_CHARGE)
    hotel_rooms = list_rooms(db, only_active=True) if can_charge_room else []

    # حدّد سياق الفاتورة (TABLE/ROOM/EXTERNAL) — يحدد الواجهة المعروضة
    ctx = sale.context_type.value if sale.context_type else "TABLE"
    pre_room = None
    pre_room_guest = ""
    customer = None
    delivery_cash_method = None
    if ctx == "ROOM":
        sess_room_id = request.session.get("draft_room_id")
        if sess_room_id:
            pre_room = get_room(db, int(sess_room_id))
        pre_room_guest = request.session.get("draft_room_guest", "") or ""
    if sale.customer_id:
        customer = get_customer(db, sale.customer_id)
    if (
        ctx == "EXTERNAL"
        and getattr(sale, "external_order_type", None) is not None
        and sale.external_order_type.value == "DELIVERY"
    ):
        delivery_cash_method = get_default_cash_method(db)

    return templates.TemplateResponse(
        "pos_checkout.html",
        {
            "request": request,
            "sale": sale,
            "cart_lines": sorted_lines,
            "methods": methods,
            "error": error,
            "can_charge_room": can_charge_room,
            "hotel_rooms": hotel_rooms,
            "ctx": ctx,
            "pre_room": pre_room,
            "pre_room_guest": pre_room_guest,
            "customer": customer,
            "delivery_cash_method": delivery_cash_method,
        },
    )


@checkout_router.post("/checkout", response_class=HTMLResponse)
async def pos_checkout_submit(
    request: Request,
    db: DBSession,
    user: User = Depends(_pos_perm),
):
    """منطق checkout موحَّد:

    - pay_mode=room (أو context=ROOM): يفتح حساب غرفة بدون دفع.
    - غير ذلك: دفع فوري + (إن كان context=EXTERNAL وللعميل هاتف) منح نقاط ولاء.
    """
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

    sale = _get_draft_for_checkout(request, db, user)
    if sale is None:
        return RedirectResponse(
            "/pos?ctx_err=" + quote("لا يوجد طلب نشط."),
            status_code=302,
        )
    loaded_chk = load_sale_with_lines(db, sale.id)
    if loaded_chk:
        sale = loaded_chk
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

    # السياق الحالي للفاتورة
    ctx = sale.context_type.value if sale.context_type else "TABLE"
    is_room_mode = (pay_mode == "room") or (ctx == "ROOM")

    # حماية: لا يُسمح بإتمام طلب «طاولة» بلا طاولة محددة.
    if sale.context_type == SaleContext.TABLE and not sale.table_id:
        return RedirectResponse(
            "/pos?ctx_err=يجب اختيار الطاولة التي سينزل عليها الطلب. وإذا لم تكن طاولة فاختر «خارجي».",
            status_code=302,
        )

    # ===== فرع 1: قيد على حساب غرفة فندق =====
    if is_room_mode:
        from modules.authz.permissions import HOTEL_CHARGE
        from modules.authz.service import user_has_permission
        from modules.hotel.service import HotelError, open_room_charge

        if not user_has_permission(user, HOTEL_CHARGE):
            return RedirectResponse(
                "/pos/checkout?error=" + quote("ليست لديك صلاحية القيد على غرفة."),
                status_code=302,
            )
        # رقم الغرفة: يفضّل من النموذج، ثم من الـ session (إن اختير من شريط POS)
        room_id_raw = (hotel_room_id or "").strip()
        if not room_id_raw:
            sess_rid = request.session.get("draft_room_id")
            room_id_raw = str(sess_rid) if sess_rid else ""
        try:
            room_id = int(room_id_raw)
        except (TypeError, ValueError):
            return RedirectResponse(
                "/pos/checkout?error=" + quote("اختر الشقة أولاً."),
                status_code=302,
            )

        # اسم النزيل: يفضّل من النموذج، ثم من session
        guest_name = (hotel_guest_name or "").strip()
        if not guest_name:
            guest_name = (
                request.session.get("draft_room_guest", "") or ""
            ).strip()
        if not guest_name:
            return RedirectResponse(
                "/pos/checkout?error=" + quote("اسم النزيل مطلوب."),
                status_code=302,
            )

        try:
            complete_sale(db, sale.id, user.id, pos_shift_id=active_shift.id)
            sale.context_type = SaleContext.ROOM
            open_room_charge(
                db,
                sale_id=sale.id,
                room_id=room_id,
                guest_name=guest_name,
                note=hotel_note,
                user_id=user.id,
            )
            db.commit()
        except InsufficientStock as e:
            db.rollback()
            return RedirectResponse(
                f"/pos/checkout?error={quote(str(e))}", status_code=302
            )
        except (SalesError, HotelError) as e:
            db.rollback()
            return RedirectResponse(
                f"/pos/checkout?error={quote(str(e))}", status_code=302
            )

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
            from infra.background import run_in_background
            from modules.kds.service import dispatch_pending_whatsapp_tickets

            run_in_background(
                dispatch_pending_whatsapp_tickets,
                sale_id_for_bg,
                name="kds-whatsapp",
            )

        receipt_id = sale.id
        request.session.pop("draft_sale_id", None)
        request.session.pop("draft_room_id", None)
        request.session.pop("draft_room_guest", None)
        psid = _session_pos_shift_id_checkout(request)
        new_sale = create_draft_sale(db, user.id, pos_shift_id=psid)
        request.session["draft_sale_id"] = new_sale.id
        db.commit()
        return RedirectResponse(
            f"/pos?ok=1&receipt={receipt_id}&room=1", status_code=302
        )

    # ===== فرع 2: دفع فوري =====
    try:
        pm_id = int(payment_method_id)
    except (TypeError, ValueError):
        return RedirectResponse(
            "/pos/checkout?error=" + quote("أسلوب الدفع غير صالح."),
            status_code=302,
        )

    pm = db.get(PaymentMethod, pm_id)
    if pm is None or not pm.is_active:
        return RedirectResponse(
            "/pos/checkout?error=" + quote("أسلوب الدفع غير صالح."),
            status_code=302,
        )

    proof_fn: str | None = None
    proof_upload = form.get("bank_transfer_proof")
    if pm.kind == PaymentMethodKind.BANK:
        if isinstance(proof_upload, UploadFile) and proof_upload.filename:
            try:
                proof_fn = save_sale_payment_proof(
                    proof_upload, _PAYMENTS_WEB_STATIC
                )
            except ValueError as exc:
                return RedirectResponse(
                    "/pos/checkout?error=" + quote(str(exc)),
                    status_code=302,
                )

    try:
        complete_sale(db, sale.id, user.id, pos_shift_id=active_shift.id)
        record_sale_payment(
            db,
            sale.id,
            pm_id,
            sale.total,
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

        # منح نقاط الولاء إذا الفاتورة لعميل (سياق EXTERNAL أو يدوياً)
        if sale.customer_id:
            from modules.customers.service import (
                get_customer,
                grant_points_for_sale,
            )

            cust = get_customer(db, sale.customer_id)
            if cust is not None:
                grant_points_for_sale(
                    db,
                    customer=cust,
                    sale_id=sale.id,
                    sale_total=sale.total,
                    user_id=user.id,
                )

        db.commit()
    except InsufficientStock as e:
        db.rollback()
        _unlink_static_relative(_PAYMENTS_WEB_STATIC, proof_fn)
        return RedirectResponse(
            f"/pos/checkout?error={quote(str(e))}", status_code=302
        )
    except (SalesError, PaymentsError, DeliveryError) as e:
        db.rollback()
        _unlink_static_relative(_PAYMENTS_WEB_STATIC, proof_fn)
        return RedirectResponse(
            f"/pos/checkout?error={quote(str(e))}", status_code=302
        )

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

        if get_bool(db, "alerts_enabled", False):
            from infra.background import run_in_background, with_db
            from modules.alerts.service import send_low_stock_alert

            def _bg_alert():
                with with_db() as bg_db:
                    send_low_stock_alert(bg_db)

            run_in_background(_bg_alert, name="low-stock-alert")
    except Exception:
        pass

    receipt_id = sale.id
    request.session.pop("draft_sale_id", None)
    psid = _session_pos_shift_id_checkout(request)
    new_sale = create_draft_sale(db, user.id, pos_shift_id=psid)
    request.session["draft_sale_id"] = new_sale.id
    db.commit()
    return RedirectResponse(f"/pos?ok=1&receipt={receipt_id}", status_code=302)


# ============================================================
# Helpers
# ============================================================
def _month_default_range() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
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


# ============================================================
# Admin: Inventory purchase invoices (with line items)
# ============================================================
purchases_router = APIRouter(prefix="/admin/purchases", tags=["purchases"])
_purchases_perm = require_permission(PURCHASES_MANAGE)


@purchases_router.get("", response_class=HTMLResponse)
def purchases_list(
    request: Request,
    db: DBSession,
    _: User = Depends(_purchases_perm),
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
    items = list(
        db.scalars(
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
            .order_by(Purchase.created_at.desc(), Purchase.id.desc())
            .limit(500)
        ).all()
    )
    total = sum((p.amount for p in items), Decimal("0"))
    return templates.TemplateResponse(
        "admin_purchases_list.html",
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


@purchases_router.get("/new", response_class=HTMLResponse)
def purchases_new_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_purchases_perm),
    error: str | None = Query(None),
):
    from modules.inventory.service import get_main_warehouse, list_warehouses

    methods = list_payment_methods_for_purchase_term(db, only_active=True)
    pay_methods = list_payment_methods_for_pay(db, only_active=True)
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
            "today": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M"),
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

    pm = db.get(PaymentMethod, pm_id)
    if pm is None or not pm.is_active:
        return RedirectResponse(
            "/admin/purchases/new?error=" + quote("أسلوب الدفع غير صالح."),
            status_code=302,
        )

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
    lines: list[tuple[int, Decimal, Decimal]] = []
    for raw_pid, raw_qty, raw_cost in zip(product_ids, qtys, costs):
        if not (raw_pid or "").strip():
            continue
        try:
            pid = int(raw_pid)
            qty = Decimal((raw_qty or "0").strip() or "0")
            cost = Decimal((raw_cost or "0").strip() or "0")
        except (InvalidOperation, ValueError):
            if invoice_image_filename:
                _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
            if payment_proof_image_filename:
                _unlink_static_relative(
                    _PAYMENTS_WEB_STATIC, payment_proof_image_filename
                )
            return RedirectResponse(
                "/admin/purchases/new?error=" + quote("بيانات بند غير صالحة."),
                status_code=302,
            )
        if qty > 0:
            lines.append((pid, qty, cost))

    if not lines:
        if invoice_image_filename:
            _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
        if payment_proof_image_filename:
            _unlink_static_relative(
                _PAYMENTS_WEB_STATIC, payment_proof_image_filename
            )
        return RedirectResponse(
            "/admin/purchases/new?error=" + quote("أضف بنداً واحداً على الأقل."),
            status_code=302,
        )

    wh_raw = (form.get("warehouse_id") or "").strip()
    try:
        warehouse_id = int(wh_raw)
    except (TypeError, ValueError):
        if invoice_image_filename:
            _unlink_static_relative(_PAYMENTS_WEB_STATIC, invoice_image_filename)
        if payment_proof_image_filename:
            _unlink_static_relative(
                _PAYMENTS_WEB_STATIC, payment_proof_image_filename
            )
        return RedirectResponse(
            "/admin/purchases/new?error=" + quote("اختر المخزن الذي تُضاف إليه البضاعة."),
            status_code=302,
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
    try:
        record_inventory_purchase(
            db,
            payment_method_id=pm_id,
            supplier=supplier,
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
    _: User = Depends(_purchases_perm),
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
    pay_methods = list_payment_methods_for_pay(db, only_active=True)
    saved = request.query_params.get("saved")
    error = request.query_params.get("error")
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
            "error": error,
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
        )
        db.commit()
    except PaymentsError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/purchases/{pid}?error={quote(str(e))}",
            status_code=302,
        )
    return RedirectResponse(f"/admin/purchases/{pid}?saved=1", status_code=302)


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
    _: User = Depends(_purchases_perm),
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
    methods = list_payment_methods(db, only_active=True)
    items = list_purchases(db, s, e, kind=PurchaseKind.EXPENSE)
    total = sum((p.amount for p in items), Decimal("0"))
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
        },
    )


@expenses_router.post("/add", response_class=HTMLResponse)
async def expenses_add(
    request: Request,
    db: DBSession,
    user: User = Depends(_purchases_perm),
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
    _: User = Depends(_purchases_perm),
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
    _: User = Depends(_purchases_perm),
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
    items = list_purchases(db, s, e, kind=PurchaseKind.ASSET)
    total = sum((p.amount for p in items), Decimal("0"))
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
    _: User = Depends(_purchases_perm),
    error: str | None = Query(None),
):
    methods = list_payment_methods_for_pay(db, only_active=True)
    return templates.TemplateResponse(
        "admin_asset_new.html",
        {
            "request": request,
            "methods": methods,
            "error": error,
            "today": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M"),
        },
    )


@assets_router.post("/new", response_class=HTMLResponse)
async def assets_create(
    request: Request,
    db: DBSession,
    user: User = Depends(_purchases_perm),
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
    _: User = Depends(_purchases_perm),
):
    p = db.execute(
        select(Purchase)
        .where(Purchase.id == pid)
        .options(selectinload(Purchase.lines))
    ).scalar_one_or_none()
    if p is None or p.kind != PurchaseKind.ASSET:
        return RedirectResponse("/admin/assets", status_code=302)
    return templates.TemplateResponse(
        "admin_asset_detail.html",
        {"request": request, "purchase": p},
    )


@assets_router.post("/{pid}/delete", response_class=HTMLResponse)
def assets_delete(
    pid: int,
    db: DBSession,
    _: User = Depends(_purchases_perm),
):
    delete_purchase(db, pid)
    db.commit()
    return RedirectResponse("/admin/assets", status_code=302)


# ============================================================
# Admin: Recurring fixed costs (لتحليل التعادل CVP)
# ============================================================
recurring_router = APIRouter(prefix="/admin/recurring-costs", tags=["recurring-costs"])


@recurring_router.get("", response_class=HTMLResponse)
def recurring_list(
    request: Request,
    db: DBSession,
    _: User = Depends(_purchases_perm),
    error: str | None = Query(None),
    saved: int = Query(0),
):
    from modules.payments.daily_burden import (
        category_label,
        compute_daily_burden,
        list_recurring_costs,
    )
    from modules.payments.models import RecurringCostCategory

    items = list_recurring_costs(db, only_active=False)
    burden = compute_daily_burden(db)
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
        },
    )


@recurring_router.post("/save", response_class=HTMLResponse)
async def recurring_save(
    request: Request,
    db: DBSession,
    _: User = Depends(_purchases_perm),
):
    from modules.payments.daily_burden import upsert_recurring_cost

    form = await request.form()
    rc_raw = (form.get("id") or "").strip()
    name = (form.get("name_ar") or "").strip()
    cat = (form.get("category") or "OTHER").strip()
    amt_raw = (form.get("monthly_amount") or "0").strip()
    is_active = (form.get("is_active") or "1") in ("1", "on", "true")
    notes = (form.get("notes") or "").strip()
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
    _: User = Depends(_purchases_perm),
):
    from modules.payments.daily_burden import delete_recurring_cost

    delete_recurring_cost(db, rc_id)
    db.commit()
    return RedirectResponse("/admin/recurring-costs", status_code=302)
