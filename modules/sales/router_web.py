from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import (
    HOTEL_CHARGE,
    POS_PRINT_CHOOSE_SIZE,
    SALES_CREATE,
    SALES_EDIT_INVOICE,
    SALES_VOID_AFTER_KITCHEN,
)
from modules.authz.service import user_has_permission
from modules.catalog.models import DiningTable, Product, ProductCategory, ProductKind
from modules.catalog.service import (
    assert_product_pos_category_visible,
    assert_product_sellable,
    load_pos_hidden_category_ids,
    pos_visible_product_criteria,
)
from modules.inventory.service import InsufficientStock
from modules.pos_shifts.models import PosShift
from modules.pos_shifts.service import require_open_pos_shift
from modules.sales.models import ExternalOrderType, Sale, SaleContext, SaleSource, SaleStatus
from modules.payments.models import PaymentMethodKind
from modules.payments.service import get_sale_payment
from modules.sales.receipt_layout import build_receipt_sections, sort_sale_lines_for_display
from modules.settings.service import (
    PAPER_SIZES,
    get_paper_css,
    get_setting,
    normalize_paper,
)
from modules.printing.receipt_text import build_sale_receipt_text
from modules.printing.service import (
    enqueue_receipt_print,
    get_receipt_printer,
    receipt_silent_print_available,
)
from modules.sales.service import (
    SalesError,
    add_or_increment_line_to_sale,
    build_table_board,
    cancel_sale,
    complete_sale,
    create_draft_sale,
    find_draft_for_table,
    get_draft_sale,
    cancel_stale_empty_pos_drafts,
    cancel_unsent_drafts_for_table,
    is_meaningful_open_order,
    list_kitchen_unsent_orders,
    list_pos_open_drafts,
    load_completed_sale_for_print,
    load_sale_with_lines,
    adjust_line_quantity,
    remove_line,
    sale_pos_order_label,
    update_line_note,
    send_draft_to_kitchen,
    send_kitchen_supplement,
    table_assignment_required,
)

router = APIRouter(prefix="/pos", tags=["pos"])


def _maybe_send_kitchen_supplement(
    db: DBSession, sale: Sale, user: User
) -> str | None:
    """بعد إضافة/زيادة بند لطلب مُرسَل للمطبخ — طباعة تكميل تلقائياً."""
    if sale.sent_to_kitchen_at is None:
        return None
    loaded = load_sale_with_lines(db, sale.id) or sale
    from modules.sales.service import sale_has_pending_kitchen

    if not sale_has_pending_kitchen(loaded):
        return None
    try:
        send_kitchen_supplement(db, loaded.id, user.id)
    except InsufficientStock as e:
        return str(e)
    except SalesError as e:
        return str(e)
    return None


def _receipt_silent_print_ctx(db: DBSession) -> dict:
    from modules.printing.service import get_receipt_printer, receipt_silent_print_available

    printer = get_receipt_printer(db)
    return {
        "silent_print_enabled": receipt_silent_print_available(db),
        "receipt_printer_name": printer.name if printer else None,
    }


def _pos_receipt_paper(
    db: DBSession,
    user: User,
    *,
    paper_param: str | None = None,
    allow_admin_override: bool = False,
) -> str:
    """مقاس الفاتورة — حراري عند وجود طابعة كاشير (مثل أمر التجهيز)."""
    from modules.platform.business_domain import BusinessDomain
    from modules.printing.service import get_receipt_printer, thermal_paper_code
    from modules.settings.service import get_receipt_paper_size, normalize_paper

    admin_paper = get_receipt_paper_size(db, BusinessDomain.RESTAURANT)
    if get_receipt_printer(db) is not None:
        return normalize_paper(thermal_paper_code(db), "80mm")
    can_choose = user_has_permission(user, POS_PRINT_CHOOSE_SIZE)
    if allow_admin_override and can_choose and paper_param:
        return normalize_paper(paper_param, admin_paper)
    return admin_paper


def _safe_receipt_back_href(raw: str | None, *, default: str = "/pos") -> str:
    """يقبل مساراً داخلياً فقط لزر الرجوع من صفحة الإيصال."""
    path = (raw or "").strip()
    if not path.startswith("/") or path.startswith("//"):
        return default
    if "://" in path or "\n" in path or "\r" in path or "\\" in path:
        return default
    return path[:500]


def _receipt_back_label(back_href: str, *, pos_label: str) -> str:
    href = (back_href or "").strip()
    if href.startswith("/hotel/settle/room/"):
        return "رجوع إلى تسوية الشقة"
    if href.startswith("/hotel/settle"):
        return "رجوع إلى التسويات"
    if href.startswith("/admin/hotel/"):
        return "رجوع إلى لوحة الشقق"
    return f"رجوع إلى {pos_label}"


def _room_receipt_context(db: DBSession, sale: Sale) -> dict:
    """سياق فاتورة النزيل/الشقة للطباعة النهائية."""
    if sale.context_type != SaleContext.ROOM:
        return {
            "is_room_receipt": False,
            "room_hint": None,
            "room_charge_note": None,
        }
    from modules.hotel.models import HotelRoom, RoomCharge

    rc = db.scalar(select(RoomCharge).where(RoomCharge.sale_id == sale.id))
    room_hint = None
    note = None
    if rc is not None:
        note = (rc.note or "").strip() or None
        if rc.room is not None:
            room_hint = str(rc.room.number)
        elif rc.room_id:
            rm = db.get(HotelRoom, rc.room_id)
            if rm is not None:
                room_hint = str(rm.number)
    return {
        "is_room_receipt": True,
        "room_hint": room_hint,
        "room_charge_note": note,
    }


def _room_hint_for_sale(db: DBSession, sale: Sale, request: Request | None = None) -> str | None:
    ctx = _room_receipt_context(db, sale)
    if ctx["room_hint"]:
        return ctx["room_hint"]
    if request is not None:
        rid = request.session.get("draft_room_id")
        if rid:
            from modules.hotel.models import HotelRoom

            rm = db.get(HotelRoom, int(rid))
            if rm is not None:
                return str(rm.number)
    return None


def _prebill_cashier_name(db: DBSession, user: User) -> str:
    from modules.pos_shifts.service import get_open_shift_for_user

    sh = get_open_shift_for_user(db, user.id)
    if sh is not None and getattr(sh, "employee", None) is not None:
        return sh.employee.full_name_ar
    return user.username


def _draft_prebill_allowed(sale: Sale) -> bool:
    return sale.status == SaleStatus.DRAFT


def _completed_receipt_allowed(sale: Sale) -> bool:
    return sale.status == SaleStatus.COMPLETED


def _prebill_guest_fields(
    request: Request, db: DBSession, sale: Sale
) -> dict:
    """اسم/هاتف للطباعة على أمر التجهيز (شقة أو خارجي غير توصيل)."""
    is_delivery = (
        sale.context_type == SaleContext.EXTERNAL
        and sale.external_order_type == ExternalOrderType.DELIVERY
    )
    if is_delivery:
        return {
            "is_delivery": True,
            "prebill_guest_show": False,
            "prebill_guest_name": None,
            "prebill_guest_phone": None,
        }
    name: str | None = None
    phone: str | None = None
    show = False
    if sale.customer_id:
        from modules.customers.service import get_customer

        c = get_customer(db, sale.customer_id)
        if c is not None:
            name = (c.name or "").strip() or None
            phone = (c.phone or "").strip() or None
            show = True
    elif sale.context_type == SaleContext.ROOM:
        show = False
    elif sale.context_type == SaleContext.EXTERNAL:
        show = True
    return {
        "is_delivery": False,
        "prebill_guest_show": show,
        "prebill_guest_name": name,
        "prebill_guest_phone": phone,
    }


def _format_loyalty_points_display(pts: Decimal) -> str:
    from modules.customers.service import format_loyalty_points_display

    return format_loyalty_points_display(pts)


def _receipt_delivery_context(
    db: DBSession, sale: Sale, sale_payment: SalePayment | None = None
) -> dict:
    """سياق طباعة فواتير التوصيل: زبون، دفع مصرفي، نقاط ولاء."""
    is_delivery = (
        sale.context_type == SaleContext.EXTERNAL
        and sale.external_order_type == ExternalOrderType.DELIVERY
    )
    payment_method_name = None
    payment_is_bank = False
    from modules.payments.service import list_sale_payments

    sale_payments = list_sale_payments(db, sale.id)
    method_names = [
        p.method.name_ar for p in sale_payments if p.method is not None and p.amount > 0
    ]
    if method_names:
        payment_method_name = " + ".join(dict.fromkeys(method_names))
        payment_is_bank = all(
            p.method is not None and p.method.kind == PaymentMethodKind.BANK
            for p in sale_payments
            if p.amount > 0
        )
    elif sale_payment is not None and sale_payment.method is not None:
        payment_method_name = sale_payment.method.name_ar
        payment_is_bank = sale_payment.method.kind == PaymentMethodKind.BANK
    customer = sale.customer
    if customer is None and sale.customer_id:
        from modules.customers.service import get_customer

        customer = get_customer(db, sale.customer_id)
    from modules.customers.service import build_receipt_loyalty_context

    loyalty_ctx = build_receipt_loyalty_context(db, sale, sale_payment)
    return {
        "is_delivery": is_delivery,
        "customer": customer,
        "payment_method_name": payment_method_name,
        "payment_is_bank": payment_is_bank,
        **loyalty_ctx,
    }


def _prebill_loyalty_context(db: DBSession, sale: Sale) -> dict:
    """نقاط الولاء المتوقعة قبل الدفع، لعرضها في أمر التجهيز لكل أنواع الطلبات."""
    if not sale.customer_id:
        return {
            "prebill_loyalty_show": False,
            "prebill_loyalty_customer": None,
            "prebill_loyalty_expected_display": "0",
            "prebill_loyalty_expected_dinar_display": "0",
            "prebill_loyalty_balance_display": "0",
        }
    from modules.customers.service import (
        format_loyalty_points_display,
        format_money_plain,
        get_customer,
        loyalty_settings,
        points_to_dinars,
    )

    customer = get_customer(db, int(sale.customer_id))
    settings = loyalty_settings(db)
    expected = Decimal("0")
    expected_dinar = Decimal("0")
    if customer is not None and settings.get("enabled"):
        earn_rate = Decimal(str(settings.get("earn_per_dinar") or 0))
        expected = (Decimal(str(sale.total or 0)) * earn_rate).quantize(
            Decimal("0.001")
        )
        expected_dinar = points_to_dinars(db, expected) if expected > 0 else Decimal("0")
    balance = (
        Decimal(str(customer.points_balance or 0)).quantize(Decimal("0.001"))
        if customer is not None
        else Decimal("0")
    )
    return {
        "prebill_loyalty_show": customer is not None,
        "prebill_loyalty_customer": customer,
        "prebill_loyalty_expected_display": format_loyalty_points_display(expected),
        "prebill_loyalty_expected_dinar_display": format_money_plain(expected_dinar),
        "prebill_loyalty_balance_display": format_loyalty_points_display(balance),
    }


def _active_pos_shift_or_redirect(request: Request, db: DBSession, user: User):
    g = require_open_pos_shift(request, db, user)
    if isinstance(g, RedirectResponse):
        return g
    return g


def _shift_template_kwargs(open_shift, db=None) -> dict:
    op_name = None
    if open_shift is not None and getattr(open_shift, "employee", None) is not None:
        op_name = open_shift.employee.full_name_ar
    ctx: dict = {"open_pos_shift": open_shift, "pos_operator_name": op_name}
    if db is not None and open_shift is not None:
        from modules.pos_shifts.shift_expenses import shift_expense_ui_context

        ctx.update(shift_expense_ui_context(db, open_shift.id))
    return ctx


def _external_needs_phone(sale: Sale) -> bool:
    """الطلب الخارجي يحتاج عميلاً مربوطاً قبل الدفع/الطباعة — وليس قبل إضافة الأصناف."""
    return (
        sale.context_type == SaleContext.EXTERNAL and not sale.customer_id
    )


def _external_needs_delivery_zone(sale: Sale) -> bool:
    from modules.sales.models import ExternalOrderType

    return (
        sale.context_type == SaleContext.EXTERNAL
        and sale.external_order_type == ExternalOrderType.DELIVERY
        and not sale.delivery_zone_id
    )


def _session_draft_sale_id(request: Request) -> int | None:
    raw = request.session.get("draft_sale_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _session_pos_shift_id(request: Request) -> int | None:
    raw = request.session.get("pos_shift_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


POS_REFUND_AUTH_EXP_KEY = "pos_refund_auth_exp"
POS_REFUND_AUTH_TTL_SEC = 900


def _pos_refund_auth_valid(request: Request) -> bool:
    raw = request.session.get(POS_REFUND_AUTH_EXP_KEY)
    try:
        exp = float(raw)
    except (TypeError, ValueError):
        return False
    return exp > time.time()


def _set_pos_refund_auth(request: Request) -> None:
    request.session[POS_REFUND_AUTH_EXP_KEY] = time.time() + POS_REFUND_AUTH_TTL_SEC


def _open_orders_panel_ctx(
    db, user: User, request: Request, current_sale: Sale | None
) -> dict:
    """قائمة طلبات مفتوحة فعلياً فقط (بنود أو مُرسَلة للمطبخ)."""
    shift_id = _session_pos_shift_id(request)
    orders = list_pos_open_drafts(
        db,
        user_id=user.id,
        pos_shift_id=shift_id,
        exclude_sale_id=None,
        limit=25,
    )

    from modules.sales.service import sale_pos_order_label

    rows: list[dict] = []
    for s in orders:
        st = "مُرسَل للمطبخ" if s.sent_to_kitchen_at else "قيد الإدخال"
        rows.append(
            {
                "id": s.id,
                "label": sale_pos_order_label(s),
                "total": s.total,
                "status": st,
                "active": current_sale is not None and s.id == current_sale.id,
            }
        )
    orders_hub: list = []
    saved_drivers: list = []
    treasury_pay_methods: list = []
    if shift_id is not None:
        from modules.delivery.drivers_service import list_recent_drivers

        saved_drivers = list_recent_drivers(db)
    from modules.settings.refund_auth import refund_auth_configured

    orders_modal = (request.query_params.get("orders") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    return {
        "open_orders_nav": rows,
        "orders_hub_rows": orders_hub,
        "refund_auth_configured": refund_auth_configured(db),
        "orders_modal": orders_modal,
        "refund_sale_prompt": (request.query_params.get("refund_sale") or "").strip(),
        "sale_sent_kitchen": bool(
            current_sale is not None and current_sale.sent_to_kitchen_at
        ),
        "has_active_sale": current_sale is not None,
        "saved_drivers": saved_drivers,
        "treasury_pay_methods": treasury_pay_methods,
    }


def _pos_orders_hub_ctx(
    db: DBSession,
    user: User,
    request: Request,
    *,
    shift_id: int | None,
    current_sale_id: int | None = None,
) -> dict:
    from modules.payments.service import (
        is_treasury_wallet_method,
        list_payment_methods_for_pay,
    )
    from modules.sales.pos_orders_hub import (
        list_pos_hub_shift_options,
        list_pos_shift_orders_hub,
    )
    from modules.settings.refund_auth import refund_auth_configured

    web_chat_pending: list = []
    try:
        from modules.messaging.chat_order_service import list_pending_chat_orders
        from modules.messaging.web_chat_service import web_chat_enabled

        if web_chat_enabled(db):
            web_chat_pending = list_pending_chat_orders(db)
    except Exception:
        web_chat_pending = []

    psid = shift_id if shift_id is not None else _session_pos_shift_id(request)
    orders_hub: list = []
    if psid is not None:
        orders_hub = list_pos_shift_orders_hub(
            db,
            pos_shift_id=psid,
            user=user,
            user_id=user.id,
            current_sale_id=current_sale_id,
        )
    treasury_pay_methods = [
        m
        for m in list_payment_methods_for_pay(db, only_active=True)
        if is_treasury_wallet_method(m)
    ]
    return {
        "orders_hub_rows": orders_hub,
        "web_chat_pending_orders": web_chat_pending,
        "web_chat_delivery_pending": [
            o for o in web_chat_pending if (o.fulfillment or "").upper() == "DELIVERY"
        ],
        "web_chat_pickup_pending": [
            o
            for o in web_chat_pending
            if (o.fulfillment or "PICKUP").upper() != "DELIVERY"
        ],
        "orders_hub_shift_options": list_pos_hub_shift_options(
            db, current_shift_id=_session_pos_shift_id(request)
        ),
        "orders_hub_shift_id": psid,
        "refund_auth_configured": refund_auth_configured(db),
        "treasury_pay_methods": treasury_pay_methods,
    }


def _pos_orders_redirect_url(**params: str) -> str:
    q = ["orders=1"]
    for k, v in params.items():
        if v:
            q.append(f"{k}={quote(str(v))}")
    return "/pos?" + "&".join(q)


def _pending_order_banner(db: DBSession, sale_id: int) -> dict | None:
    """بيانات تنبيه معلوماتي لطلب سابق لم يُكتمل (بعد فتح طلب جديد)."""
    sale = load_sale_with_lines(db, sale_id) or get_draft_sale(db, sale_id)
    if sale is None or not is_meaningful_open_order(sale):
        return None
    ctx = sale.context_type.value if sale.context_type else "TABLE"
    if sale.sent_to_kitchen_at is None:
        hint = "لم يُرسل للمطبخ بعد — يمكنك فتحه وإرساله أو إلغاؤه."
    elif ctx == "ROOM":
        hint = "بانتظار «قيد على حساب الشقة» — افتحه لإتمام القيد."
    else:
        hint = "بانتظار التحصيل — افتحه لإتمام البيع."
    return {
        "id": sale.id,
        "hint": hint,
        "context": ctx,
        "sent_kitchen": bool(sale.sent_to_kitchen_at),
    }


from modules.receipt_whatsapp.service import pos_whatsapp_sidebar_ctx


def _pos_sidebar_ctx(
    db: DBSession, user: User, request: Request, sale: Sale | None
) -> dict:
    """سياق مشترك لقالب الفاتورة (شبكة الطاولات + تحذيرات المطبخ)."""
    from modules.sales.line_notes import get_modifier_presets

    tables = _active_tables(db)
    psid = _session_pos_shift_id(request)
    if sale is not None and not sale.lines:
        loaded_sale = load_sale_with_lines(db, sale.id)
        if loaded_sale is not None:
            sale = loaded_sale
    cur_table = sale.table_id if sale else None
    cur_sale = sale.id if sale else None
    from modules.sales.order_policy import (
        load_order_policy,
        payment_block_reason,
        room_charge_block_reason,
    )
    from modules.sales.order_pipeline import _kitchen_agg, build_pipeline_view
    from modules.sales.pos_order_status import list_order_status_rows

    policy = load_order_policy(db)
    pay_block = payment_block_reason(db, sale, policy) if sale else None
    charge_block = room_charge_block_reason(db, sale, policy) if sale else None
    pipeline = None
    if sale is not None:
        agg = _kitchen_agg(db, [sale.id]).get(sale.id, {})
        from modules.delivery.drivers_service import get_handoff

        handoff = get_handoff(db, sale.id)
        pipeline = build_pipeline_view(db, sale, kitchen_agg=agg, handoff=handoff)

    from modules.authz.permissions import HOTEL_CHARGE, SALES_VOID_AFTER_KITCHEN
    from modules.sales.void_service import void_supervisor_configured

    web_chat_pending: list = []
    try:
        from modules.messaging.chat_order_service import list_pending_chat_orders
        from modules.messaging.web_chat_service import web_chat_enabled

        if web_chat_enabled(db):
            web_chat_pending = list_pending_chat_orders(db)
    except Exception:
        web_chat_pending = []

    return {
        **_cart_lines_ctx(db, sale, request),
        **_open_orders_panel_ctx(db, user, request, sale),
        "modifier_pool": get_modifier_presets(db),
        "order_policy": policy.to_dict(),
        "pipeline": pipeline,
        "can_pay": pay_block is None,
        "pay_block_reason": pay_block,
        "can_charge_room": charge_block is None,
        "charge_block_reason": charge_block,
        "can_hotel_charge": user_has_permission(user, HOTEL_CHARGE),
        "can_void_after_kitchen": user_has_permission(user, SALES_VOID_AFTER_KITCHEN),
        "void_supervisor_configured": void_supervisor_configured(db),
        "order_status_rows": (
            list_order_status_rows(
                db,
                pos_shift_id=psid,
                user_id=user.id,
                current_sale_id=cur_sale,
                room_session_ok=bool(request.session.get("draft_room_id")),
            )
            if psid is not None
            else []
        ),
        "table_board": build_table_board(
            db,
            user_id=user.id,
            pos_shift_id=psid,
            tables=tables,
            current_sale_id=cur_sale,
            current_table_id=cur_table,
            current_sale=sale,
        ),
        "kitchen_unsent_orders": (
            list_kitchen_unsent_orders(
                db,
                user_id=user.id,
                pos_shift_id=psid,
                exclude_sale_id=(
                    int(request.session.get("draft_sale_id"))
                    if request.session.get("draft_sale_id")
                    else None
                ),
            )
            if policy.kitchen_workflow_enabled
            else []
        ),
        "web_chat_pending_orders": web_chat_pending,
        "web_chat_delivery_pending": [
            o for o in web_chat_pending if (o.fulfillment or "").upper() == "DELIVERY"
        ],
        "web_chat_pickup_pending": [
            o
            for o in web_chat_pending
            if (o.fulfillment or "PICKUP").upper() != "DELIVERY"
        ],
        **pos_whatsapp_sidebar_ctx(db, sale),
    }


def _cart_lines_ctx(db, sale: Sale | None, request: Request | None = None) -> dict:
    base = {
        "tables": _active_tables(db),
        "rooms": _active_rooms(db),
        "delivery_zones": [],
        "customer": None,
        "session_room_id": None,
        "session_room_guest": "",
        "session_room_phone": "",
    }
    if request is not None:
        try:
            base["session_room_id"] = request.session.get("draft_room_id")
            base["session_room_guest"] = (
                request.session.get("draft_room_guest") or ""
            )
            base["session_room_phone"] = (
                request.session.get("draft_room_phone") or ""
            )
        except Exception:  # noqa: BLE001
            base["session_room_id"] = None
    if sale is None:
        base["cart_lines"] = []
        return base
    from modules.delivery.service import list_zones

    base["delivery_zones"] = list_zones(db, only_active=True)
    base["cart_lines"] = sort_sale_lines_for_display(db, list(sale.lines))
    from modules.customers.service import loyalty_settings

    base["loyalty"] = loyalty_settings(db)
    if sale.customer_id:
        from modules.customers.service import get_customer, points_to_dinars

        base["customer"] = get_customer(db, sale.customer_id)
        if base["customer"] and Decimal(str(base["customer"].points_balance or 0)) > 0:
            base["customer_points_value"] = points_to_dinars(
                db, base["customer"].points_balance
            )
    elif request is not None:
        base["draft_customer_phone"] = (
            request.session.get("draft_customer_phone") or ""
        )
        base["draft_customer_name"] = (
            request.session.get("draft_customer_name") or ""
        )
    return base


def _active_tables(db) -> list[DiningTable]:
    return list(
        db.scalars(
            select(DiningTable)
            .where(DiningTable.is_active.is_(True))
            .order_by(DiningTable.sort_order, DiningTable.id)
        ).all()
    )


def _active_rooms(db) -> list:
    """شقق مسكونة فقط (CHECKED_IN / OCCUPIED) لسياق ROOM في الـ POS."""
    from modules.hotel.service import list_occupied_rooms_for_pos

    return list_occupied_rooms_for_pos(db)


def _get_current_draft(request: Request, db, user: User) -> Sale | None:
    """المسودة النشطة في الجلسة فقط — بدون إنشاء تلقائي."""
    raw = request.session.get("draft_sale_id")
    if raw is None:
        return None
    try:
        sid = int(raw)
    except (TypeError, ValueError):
        request.session.pop("draft_sale_id", None)
        return None
    sale = get_draft_sale(db, sid)
    if sale is None:
        request.session.pop("draft_sale_id", None)
        return None
    sr = request.session.get("pos_shift_id")
    if sr is not None:
        try:
            psid = int(sr)
            from modules.sales.order_pipeline import migrate_draft_to_shift

            if migrate_draft_to_shift(db, sale, psid):
                db.flush()
        except (TypeError, ValueError):
            pass
    return sale


def _ensure_draft_or_create(request: Request, db, user: User) -> Sale:
    """يُستخدم عند إضافة بند أو إتمام عملية — يُنشئ مسودة إن لم تكن موجودة."""
    sale = _get_current_draft(request, db, user)
    if sale is not None:
        return sale
    psid = _session_pos_shift_id(request)
    sale = create_draft_sale(db, user.id, pos_shift_id=psid)
    request.session["draft_sale_id"] = sale.id
    db.flush()
    return sale


def _parse_int(v: str | None, default: int | None = None) -> int | None:
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _category_tree_ids(db, root_id: int) -> set[int]:
    """معرّفات الفئة الجذر وجميع الفروع تحتها (لعدّ المنتجات)."""
    ids: set[int] = {root_id}
    stack = [root_id]
    while stack:
        pid = stack.pop()
        for cid in db.scalars(select(ProductCategory.id).where(ProductCategory.parent_id == pid)):
            if cid not in ids:
                ids.add(cid)
                stack.append(cid)
    return ids


def _root_product_counts(
    db, roots: list[ProductCategory], pos_crit: tuple
) -> dict[int, int]:
    """عدد المنتجات تحت كل جذر — استعلامان بدل N+1 لكل تصنيف."""
    if not roots:
        return {}
    cat_rows = db.execute(
        select(ProductCategory.id, ProductCategory.parent_id)
    ).all()
    children_of: dict[int | None, list[int]] = {}
    for cid, pid in cat_rows:
        children_of.setdefault(pid, []).append(int(cid))

    def _tree_ids(root_id: int) -> set[int]:
        ids: set[int] = {root_id}
        stack = [root_id]
        while stack:
            pid = stack.pop()
            for cid in children_of.get(pid, ()):
                if cid not in ids:
                    ids.add(cid)
                    stack.append(cid)
        return ids

    per_cat = {
        int(cid): int(n or 0)
        for cid, n in db.execute(
            select(Product.category_id, func.count())
            .where(*pos_crit, Product.category_id.is_not(None))
            .group_by(Product.category_id)
        ).all()
        if cid is not None
    }
    out: dict[int, int] = {}
    for r in roots:
        out[r.id] = sum(per_cat.get(cid, 0) for cid in _tree_ids(r.id))
    return out


def _build_pos_panel(db, request: Request) -> dict:
    from modules.sales.line_notes import get_modifier_presets

    hidden_cats = load_pos_hidden_category_ids(db)
    pos_crit = pos_visible_product_criteria(hidden_category_ids=hidden_cats)
    modifier_pool = get_modifier_presets(db)
    roots = list(
        db.scalars(
            select(ProductCategory)
            .where(ProductCategory.parent_id.is_(None))
            .options(selectinload(ProductCategory.children))
            .order_by(ProductCategory.sort_order, ProductCategory.id)
        ).all()
    )
    roots = [r for r in roots if r.show_in_pos]
    root_product_counts = _root_product_counts(db, roots, pos_crit)
    uncat = int(
        db.scalar(
            select(func.count())
            .select_from(Product)
            .where(
                *pos_crit,
                Product.category_id.is_(None),
            )
        )
        or 0
    )

    root_q = _parse_int(request.query_params.get("root"))
    sub_q = _parse_int(request.query_params.get("sub"))

    root_ids = {r.id for r in roots}
    children: list[ProductCategory] = []
    root_sel: int | None = None
    sub_sel: int | None = None
    cat_filter: int | None = None

    if not roots and uncat == 0:
        products = list(
            db.scalars(
                select(Product)
                .where(*pos_crit)
                .order_by(Product.name_ar)
            ).all()
        )
        return {
            "roots": roots,
            "children": [],
            "root_sel": None,
            "sub_sel": None,
            "products": products,
            "uncat": uncat,
            "show_uncat_tile": False,
            "root_product_counts": root_product_counts,
            "modifier_pool": modifier_pool,
        }

    if root_q == 0 and uncat > 0:
        root_sel = 0
        sub_sel = None
        cat_filter = None
        products = list(
            db.scalars(
                select(Product)
                .where(
                    *pos_crit,
                    Product.category_id.is_(None),
                )
                .order_by(Product.name_ar)
            ).all()
        )
        return {
            "roots": roots,
            "children": [],
            "root_sel": 0,
            "sub_sel": None,
            "products": products,
            "uncat": uncat,
            "show_uncat_tile": uncat > 0,
            "root_product_counts": root_product_counts,
            "modifier_pool": modifier_pool,
        }

    if roots:
        root_sel = root_q if root_q in root_ids else roots[0].id
        children = list(
            db.scalars(
                select(ProductCategory)
                .where(
                    ProductCategory.parent_id == root_sel,
                    ProductCategory.show_in_pos.is_(True),
                )
                .order_by(ProductCategory.sort_order, ProductCategory.id)
            ).all()
        )
        child_ids = {c.id for c in children}
        if children:
            sub_sel = sub_q if sub_q in child_ids else children[0].id
            cat_filter = sub_sel
        else:
            sub_sel = None
            cat_filter = root_sel
        products = list(
            db.scalars(
                select(Product)
                .where(
                    *pos_crit,
                    Product.category_id == cat_filter,
                )
                .order_by(Product.name_ar)
            ).all()
        )
    else:
        root_sel = None
        products = list(
            db.scalars(
                select(Product)
                .where(
                    *pos_crit,
                    Product.category_id.is_(None),
                )
                .order_by(Product.name_ar)
            ).all()
        )

    return {
        "roots": roots,
        "children": children,
        "root_sel": root_sel,
        "sub_sel": sub_sel,
        "products": products,
        "uncat": uncat,
        "show_uncat_tile": uncat > 0,
        "root_product_counts": root_product_counts,
        "modifier_pool": modifier_pool,
    }


@router.get("/panel", response_class=HTMLResponse)
def pos_panel_fragment(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    ctx = _build_pos_panel(db, request)
    ctx["request"] = request
    return templates.TemplateResponse("pos_panel.html", ctx)


@router.get("/orders-hub", response_class=HTMLResponse)
def pos_orders_hub_panel(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    shift_id: int | None = Query(None),
):
    """محتوى نافذة جميع الطلبات (حسب جلسة الكاشير)."""
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return HTMLResponse("", status_code=401)
    sale = _get_current_draft(request, db, user)
    current_sale_id = sale.id if sale else None
    sid = shift_id if shift_id is not None else _session_pos_shift_id(request)
    if sid is None:
        return HTMLResponse(
            '<p class="muted" style="margin:0.75rem 0">لا توجد جلسة كاشير مفتوحة.</p>'
        )
    sh = db.get(PosShift, sid)
    if sh is None:
        return HTMLResponse(
            '<p class="muted" style="margin:0.75rem 0">الجلسة غير موجودة.</p>'
        )
    return templates.TemplateResponse(
        "pos_all_orders_panel.html",
        {
            "request": request,
            **_pos_orders_hub_ctx(
                db,
                user,
                request,
                shift_id=sid,
                current_sale_id=current_sale_id,
            ),
        },
    )


def _kds_rejected_ticket_rows(db: DBSession, user: User, request: Request, *, limit: int = 10):
    from modules.sales.models import KitchenTicket, TicketStatus

    psid = _session_pos_shift_id(request)
    stmt = (
        select(KitchenTicket)
        .join(Sale, Sale.id == KitchenTicket.sale_id)
        .where(
            KitchenTicket.status == TicketStatus.CANCELLED,
            KitchenTicket.delivery_status == "REJECTED",
            Sale.status == SaleStatus.DRAFT,
            Sale.source == SaleSource.POS,
            Sale.created_by_id == user.id,
        )
        .options(
            selectinload(KitchenTicket.sale).selectinload(Sale.table),
            selectinload(KitchenTicket.kitchen_section),
            selectinload(KitchenTicket.kitchen_department),
            selectinload(KitchenTicket.root_category),
        )
        .order_by(KitchenTicket.served_at.desc(), KitchenTicket.id.desc())
        .limit(limit)
    )
    if psid is not None:
        stmt = stmt.where(Sale.pos_shift_id == psid)
    return list(db.scalars(stmt).all())


@router.get("/kds-rejected.json")
def pos_kds_rejected_json(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return JSONResponse({"ok": False, "items": []}, status_code=401)
    tickets = _kds_rejected_ticket_rows(db, user, request)
    by_sale: dict[int, dict] = {}
    for t in tickets:
        sale = t.sale
        if sale is None:
            continue
        entry = by_sale.setdefault(
            int(sale.id),
            {
                "sale_id": int(sale.id),
                "label": sale_pos_order_label(sale),
                "table": sale.table.name_ar if sale.table else None,
                "reasons": [],
                "sections": [],
            },
        )
        if t.delivery_info and t.delivery_info not in entry["reasons"]:
            entry["reasons"].append(t.delivery_info)
        section = None
        if t.kitchen_section is not None:
            section = t.kitchen_section.name
        elif t.kitchen_department is not None:
            section = t.kitchen_department.name_ar
        elif t.root_category is not None:
            section = t.root_category.name_ar
        if section and section not in entry["sections"]:
            entry["sections"].append(section)
    return JSONResponse({"ok": True, "items": list(by_sale.values())})


@router.post("/kds-rejected/{sale_id}/cancel")
def pos_cancel_kds_rejected_order(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return JSONResponse({"ok": False, "error": "لا توجد جلسة كاشير مفتوحة."}, status_code=401)
    sale = get_draft_sale(db, sale_id)
    psid = _session_pos_shift_id(request)
    if (
        sale is None
        or sale.created_by_id != user.id
        or (psid is not None and sale.pos_shift_id != psid)
    ):
        return JSONResponse({"ok": False, "error": "الطلب غير موجود أو لا يخص هذه الجلسة."}, status_code=404)
    tickets = [t for t in _kds_rejected_ticket_rows(db, user, request, limit=50) if t.sale_id == sale_id]
    if not tickets:
        return JSONResponse({"ok": False, "error": "لا يوجد رفض مطبخ لهذا الطلب."}, status_code=409)
    reason = " | ".join(
        dict.fromkeys([(t.delivery_info or "").strip() for t in tickets if (t.delivery_info or "").strip()])
    ) or "رفض من شاشة المطبخ"
    try:
        from modules.sales.void_service import VoidError, cancel_kds_rejected_sale

        table_id = sale.table_id
        cancel_kds_rejected_sale(db, sale_id=sale_id, reason=reason, user_id=user.id)
        if table_id is not None:
            cancel_unsent_drafts_for_table(
                db,
                user_id=user.id,
                table_id=int(table_id),
                pos_shift_id=psid,
            )
        if _session_draft_sale_id(request) == sale_id:
            request.session.pop("draft_sale_id", None)
            new_sale = create_draft_sale(db, user.id, pos_shift_id=psid)
            request.session["draft_sale_id"] = new_sale.id
        db.commit()
    except VoidError as exc:
        db.rollback()
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception:
        db.rollback()
        import logging

        logging.getLogger("kds").exception("cancel rejected KDS sale failed sale_id=%s", sale_id)
        return JSONResponse({"ok": False, "error": "تعذّر إلغاء الطلب المرفوض."}, status_code=500)
    return JSONResponse({"ok": True})


@router.get("/web-chat-rails", response_class=HTMLResponse)
def pos_web_chat_rails(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    """تحديث طلبات الشات في أقسام التوصيل/الاستلام."""
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return HTMLResponse("", status_code=401)
    sale = _get_current_draft(request, db, user)
    ctx = _pos_sidebar_ctx(db, user, request, sale)
    return templates.TemplateResponse(
        "pos_web_chat_rails_fragment.html",
        {
            "request": request,
            "web_chat_delivery_pending": ctx.get("web_chat_delivery_pending") or [],
            "web_chat_pickup_pending": ctx.get("web_chat_pickup_pending") or [],
        },
    )


@router.get("/web-chat-orders-strip", response_class=HTMLResponse)
def pos_web_chat_orders_strip(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    """شريط طلبات الشات — يُحدَّث تلقائياً على شاشة الكاشير."""
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return HTMLResponse("", status_code=401)
    sale = _get_current_draft(request, db, user)
    ctx = _pos_sidebar_ctx(db, user, request, sale)
    return templates.TemplateResponse(
        "pos_web_chat_alert_strip.html",
        {
            "request": request,
            "web_chat_pending_orders": ctx.get("web_chat_pending_orders") or [],
        },
    )


@router.get("/live", response_class=HTMLResponse)
def pos_live_poll(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    """تحديث تلقائي للفاتورة وشريط الطلبات عند تغيّر حالة المطبخ."""
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return HTMLResponse("", status_code=401)
    shift_id = _session_pos_shift_id(request)
    from modules.sales.order_pipeline import migrate_user_stale_shift_drafts

    if migrate_user_stale_shift_drafts(db, user_id=user.id, pos_shift_id=shift_id):
        db.commit()
    sale = _get_current_draft(request, db, user)
    if sale is not None:
        loaded = load_sale_with_lines(db, sale.id)
        if loaded:
            sale = loaded
    return templates.TemplateResponse(
        "pos_sidebar_response.html",
        {
            "request": request,
            "sale": sale,
            "feedback": None,
            "refund_sale_prompt": "",
            **_pos_sidebar_ctx(db, user, request, sale),
        },
    )


@router.get("", response_class=HTMLResponse)
def pos_screen(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    from modules.authz.kiosk import requires_pos_pin
    from modules.pos_shifts.service import session_pos_employee_id

    if requires_pos_pin(user) and session_pos_employee_id(request) is None:
        return RedirectResponse("/pos/pin", status_code=302)
    open_shift = gr
    shift_id = _session_pos_shift_id(request)
    keep_id: int | None = None
    raw_sid = request.session.get("draft_sale_id")
    if raw_sid is not None:
        try:
            keep_id = int(raw_sid)
        except (TypeError, ValueError):
            keep_id = None
    cancel_stale_empty_pos_drafts(
        db, user_id=user.id, pos_shift_id=shift_id, keep_sale_id=keep_id
    )
    from modules.sales.order_pipeline import migrate_user_stale_shift_drafts

    if migrate_user_stale_shift_drafts(db, user_id=user.id, pos_shift_id=shift_id):
        db.flush()
    db.commit()

    sale = _get_current_draft(request, db, user)
    if sale is not None:
        loaded = load_sale_with_lines(db, sale.id)
        if loaded:
            sale = loaded

    panel = _build_pos_panel(db, request)
    pending_banner = None
    raw_pending = (request.query_params.get("pending_sale") or "").strip()
    if raw_pending.isdigit():
        pending_banner = _pending_order_banner(db, int(raw_pending))

    sidebar_feedback = (request.query_params.get("ctx_ok") or "").strip() or None
    current_sale_id = sale.id if sale else None

    receipt_whatsapp: dict = {
        "checkout_whatsapp_phone": "",
        "checkout_whatsapp_sale_id": None,
    }
    raw_receipt = (request.query_params.get("receipt") or "").strip()
    if raw_receipt.isdigit():
        from modules.receipt_whatsapp.service import sale_phone_hint

        completed = load_completed_sale_for_print(db, int(raw_receipt))
        if completed is not None:
            receipt_whatsapp = {
                "checkout_whatsapp_phone": sale_phone_hint(db, completed),
                "checkout_whatsapp_sale_id": completed.id,
            }

    return templates.TemplateResponse(
        "pos.html",
        {
            "request": request,
            "sale": sale,
            "error": None,
            "feedback": sidebar_feedback,
            "pending_order_banner": pending_banner,
            "refund_sale_prompt": (request.query_params.get("refund_sale") or "").strip(),
            **panel,
            **_pos_sidebar_ctx(db, user, request, sale),
            **_pos_orders_hub_ctx(
                db,
                user,
                request,
                shift_id=_session_pos_shift_id(request),
                current_sale_id=current_sale_id,
            ),
            **_shift_template_kwargs(open_shift, db),
            **receipt_whatsapp,
        },
    )


@router.post("/select-table", response_class=HTMLResponse)
def pos_select_table(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    table_id: str = Form(...),
):
    """اختيار طاولة: فتح الطلب الموجود عليها أو بدء مسودة جديدة."""
    from modules.sales.models import ExternalOrderType

    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    try:
        tid = int((table_id or "").strip())
    except ValueError:
        return RedirectResponse(
            "/pos?ctx_err=" + quote("رقم الطاولة غير صالح."),
            status_code=302,
        )
    t = db.get(DiningTable, tid)
    if t is None or not t.is_active:
        return RedirectResponse(
            "/pos?ctx_err=" + quote("الطاولة غير موجودة أو غير نشطة."),
            status_code=302,
        )

    psid = _session_pos_shift_id(request)
    existing = find_draft_for_table(
        db, user_id=user.id, table_id=tid, pos_shift_id=psid
    )
    if existing is not None:
        request.session["draft_sale_id"] = existing.id
        db.commit()
        return RedirectResponse("/pos", status_code=302)

    cur = _get_current_draft(request, db, user)
    if (
        cur is not None
        and is_meaningful_open_order(cur)
        and cur.table_id is not None
        and cur.table_id != tid
    ):
        cur = None
    sale = cur if cur is not None else create_draft_sale(db, user.id, pos_shift_id=psid)
    sale.context_type = SaleContext.TABLE
    sale.table_id = tid
    sale.customer_id = None
    sale.external_order_type = ExternalOrderType.PICKUP
    sale.delivery_zone_id = None
    sale.delivery_zone_name = None
    sale.delivery_fee = Decimal("0")
    request.session["draft_sale_id"] = sale.id
    request.session.pop("draft_room_id", None)
    request.session.pop("draft_room_guest", None)
    request.session.pop("draft_room_phone", None)
    db.commit()
    return RedirectResponse("/pos", status_code=302)


@router.post("/customer/unlink", response_class=HTMLResponse)
def pos_customer_unlink(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    """فك ربط العميل عن المسودة — لاختيار زبون آخر."""
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    sale = _get_current_draft(request, db, user)
    if sale is not None:
        sale.customer_id = None
        db.commit()
    request.session.pop("draft_customer_phone", None)
    request.session.pop("draft_customer_name", None)
    loaded = load_sale_with_lines(db, sale.id) if sale else None
    return templates.TemplateResponse(
        "pos_sidebar_response.html",
        {
            "request": request,
            "sale": loaded,
            "feedback": "أدخل رقم الهاتف — يُتعرّف على العملاء المخزّنين تلقائياً.",
            **_pos_sidebar_ctx(db, user, request, loaded),
        },
    )


@router.post("/lookup-phone", response_class=HTMLResponse)
def pos_lookup_phone(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    customer_phone: str = Form(""),
    customer_name: str = Form(""),
    context_type: str = Form(""),
    external_order_type: str = Form("PICKUP"),
    unlink_customer: str = Form(""),
    save_customer: str = Form(""),
):
    """ربط عميل معروف تلقائياً عند إدخال رقم هاتف مسجّل مسبقاً."""
    from modules.customers.service import (
        get_by_phone,
        get_or_create_by_phone,
        normalize_phone,
    )
    from modules.sales.models import ExternalOrderType, SaleContext

    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr

    if (unlink_customer or "").strip().lower() in ("1", "true", "yes", "on"):
        sale = _get_current_draft(request, db, user)
        if sale is not None:
            if sale.customer_id:
                from modules.customers.service import get_customer

                cust = get_customer(db, sale.customer_id)
                if cust:
                    request.session["draft_customer_phone"] = cust.phone or ""
                    request.session["draft_customer_name"] = cust.name or ""
                else:
                    request.session.pop("draft_customer_phone", None)
                    request.session.pop("draft_customer_name", None)
            else:
                request.session.pop("draft_customer_phone", None)
                request.session.pop("draft_customer_name", None)
            sale.customer_id = None
            db.commit()
        else:
            request.session.pop("draft_customer_phone", None)
            request.session.pop("draft_customer_name", None)
        loaded = load_sale_with_lines(db, sale.id) if sale else None
        return templates.TemplateResponse(
            "pos_sidebar_response.html",
            {
                "request": request,
                "sale": loaded,
                "feedback": "عدّل الرقم أو الاسم ثم احفظ.",
                **_pos_sidebar_ctx(db, user, request, loaded),
            },
        )

    sale = _ensure_draft_or_create(request, db, user)
    ctx_raw = (context_type or "").strip().upper()
    if ctx_raw == "EXTERNAL":
        sale.context_type = SaleContext.EXTERNAL
        if sale.sent_to_kitchen_at is None:
            sale.table_id = None
        ext_raw = (external_order_type or "PICKUP").strip().upper()
        sale.external_order_type = (
            ExternalOrderType.DELIVERY
            if ext_raw == ExternalOrderType.DELIVERY.value
            else ExternalOrderType.PICKUP
        )
        request.session.pop("draft_room_id", None)
        request.session.pop("draft_room_guest", None)
        request.session.pop("draft_room_phone", None)
    phone_norm = normalize_phone(customer_phone)
    nm = (customer_name or "").strip()
    save_flag = (save_customer or "").strip().lower() in ("1", "true", "yes", "on")
    feedback: str | None = None
    customer_linked = False
    if ctx_raw == "TABLE":
        if phone_norm:
            try:
                from modules.customers.service import CustomersError

                cust = get_or_create_by_phone(db, phone=phone_norm, name=nm or None)
                sale.customer_id = cust.id
                if nm and not cust.name:
                    cust.name = nm
                feedback = "✓ تم ربط العميل بالفاتورة"
            except CustomersError as exc:
                sale.customer_id = None
                feedback = str(exc)
        else:
            sale.customer_id = None
    elif phone_norm:
        from modules.customers.service import CustomersError, require_valid_phone

        try:
            valid_phone = require_valid_phone(customer_phone)
        except CustomersError as exc:
            sale.customer_id = None
            feedback = str(exc)
        else:
            existing = get_by_phone(db, valid_phone)
            if existing is not None:
                sale.customer_id = existing.id
                customer_linked = True
                if nm:
                    existing.name = nm
                label = existing.name or existing.phone
                feedback = f"✓ عميل مسجّل: {label}"
            elif ctx_raw in ("EXTERNAL", "") and (nm or save_flag):
                if save_flag and not nm:
                    sale.customer_id = None
                    feedback = "أدخل اسم العميل للعملاء الجدد ثم احفظ."
                else:
                    try:
                        c = get_or_create_by_phone(
                            db, phone=valid_phone, name=nm or None
                        )
                        sale.customer_id = c.id
                        customer_linked = True
                        feedback = "✓ تم حفظ العميل وربطه بالفاتورة"
                    except CustomersError as exc:
                        sale.customer_id = None
                        feedback = str(exc)
            elif ctx_raw in ("EXTERNAL", ""):
                sale.customer_id = None
            else:
                sale.customer_id = None
    else:
        sale.customer_id = None
        if (customer_phone or "").strip():
            feedback = "رقم الهاتف مطلوب (10 أرقام مثل 0917122552)."
    if sale.customer_id is not None:
        request.session.pop("draft_customer_phone", None)
        request.session.pop("draft_customer_name", None)
    else:
        request.session["draft_customer_phone"] = (customer_phone or "").strip()
        request.session["draft_customer_name"] = nm
    db.commit()
    if (
        request.headers.get("hx-request")
        and ctx_raw == "EXTERNAL"
        and phone_norm
        and not customer_linked
        and not feedback
    ):
        return Response(status_code=204)
    loaded = load_sale_with_lines(db, sale.id) or sale
    return templates.TemplateResponse(
        "pos_sidebar_response.html",
        {
            "request": request,
            "sale": loaded,
            "feedback": feedback,
            **_pos_sidebar_ctx(db, user, request, loaded),
        },
    )


@router.post("/add-line", response_class=HTMLResponse)
async def pos_add_line(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    product_id: int = Form(...),
    quantity: str = Form(...),
    extra_note: str = Form(""),
    direct_add: str = Form(""),
    line_id: str = Form(""),
):
    from modules.sales.line_notes import parse_preset_form_values

    form = await request.form()
    preset = parse_preset_form_values(form)
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    open_shift = gr

    sale = _ensure_draft_or_create(request, db, user)

    # حماية: في سياق TABLE يجب اختيار طاولة قبل أي إضافة.
    if table_assignment_required(sale):
        loaded = load_sale_with_lines(db, sale.id) or sale
        msg = (
            "اختر الطاولة من الشبكة أولاً قبل إضافة أي صنف. "
            "وإذا لم يكن الطلب على طاولة فحوّله إلى «خارجي»."
        )
        if request.headers.get("hx-request"):
            return templates.TemplateResponse(
                "pos_sidebar_response.html",
                {
                    "request": request,
                    "sale": loaded,
                    "feedback": msg,
                    **_pos_sidebar_ctx(db, user, request, loaded),
                },
            )
        panel = _build_pos_panel(db, request)
        return templates.TemplateResponse(
            "pos.html",
            {
                "request": request,
                "sale": loaded,
                "error": msg,
                "feedback": None,
                **panel,
                **_pos_sidebar_ctx(db, user, request, loaded),
                **_shift_template_kwargs(open_shift, db),
            },
            status_code=400,
        )

    # حماية: في سياق ROOM يجب اختيار شقة قبل أي إضافة.
    if (
        sale.context_type == SaleContext.ROOM
        and not request.session.get("draft_room_id")
    ):
        loaded = load_sale_with_lines(db, sale.id) or sale
        msg = (
            "اختر الشقة أولاً من تبويب «شقة» قبل إضافة أي صنف. "
            "هذا يضمن أن الفاتورة تُقيَّد على الحساب الصحيح."
        )
        if request.headers.get("hx-request"):
            return templates.TemplateResponse(
                "pos_sidebar_response.html",
                {
                    "request": request,
                    "sale": loaded,
                    "feedback": msg,
                    **_pos_sidebar_ctx(db, user, request, loaded),
                },
            )
        panel = _build_pos_panel(db, request)
        return templates.TemplateResponse(
            "pos.html",
            {
                "request": request,
                "sale": loaded,
                "error": msg,
                "feedback": None,
                **panel,
                **_pos_sidebar_ctx(db, user, request, loaded),
                **_shift_template_kwargs(open_shift, db),
            },
            status_code=400,
        )

    from modules.sales.line_notes import (
        compose_line_note,
        get_modifier_presets,
        get_product_modifier_presets,
    )

    product = db.get(Product, product_id)
    modifier_pool = get_modifier_presets(db)
    presets_list = get_product_modifier_presets(product, pool=modifier_pool)
    note = None
    if (direct_add or "").strip() != "1":
        note = compose_line_note(
            preset, extra_note, allowed_presets=presets_list
        )
    try:
        qty = Decimal((quantity or "1").strip())
        lid_raw = (line_id or "").strip()
        if lid_raw:
            try:
                lid = int(lid_raw)
                update_line_note(db, sale.id, lid, note)
            except (TypeError, ValueError):
                raise SalesError("بند السلة غير صالح.")
        else:
            add_or_increment_line_to_sale(
                db, sale.id, product_id, qty, user.id, line_note=note
            )
        sup_msg = _maybe_send_kitchen_supplement(db, sale, user)
        if sup_msg:
            raise SalesError(sup_msg)
        db.commit()
    except InsufficientStock as e:
        db.rollback()
        loaded = load_sale_with_lines(db, sale.id) or sale
        if request.headers.get("hx-request"):
            return templates.TemplateResponse(
                "pos_sidebar_response.html",
                {
                    "request": request,
                    "sale": loaded,
                    "feedback": str(e),
                    **_pos_sidebar_ctx(db, user, request, loaded),
                },
            )
        panel = _build_pos_panel(db, request)
        return templates.TemplateResponse(
            "pos.html",
            {
                "request": request,
                "sale": loaded,
                "error": str(e),
                "feedback": None,
                **panel,
                **_pos_sidebar_ctx(db, user, request, loaded),
                **_shift_template_kwargs(open_shift, db),
            },
            status_code=400,
        )
    except (SalesError, Exception) as e:
        db.rollback()
        panel = _build_pos_panel(db, request)
        sale = _ensure_draft_or_create(request, db, user)
        loaded = load_sale_with_lines(db, sale.id)
        if loaded:
            sale = loaded
        if request.headers.get("hx-request"):
            return templates.TemplateResponse(
                "pos_sidebar_response.html",
                {
                    "request": request,
                    "sale": sale,
                    "feedback": str(e),
                    **_pos_sidebar_ctx(db, user, request, sale),
                },
            )
        return templates.TemplateResponse(
            "pos.html",
            {
                "request": request,
                "sale": sale,
                "error": str(e),
                "feedback": None,
                **panel,
                **_pos_sidebar_ctx(db, user, request, sale),
                **_shift_template_kwargs(open_shift, db),
            },
            status_code=400,
        )
    if request.headers.get("hx-request"):
        loaded = load_sale_with_lines(db, sale.id)
        if loaded:
            sale = loaded
        return templates.TemplateResponse(
            "pos_sidebar_response.html",
            {
                "request": request,
                "sale": sale,
                "feedback": None,
                **_pos_sidebar_ctx(db, user, request, sale),
            },
        )
    return RedirectResponse("/pos", status_code=302)


@router.post("/scan", response_class=HTMLResponse)
def pos_barcode_scan(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    barcode: str = Form(...),
):
    from modules.sales.models import SaleContext

    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr

    sale = _ensure_draft_or_create(request, db, user)
    code = barcode.strip()
    if not code:
        sx = load_sale_with_lines(db, sale.id) or sale
        return templates.TemplateResponse(
            "pos_sidebar_response.html",
            {
                "request": request,
                "sale": sx,
                "feedback": "أدخل باركوداً.",
                **_pos_sidebar_ctx(db, user, request, sx),
            },
        )

    # حماية: في سياق TABLE يجب اختيار طاولة قبل أي إضافة.
    if table_assignment_required(sale):
        sx = load_sale_with_lines(db, sale.id) or sale
        return templates.TemplateResponse(
            "pos_sidebar_response.html",
            {
                "request": request,
                "sale": sx,
                "feedback": "⚠️ اختر الطاولة أولاً أو حوّل الطلب إلى «خارجي».",
                **_pos_sidebar_ctx(db, user, request, sx),
            },
        )

    # حماية: في سياق ROOM يجب اختيار شقة قبل أي إضافة.
    if (
        sale.context_type == SaleContext.ROOM
        and not request.session.get("draft_room_id")
    ):
        sx = load_sale_with_lines(db, sale.id) or sale
        return templates.TemplateResponse(
            "pos_sidebar_response.html",
            {
                "request": request,
                "sale": sx,
                "feedback": "⚠️ اختر الشقة أولاً قبل إضافة أي صنف.",
                **_pos_sidebar_ctx(db, user, request, sx),
            },
        )
    stmt = (
        select(Product)
        .where(
            Product.barcode == code,
            *pos_visible_product_criteria(
                hidden_category_ids=load_pos_hidden_category_ids(db)
            ),
        )
        .limit(1)
    )
    product = db.execute(stmt).scalar_one_or_none()
    if product is None:
        sx = load_sale_with_lines(db, sale.id) or sale
        return templates.TemplateResponse(
            "pos_sidebar_response.html",
            {"request": request, "sale": sx, "feedback": "لم يُعثر على منتج بهذا الباركود.", **_cart_lines_ctx(db, sx, request)},
        )
    try:
        add_or_increment_line_to_sale(db, sale.id, product.id, Decimal("1"), user.id)
        sup_msg = _maybe_send_kitchen_supplement(db, sale, user)
        if sup_msg:
            raise SalesError(sup_msg)
        db.commit()
    except (InsufficientStock, SalesError) as e:
        db.rollback()
        sx = load_sale_with_lines(db, sale.id) or sale
        return templates.TemplateResponse(
            "pos_sidebar_response.html",
            {
                "request": request,
                "sale": sx,
                "feedback": str(e),
                **_pos_sidebar_ctx(db, user, request, sx),
            },
        )
    loaded = load_sale_with_lines(db, sale.id)
    assert loaded is not None
    return templates.TemplateResponse(
        "pos_sidebar_response.html",
        {
            "request": request,
            "sale": loaded,
            "feedback": None,
            **_pos_sidebar_ctx(db, user, request, loaded),
        },
    )


@router.post("/adjust-line/{line_id}", response_class=HTMLResponse)
def pos_adjust_line(
    request: Request,
    line_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    delta: str = Form(...),
):
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    sale = _ensure_draft_or_create(request, db, user)
    feedback = None
    try:
        d = Decimal((delta or "0").strip())
        adjust_line_quantity(db, sale.id, line_id, d)
        if d > 0:
            sup_msg = _maybe_send_kitchen_supplement(db, sale, user)
            if sup_msg:
                raise SalesError(sup_msg)
        db.commit()
    except (SalesError, InsufficientStock) as e:
        db.rollback()
        feedback = str(e)
    if request.headers.get("hx-request"):
        loaded = load_sale_with_lines(db, sale.id)
        if loaded:
            sale = loaded
        return templates.TemplateResponse(
            "pos_sidebar_response.html",
            {
                "request": request,
                "sale": sale,
                "feedback": feedback,
                **_pos_sidebar_ctx(db, user, request, sale),
            },
        )
    return RedirectResponse("/pos", status_code=302)


@router.post("/remove-line/{line_id}", response_class=HTMLResponse)
def pos_remove_line(
    request: Request,
    line_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    sale = _ensure_draft_or_create(request, db, user)
    try:
        remove_line(db, sale.id, line_id)
        db.commit()
    except SalesError:
        db.rollback()
    if request.headers.get("hx-request"):
        loaded = load_sale_with_lines(db, sale.id)
        if loaded:
            sale = loaded
        return templates.TemplateResponse(
            "pos_sidebar_response.html",
            {
                "request": request,
                "sale": sale,
                "feedback": None,
                **_pos_sidebar_ctx(db, user, request, sale),
            },
        )
    return RedirectResponse("/pos", status_code=302)


@router.post("/void-line/{line_id}", response_class=HTMLResponse)
def pos_void_sent_line(
    request: Request,
    line_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_VOID_AFTER_KITCHEN)),
    void_reason: str = Form(""),
    supervisor_pin: str = Form(""),
):
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    sale = _get_current_draft(request, db, user)
    if sale is None:
        return RedirectResponse(
            "/pos?ctx_err=" + quote("لا توجد فاتورة نشطة."),
            status_code=302,
        )
    from modules.sales.void_service import (
        VoidError,
        authenticate_void_supervisor,
        void_pin_failure_can_request_supervisor,
        void_sent_line,
    )

    feedback = None
    sale_cancelled = False
    try:
        supervisor = authenticate_void_supervisor(db, supervisor_pin)
        void_sent_line(
            db,
            sale_id=sale.id,
            line_id=line_id,
            reason=void_reason,
            supervisor=supervisor,
            user_id=user.id,
        )
        loaded = load_sale_with_lines(db, sale.id)
        if loaded is None or loaded.status == SaleStatus.CANCELLED:
            sale_cancelled = True
            table_id = sale.table_id
            request.session.pop("draft_sale_id", None)
            psid = _session_pos_shift_id(request)
            new_sale = create_draft_sale(db, user.id, pos_shift_id=psid)
            if table_id is not None:
                new_sale.table_id = int(table_id)
            request.session["draft_sale_id"] = new_sale.id
            sale = new_sale
        elif loaded is not None:
            sale = loaded
        db.commit()
        feedback = "تم مسح الصنف وإبلاغ المطبخ ✓"
    except VoidError as e:
        msg = str(e)
        if (void_reason or "").strip() and void_pin_failure_can_request_supervisor(msg):
            loaded = load_sale_with_lines(db, sale.id)
            if loaded is not None:
                from modules.notifications.hooks import emit_pos_item_void_requested

                emit_pos_item_void_requested(
                    db,
                    sale=loaded,
                    line_id=line_id,
                    reason=void_reason,
                    cashier_user_id=user.id,
                )
                db.commit()
                feedback = "تم إرسال طلب الموافقة للمشرف ✓"
            else:
                db.rollback()
                feedback = msg
        else:
            db.rollback()
            feedback = msg
    if request.headers.get("hx-request"):
        loaded = load_sale_with_lines(db, sale.id) if not sale_cancelled else sale
        if loaded:
            sale = loaded
        return templates.TemplateResponse(
            "pos_sidebar_response.html",
            {
                "request": request,
                "sale": sale,
                "feedback": feedback,
                **_pos_sidebar_ctx(db, user, request, sale),
            },
        )
    return RedirectResponse("/pos", status_code=302)


@router.post("/void-sale", response_class=HTMLResponse)
def pos_void_sent_sale(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_VOID_AFTER_KITCHEN)),
    void_reason: str = Form(""),
    supervisor_pin: str = Form(""),
):
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    sale = _get_current_draft(request, db, user)
    if sale is None:
        return RedirectResponse(
            "/pos?ctx_err=" + quote("لا توجد فاتورة نشطة."),
            status_code=302,
        )
    from modules.sales.void_service import (
        VoidError,
        authenticate_void_supervisor,
        void_pin_failure_can_request_supervisor,
        void_sent_sale,
    )

    feedback = None
    table_id = sale.table_id
    try:
        supervisor = authenticate_void_supervisor(db, supervisor_pin)
        void_sent_sale(
            db,
            sale_id=sale.id,
            reason=void_reason,
            supervisor=supervisor,
            user_id=user.id,
        )
        request.session.pop("draft_sale_id", None)
        psid = _session_pos_shift_id(request)
        new_sale = create_draft_sale(db, user.id, pos_shift_id=psid)
        if table_id is not None:
            new_sale.table_id = int(table_id)
        request.session["draft_sale_id"] = new_sale.id
        sale = new_sale
        db.commit()
        feedback = "تم إلغاء الفاتورة وإبلاغ المطبخ ✓"
    except VoidError as e:
        msg = str(e)
        if (void_reason or "").strip() and void_pin_failure_can_request_supervisor(msg):
            loaded = load_sale_with_lines(db, sale.id)
            if loaded is not None:
                from modules.notifications.hooks import emit_pos_invoice_cancel_requested

                emit_pos_invoice_cancel_requested(
                    db,
                    sale=loaded,
                    reason=void_reason,
                    cashier_user_id=user.id,
                )
                db.commit()
                feedback = "تم إرسال طلب الموافقة للمشرف ✓"
            else:
                db.rollback()
                feedback = msg
                sale = _get_current_draft(request, db, user) or sale
        else:
            db.rollback()
            feedback = msg
            sale = _get_current_draft(request, db, user) or sale
    if request.headers.get("hx-request"):
        loaded = load_sale_with_lines(db, sale.id)
        if loaded:
            sale = loaded
        return templates.TemplateResponse(
            "pos_sidebar_response.html",
            {
                "request": request,
                "sale": sale,
                "feedback": feedback,
                **_pos_sidebar_ctx(db, user, request, sale),
            },
        )
    return RedirectResponse("/pos", status_code=302)


@router.post("/complete", response_class=HTMLResponse)
def pos_complete_redirect(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    """نقطة قديمة — تحوّل إلى صفحة اختيار الدفع لضمان تسجيل أسلوب دفع لكل بيع."""
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    return RedirectResponse("/pos/checkout", status_code=302)


@router.post("/new-order", response_class=HTMLResponse)
def pos_new_order(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    """يفتح دائماً مسودة فارغة جديدة؛ الطلب السابق يبقى محفوظاً مع تنبيه اختياري."""
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    psid = _session_pos_shift_id(request)
    pending_id: int | None = None
    cur = _get_current_draft(request, db, user)
    if cur is not None:
        loaded = load_sale_with_lines(db, cur.id) or cur
        if is_meaningful_open_order(loaded):
            pending_id = int(loaded.id)

    cancel_stale_empty_pos_drafts(
        db,
        user_id=user.id,
        pos_shift_id=psid,
        keep_sale_id=pending_id,
    )
    sale = create_draft_sale(db, user.id, pos_shift_id=psid)
    db.flush()
    request.session["draft_sale_id"] = sale.id
    request.session.pop("draft_room_id", None)
    request.session.pop("draft_room_guest", None)
    request.session.pop("draft_room_phone", None)
    db.commit()

    q = "new=1"
    if pending_id is not None:
        q += f"&pending_sale={pending_id}"
    return RedirectResponse(f"/pos?{q}", status_code=302)


@router.post("/open-order/{sale_id}", response_class=HTMLResponse)
def pos_open_order(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    s = get_draft_sale(db, sale_id)
    if s is None:
        return RedirectResponse(
            "/pos?ctx_err=" + quote("لا يمكن فتح هذا الطلب."),
            status_code=302,
        )
    psid = _session_pos_shift_id(request)
    from modules.sales.order_pipeline import migrate_draft_to_shift

    if psid is not None and migrate_draft_to_shift(db, s, psid):
        db.flush()
    elif psid is not None and s.pos_shift_id not in (None, psid):
        return RedirectResponse(
            "/pos?ctx_err=" + quote("هذا الطلب مرتبط بجلسة كاشير أخرى."),
            status_code=302,
        )
    if s.created_by_id != user.id and (psid is None or s.pos_shift_id != psid):
        from modules.messaging.chat_order_service import is_online_guest_sale

        if not is_online_guest_sale(s):
            return RedirectResponse(
                "/pos?ctx_err=" + quote("لا يمكن فتح هذا الطلب."),
                status_code=302,
            )
    psid = _session_pos_shift_id(request)
    if psid is not None and s.pos_shift_id is None:
        s.pos_shift_id = psid
        db.commit()
    request.session["draft_sale_id"] = sale_id
    return RedirectResponse("/pos", status_code=302)


@router.post("/open-order/{sale_id}/checkout", response_class=HTMLResponse)
def pos_open_order_checkout(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    s = get_draft_sale(db, sale_id)
    if s is None:
        return RedirectResponse(
            "/pos?orders=1&ctx_err=" + quote("لا يمكن فتح هذا الطلب."),
            status_code=302,
        )
    psid = _session_pos_shift_id(request)
    if psid is not None and s.pos_shift_id is None:
        s.pos_shift_id = psid
        db.flush()
    request.session["draft_sale_id"] = sale_id
    db.commit()
    return RedirectResponse("/pos/checkout", status_code=302)


@router.post("/order/{sale_id}/mark-served", response_class=HTMLResponse)
def pos_order_mark_served(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    return_to: str = Form("hub"),
):
    from datetime import datetime, timezone

    from modules.sales.order_pipeline import build_pipeline_view

    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    sale = get_draft_sale(db, sale_id)
    back_pos = (return_to or "").strip().lower() == "pos"
    dest = "/pos" if back_pos else _pos_orders_redirect_url()
    if sale is None or not sale.sent_to_kitchen_at:
        return RedirectResponse(
            dest + ("&" if "?" in dest else "?")
            + "ctx_err="
            + quote("لا يمكن تسجيل التقديم لهذا الطلب."),
            status_code=302,
        )
    if sale.served_to_customer_at:
        return RedirectResponse(dest, status_code=302)
    pipe = build_pipeline_view(db, sale)
    if pipe.key not in ("ready", "ready_serve"):
        return RedirectResponse(
            dest + ("&" if "?" in dest else "?")
            + "ctx_err="
            + quote("بانتظار جاهزية المطبخ قبل التقديم."),
            status_code=302,
        )
    sale.served_to_customer_at = datetime.now(timezone.utc)
    try:
        from modules.notifications.hooks import emit_order_delivered

        emit_order_delivered(db, sale)
    except Exception:  # noqa: BLE001
        pass
    db.commit()
    return RedirectResponse(dest, status_code=302)


@router.post("/order/{sale_id}/assign-driver", response_class=HTMLResponse)
def pos_order_assign_driver(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    driver_name: str = Form(""),
    driver_phone: str = Form(""),
    driver_note: str = Form(""),
    return_to: str = Form("hub"),
):
    from modules.delivery.drivers_service import DeliveryDriverError, assign_driver_to_sale

    back_to = (return_to or "").strip().lower()
    back_pos = back_to == "pos"
    back_checkout = back_to == "checkout"
    back_modal = back_to == "modal"

    def _redirect_err(msg: str) -> RedirectResponse:
        if back_modal:
            return RedirectResponse(
                "/pos?"
                + f"open_checkout={sale_id}&checkout_error="
                + quote(msg),
                status_code=302,
            )
        if back_checkout:
            return RedirectResponse(
                "/pos/checkout?error=" + quote(msg),
                status_code=302,
            )
        if back_pos:
            return RedirectResponse("/pos?ctx_err=" + quote(msg), status_code=302)
        return RedirectResponse(
            "/pos?orders=1&ctx_err=" + quote(msg),
            status_code=302,
        )

    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    sale = get_draft_sale(db, sale_id)
    if sale is None:
        return _redirect_err("الطلب غير موجود.")
    psid = _session_pos_shift_id(request)
    from modules.sales.order_pipeline import migrate_draft_to_shift

    if psid is not None:
        migrate_draft_to_shift(db, sale, psid)
    try:
        assign_driver_to_sale(
            db,
            sale,
            name=driver_name,
            phone=driver_phone,
            user_id=user.id,
            note=driver_note,
        )
        db.commit()
    except DeliveryDriverError as exc:
        db.rollback()
        return _redirect_err(str(exc))
    loaded = load_sale_with_lines(db, sale_id) or sale
    if request.headers.get("hx-request") and back_pos:
        return templates.TemplateResponse(
            "pos_sidebar_response.html",
            {
                "request": request,
                "sale": loaded,
                "feedback": "تم تسجيل السائق.",
                **_pos_sidebar_ctx(db, user, request, loaded),
            },
        )
    if back_modal:
        return RedirectResponse(
            "/pos?"
            + f"open_checkout={sale_id}&checkout_ok="
            + quote("تم تسجيل السائق."),
            status_code=302,
        )
    if back_checkout:
        return RedirectResponse(
            "/pos/checkout?ok=" + quote("تم تسجيل السائق."),
            status_code=302,
        )
    if back_pos:
        return RedirectResponse(
            "/pos?ctx_ok=" + quote("تم تسجيل السائق."),
            status_code=302,
        )
    return RedirectResponse("/pos", status_code=302)


@router.post("/order/{sale_id}/change-payment", response_class=HTMLResponse)
def pos_order_change_payment(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    payment_method_id: str = Form(""),
):
    from modules.payments.service import PaymentsError, correct_sale_payment_method
    from modules.sales.invoice_actions import user_can_correct_payment

    if not user_can_correct_payment(user):
        return RedirectResponse(
            "/pos?orders=1&ctx_err=" + quote("ليست لديك صلاحية تصحيح وسيلة الدفع."),
            status_code=302,
        )
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    try:
        pm_id = int(payment_method_id)
    except (TypeError, ValueError):
        return RedirectResponse(
            "/pos?orders=1&ctx_err=" + quote("اختر وسيلة دفع."),
            status_code=302,
        )
    try:
        correct_sale_payment_method(db, sale_id, pm_id, user_id=user.id)
        db.commit()
    except PaymentsError as exc:
        db.rollback()
        return RedirectResponse(
            "/pos?orders=1&ctx_err=" + quote(str(exc)),
            status_code=302,
        )
    return RedirectResponse("/pos", status_code=302)


@router.post("/refund-auth", response_class=HTMLResponse)
def pos_refund_auth(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    sale_id: str = Form(""),
    refund_code: str = Form(""),
):
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    try:
        sid = int((sale_id or "").strip())
    except ValueError:
        return RedirectResponse(
            "/pos?ctx_err=" + quote("رقم الطلب غير صالح.") + "&orders=1",
            status_code=302,
        )
    from modules.settings.refund_auth import RefundAuthError, check_refund_authorization

    try:
        check_refund_authorization(db, refund_code)
    except RefundAuthError as exc:
        return RedirectResponse(
            "/pos?ctx_err=" + quote(str(exc)) + "&orders=1&refund_sale=" + str(sid),
            status_code=302,
        )
    _set_pos_refund_auth(request)
    return RedirectResponse(f"/pos/refund/{sid}", status_code=302)


@router.get("/refund/{sale_id}", response_class=HTMLResponse)
def pos_refund_page(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    if not _pos_refund_auth_valid(request):
        return RedirectResponse(
            "/pos?ctx_err="
            + quote("أدخل كود الاسترداد أولاً من تبويب «جميع الطلبات».")
            + f"&orders=1&refund_sale={sale_id}",
            status_code=302,
        )
    from modules.payments.service import list_payment_methods_for_pay
    from modules.refunds.service import RefundsError, get_refundable_sale_summary

    try:
        summary = get_refundable_sale_summary(db, sale_id)
    except RefundsError as exc:
        return RedirectResponse(
            "/pos?ctx_err=" + quote(str(exc)) + "&orders=1",
            status_code=302,
        )
    except Exception:
        import logging

        logging.getLogger("refunds.web").exception(
            "pos refund page failed for sale_id=%s", sale_id
        )
        return RedirectResponse(
            "/pos?ctx_err="
            + quote("تعذّر فتح صفحة الاسترداد لهذه الفاتورة. راجع سجل الأخطاء.")
            + "&orders=1",
            status_code=302,
        )
    methods = list_payment_methods_for_pay(db, only_active=True)
    return templates.TemplateResponse(
        "pos_refund.html",
        {
            "request": request,
            "summary": summary,
            "methods": methods,
            "error": request.query_params.get("error"),
            "saved_return_id": request.query_params.get("saved"),
            **(_shift_template_kwargs(gr, db) if gr else {}),
        },
    )


@router.post("/refund/{sale_id}", response_class=HTMLResponse)
async def pos_refund_create(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    if not _pos_refund_auth_valid(request):
        return RedirectResponse(
            "/pos?ctx_err=" + quote("انتهت صلاحية كود الاسترداد — أدخل الكود مجدداً.")
            + f"&orders=1&refund_sale={sale_id}",
            status_code=302,
        )
    from modules.refunds.service import RefundsError, create_sale_return

    form = await request.form()
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
        try:
            qty = Decimal(str(value).strip() or "0")
        except (InvalidOperation, ValueError):
            qty = Decimal("0")
        if qty > 0:
            items.append((int(sale_line_id), qty))

    try:
        refund_method_raw = str(form.get("refund_payment_method_id") or "").strip()
        refund_method_id = int(refund_method_raw) if refund_method_raw else None
    except ValueError:
        return RedirectResponse(
            f"/pos/refund/{sale_id}?error=" + quote("أسلوب رد المبلغ غير صالح."),
            status_code=302,
        )

    try:
        sale_return = create_sale_return(
            db,
            sale_id=sale_id,
            lines=items,
            created_by_id=user.id,
            reason=str(form.get("reason") or "").strip() or None,
            note=str(form.get("note") or "").strip() or None,
            refund_payment_method_id=refund_method_id,
            allow_payment_override=False,
            approved_by_id=None,
            line_restock=line_restock or None,
        )
        db.commit()
    except RefundsError as exc:
        db.rollback()
        return RedirectResponse(
            f"/pos/refund/{sale_id}?error={quote(str(exc))}",
            status_code=302,
        )
    except Exception:
        db.rollback()
        import logging

        logging.getLogger("refunds.web").exception(
            "pos refund create failed for sale_id=%s", sale_id
        )
        return RedirectResponse(
            f"/pos/refund/{sale_id}?error="
            + quote(
                "تعذّر تسجيل الاسترداد بسبب خطأ غير متوقع. لم يتم حفظ العملية، حاول مرة أخرى أو راجع سجل الأخطاء."
            ),
            status_code=302,
        )

    return RedirectResponse(
        f"/pos/refund/{sale_id}?saved={sale_return.id}",
        status_code=302,
    )


@router.get("/refund/receipt/{sale_return_id}", response_class=HTMLResponse)
def pos_refund_receipt(
    request: Request,
    sale_return_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    autoprint: int = Query(0, ge=0, le=1),
):
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    from modules.refunds.service import get_sale_return

    sale_return = get_sale_return(db, sale_return_id)
    if sale_return is None:
        return RedirectResponse(
            "/pos?orders=1&ctx_err=" + quote("سند الاسترداد غير موجود."),
            status_code=302,
        )
    return templates.TemplateResponse(
        "refund_receipt.html",
        {
            "request": request,
            "sale_return": sale_return,
            "autoprint": autoprint,
            "can_override": False,
            "pos_receipt_context": True,
            **(_shift_template_kwargs(gr, db) if gr else {}),
        },
    )


def _send_sale_to_kitchen(
    request: Request,
    db: DBSession,
    user: User,
    sale: Sale,
    *,
    room_session_ok: bool,
) -> RedirectResponse | None:
    """إرسال مسودة للمطبخ. None = نجاح (بعد commit)."""
    from modules.sales.order_policy import load_order_policy

    if not load_order_policy(db).kitchen_workflow_enabled:
        return RedirectResponse(
            "/pos?ctx_err="
            + quote("إرسال الطلب للمطبخ متوقف من الإعدادات. اطبع أمر التجهيز مباشرة ثم أكمل التحصيل."),
            status_code=302,
        )
    if _external_needs_phone(sale):
        return RedirectResponse(
            "/pos?ctx_err="
            + quote("أدخل رقم هاتف العميل قبل الإرسال للمطبخ."),
            status_code=302,
        )
    if _external_needs_delivery_zone(sale):
        return RedirectResponse(
            "/pos?ctx_err=" + quote("اختر منطقة التوصيل قبل الإرسال."),
            status_code=302,
        )
    try:
        from modules.sales.order_pipeline import ensure_sale_shift

        psid = _session_pos_shift_id(request)
        ensure_sale_shift(db, sale, psid)
        send_draft_to_kitchen(
            db,
            sale.id,
            user.id,
            room_session_ok=room_session_ok,
            pos_shift_id=psid,
        )
        from modules.kds.service import create_tickets_for_sale

        tickets = create_tickets_for_sale(db, sale.id)
        db.commit()
        needs_wa = any(t.delivery_status == "PENDING_SEND" for t in tickets)
    except InsufficientStock as e:
        db.rollback()
        return RedirectResponse("/pos?ctx_err=" + quote(str(e)), status_code=302)
    except SalesError as e:
        db.rollback()
        return RedirectResponse("/pos?ctx_err=" + quote(str(e)), status_code=302)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        import logging

        logging.getLogger("kds").warning("send-kitchen failed: %s", exc)
        return RedirectResponse(
            "/pos?ctx_err=" + quote("تعذّر إرسال الطلب للمطبخ."),
            status_code=302,
        )
    if needs_wa:
        from infra.background import run_in_background
        from modules.kds.service import dispatch_pending_whatsapp_tickets

        run_in_background(
            dispatch_pending_whatsapp_tickets,
            sale.id,
            name="kds-whatsapp",
        )
    return None


@router.get("/order/{sale_id}/kitchen-print", response_class=HTMLResponse)
def pos_order_kitchen_print(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    sale = load_sale_with_lines(db, sale_id)
    if sale is None:
        return RedirectResponse(
            "/pos?ctx_err=" + quote("لا يمكن طباعة أمر تجهيز لهذا الطلب."),
            status_code=302,
        )
    psid = _session_pos_shift_id(request)
    if psid is not None and sale.pos_shift_id not in (None, psid):
        return RedirectResponse(
            "/pos?ctx_err=" + quote("هذا الطلب مرتبط بجلسة كاشير أخرى."),
            status_code=302,
        )
    from modules.kds.service import build_master_ticket_sections

    return templates.TemplateResponse(
        "kitchen_master_print.html",
        {
            "request": request,
            "sale": sale,
            "sections": build_master_ticket_sections(db, sale),
            "autoprint": int(request.query_params.get("autoprint", "1") or 0),
        },
    )


def _redirect_after_kitchen_send(
    sale_id: int,
    *,
    pickup_checkout: bool = False,
) -> RedirectResponse:
    """بعد الإرسال للتجهيز — العودة للكاشير وطباعة أمر التجهيز عبر iframe."""
    if pickup_checkout:
        return RedirectResponse("/pos/checkout", status_code=302)
    return RedirectResponse(
        f"/pos?ok=1&kitchen_sent={sale_id}",
        status_code=302,
    )


@router.post("/send-kitchen", response_class=HTMLResponse)
def pos_send_kitchen(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    sale = _ensure_draft_or_create(request, db, user)
    loaded = load_sale_with_lines(db, sale.id)
    if loaded:
        sale = loaded
    room_ok = (
        bool(request.session.get("draft_room_id"))
        if sale.context_type == SaleContext.ROOM
        else True
    )
    err = _send_sale_to_kitchen(request, db, user, sale, room_session_ok=room_ok)
    if err:
        return err

    from modules.sales.order_policy import load_order_policy

    policy = load_order_policy(db)
    if (
        policy.pay_redirect_after_send_pickup
        and sale.context_type == SaleContext.EXTERNAL
        and sale.external_order_type == ExternalOrderType.PICKUP
    ):
        return _redirect_after_kitchen_send(sale.id, pickup_checkout=True)

    return _redirect_after_kitchen_send(sale.id)


@router.post("/order/{sale_id}/send-kitchen", response_class=HTMLResponse)
def pos_order_send_kitchen(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    sale = get_draft_sale(db, sale_id)
    if sale is None:
        return RedirectResponse(
            "/pos?ctx_err=" + quote("لا يمكن إرسال هذا الطلب."),
            status_code=302,
        )
    loaded = load_sale_with_lines(db, sale.id)
    if loaded:
        sale = loaded
    if sale.sent_to_kitchen_at:
        return RedirectResponse("/pos", status_code=302)
    room_ok = (
        bool(request.session.get("draft_room_id"))
        if sale.context_type == SaleContext.ROOM
        else True
    )
    err = _send_sale_to_kitchen(request, db, user, sale, room_session_ok=room_ok)
    if err:
        return err
    return _redirect_after_kitchen_send(sale.id)


@router.get("/print/{sale_id}", response_class=HTMLResponse)
def pos_print_sale(
    sale_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    autoprint: int = Query(1, ge=0, le=1),
    embed: int = Query(0, ge=0, le=1),
):
    """إعادة طباعة — أمر تجهيز للمسودة، فاتورة للمكتملة."""
    sale = load_sale_with_lines(db, sale_id)
    if sale is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="الطلب غير موجود.")
    ap = "1" if autoprint else "0"
    em = "1" if embed else "0"
    q = f"autoprint={ap}&embed={em}"
    if sale.status == SaleStatus.COMPLETED:
        return RedirectResponse(
            f"/pos/receipt/{sale_id}?{q}",
            status_code=302,
        )
    if _draft_prebill_allowed(sale):
        return RedirectResponse(
            f"/pos/prebill/{sale_id}?{q}",
            status_code=302,
        )
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="لا يمكن طباعة هذا الطلب (حالة غير مدعومة).",
    )


@router.get("/prebill/{sale_id}", response_class=HTMLResponse)
def pos_prebill_print(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    autoprint: int = Query(0, ge=0, le=1),
    embed: int = Query(0, ge=0, le=1),
):
    sale = load_sale_with_lines(db, sale_id)
    if sale is None or not _draft_prebill_allowed(sale):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="الفاتورة غير موجودة أو غير متاحة.",
        )
    sections = build_receipt_sections(db, sale)
    chosen_paper = _pos_receipt_paper(db, user)
    paper_css = get_paper_css(chosen_paper)
    store_name = get_setting(db, "store_name", "نقطة البيع")
    cart_lines = sort_sale_lines_for_display(db, list(sale.lines))
    room_hint = None
    if sale.context_type == SaleContext.ROOM:
        rid = request.session.get("draft_room_id")
        if rid:
            from modules.hotel.models import HotelRoom

            rm = db.get(HotelRoom, int(rid))
            if rm is not None:
                room_hint = str(rm.number)
    guest_ctx = _prebill_guest_fields(request, db, sale)
    delivery_ctx = {}
    if guest_ctx["is_delivery"]:
        delivery_ctx = _receipt_delivery_context(db, sale, None)
    loyalty_ctx = _prebill_loyalty_context(db, sale)
    return templates.TemplateResponse(
        "receipt_prebill.html",
        {
            "request": request,
            "sale": sale,
            "sections": sections,
            "cart_lines": cart_lines,
            "autoprint": autoprint,
            "embed": embed,
            "paper": chosen_paper,
            "paper_css": paper_css,
            "store_name": store_name,
            "room_hint": room_hint,
            "cashier_name": _prebill_cashier_name(db, user),
            "silent_print_url": f"/pos/prebill/{sale.id}/silent-print",
            "return_kitchen_sent": True,
            **guest_ctx,
            **delivery_ctx,
            **loyalty_ctx,
            **_receipt_silent_print_ctx(db),
        },
    )


async def _read_silent_print_image(request: Request) -> str | None:
    ct = (request.headers.get("content-type") or "").lower()
    if "application/json" not in ct:
        return None
    try:
        body = await request.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    raw = body.get("image_png_b64")
    return str(raw).strip() if raw else None


async def _read_whatsapp_post_body(request: Request) -> tuple[str | None, str | None]:
    ct = (request.headers.get("content-type") or "").lower()
    if "application/json" not in ct:
        return None, None
    try:
        body = await request.json()
    except Exception:
        return None, None
    if not isinstance(body, dict):
        return None, None
    image = body.get("image_png_b64")
    phone = body.get("phone")
    return (
        str(image).strip() if image else None,
        str(phone).strip() if phone else None,
    )


def _silent_print_sale(
    db: DBSession,
    request: Request,
    user: User,
    sale: Sale,
    *,
    is_prebill: bool,
    image_png_b64: str | None = None,
) -> JSONResponse:
    try:
        if image_png_b64:
            raw = (image_png_b64 or "").strip()
            if len(raw) < 100:
                raise ValueError("صورة الفاتورة غير صالحة.")
            if len(raw) > 2_800_000:
                raise ValueError("حجم صورة الفاتورة كبير جداً — حدّث الصفحة وحاول مجدداً.")
            job = enqueue_receipt_print(db, sale=sale, text="", image_b64=raw)
            db.commit()
            return JSONResponse({"ok": True, "job_id": job.id, "mode": "raster"})

        store_name = get_setting(db, "store_name", "نقطة البيع")
        room_hint = _room_hint_for_sale(db, sale, request)
        guest = _prebill_guest_fields(request, db, sale)
        payment_method_name = None
        loyalty_ctx: dict = {}
        sp = None
        if not is_prebill:
            sp = get_sale_payment(db, sale.id)
            from modules.payments.service import list_sale_payments

            sale_payments = list_sale_payments(db, sale.id)
            method_names = [
                p.method.name_ar
                for p in sale_payments
                if p.method is not None and p.amount > 0
            ]
            if method_names:
                payment_method_name = " + ".join(dict.fromkeys(method_names))
            elif sp is not None and sp.method is not None:
                payment_method_name = sp.method.name_ar
            from modules.customers.service import build_receipt_loyalty_context

            loyalty_ctx = build_receipt_loyalty_context(db, sale, sp)
        if is_prebill:
            cashier_name = _prebill_cashier_name(db, user)
            doc_title = "أمر تجهيز - غير مدفوع"
        else:
            from modules.printing.doc_kind_resolve import restaurant_sale_doc_title

            cu = db.get(User, sale.created_by_id) if sale.created_by_id else None
            cashier_name = cu.username if cu else ""
            room_ctx_sp = _room_receipt_context(db, sale)
            doc_title = restaurant_sale_doc_title(
                sale,
                room_hint=room_hint or room_ctx_sp.get("room_hint"),
                is_room_receipt=bool(room_ctx_sp.get("is_room_receipt")),
            )
        printer = get_receipt_printer(db)
        paper_w = printer.paper_width if printer else 80
        text = build_sale_receipt_text(
            db,
            sale,
            store_name=store_name,
            title=doc_title,
            is_prebill=is_prebill,
            cashier_name=cashier_name,
            room_hint=room_hint,
            guest_name=guest.get("prebill_guest_name"),
            guest_phone=guest.get("prebill_guest_phone"),
            payment_method_name=payment_method_name,
            loyalty_points_display=loyalty_ctx.get("loyalty_points_earned_display", "0"),
            loyalty_points_redeemed_display=loyalty_ctx.get(
                "loyalty_points_redeemed_display", "0"
            ),
            loyalty_redeem_dinar_display=loyalty_ctx.get(
                "loyalty_redeem_dinar_display", "0"
            ),
            loyalty_balance_display=loyalty_ctx.get("loyalty_balance_display", "0"),
            loyalty_show_footer=loyalty_ctx.get("loyalty_show_footer", False),
            loyalty_show_earned=loyalty_ctx.get("loyalty_show_earned", False),
            loyalty_points_earned_dinar_display=loyalty_ctx.get(
                "loyalty_points_earned_dinar_display", "0"
            ),
            receipt_show_payment_breakdown=loyalty_ctx.get(
                "receipt_show_payment_breakdown", False
            ),
            receipt_invoice_total_display=loyalty_ctx.get(
                "receipt_invoice_total_display"
            ),
            receipt_loyalty_discount_display=loyalty_ctx.get(
                "receipt_loyalty_discount_display"
            ),
            receipt_amount_paid_display=loyalty_ctx.get("receipt_amount_paid_display"),
            receipt_amount_customer_total_display=loyalty_ctx.get(
                "receipt_amount_customer_total_display"
            ),
            paper_width=paper_w,
        )
        from modules.branding.service import get_branding

        brand = get_branding(db)
        logo_url = (brand.get("print_logo_url") or brand.get("logo_url") or "").strip()
        job = enqueue_receipt_print(
            db, sale=sale, text=text, logo_url=logo_url or None
        )
        db.commit()
        return JSONResponse({"ok": True, "job_id": job.id})
    except ValueError as e:
        db.rollback()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        db.rollback()
        return JSONResponse({"ok": False, "error": str(e)[:300]}, status_code=500)


@router.post("/prebill/{sale_id}/silent-print")
async def pos_prebill_silent_print(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    sale = load_sale_with_lines(db, sale_id)
    if sale is None or not _draft_prebill_allowed(sale):
        return JSONResponse(
            {"ok": False, "error": "الفاتورة غير موجودة أو غير متاحة."},
            status_code=404,
        )
    image_b64 = await _read_silent_print_image(request)
    return _silent_print_sale(
        db, request, user, sale, is_prebill=True, image_png_b64=image_b64
    )


@router.get("/receipt/{sale_id}", response_class=HTMLResponse)
def print_receipt(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    autoprint: int = Query(0, ge=0, le=1),
    paper: str | None = Query(None),
    orientation: str | None = Query(None),
    doc: str | None = Query(None),
    embed: int = Query(0, ge=0, le=1),
    back: str | None = Query(None),
):
    from modules.printing.doc_kind_resolve import (
        assign_sale_doc_number,
        resolve_sale_doc_kind,
        restaurant_sale_doc_title,
    )
    from modules.settings.service import (
        PAPER_ORIENTATIONS,
        get_receipt_orientation,
        normalize_orientation,
    )
    from modules.platform.business_domain import BusinessDomain

    sale = load_completed_sale_for_print(db, sale_id)
    if sale is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="الفاتورة غير موجودة أو غير مكتملة.")
    sections = build_receipt_sections(db, sale)
    can_choose = user_has_permission(user, POS_PRINT_CHOOSE_SIZE)
    chosen_paper = _pos_receipt_paper(
        db,
        user,
        paper_param=paper,
        allow_admin_override=True,
    )
    orient = normalize_orientation(
        orientation, get_receipt_orientation(db, BusinessDomain.RESTAURANT)
    )
    if chosen_paper in ("80mm", "58mm"):
        orient = "portrait"
    paper_css = get_paper_css(chosen_paper, orient)
    store_name = get_setting(db, "store_name", "نقطة البيع")
    sale_payment = get_sale_payment(db, sale.id)
    delivery_ctx = _receipt_delivery_context(db, sale, sale_payment)
    room_ctx = _room_receipt_context(db, sale)
    cashier = db.get(User, sale.created_by_id) if sale.created_by_id else None
    cashier_name = cashier.username if cashier else ""
    from modules.receipt_whatsapp.service import sale_phone_hint, whatsapp_receipt_ctx

    from modules.printing.doc_numbers import DOC_KIND_PREFIX, next_doc_number

    # أوراق المطعم = فاتورة دائماً (مقيدة على شقة أو عادية) — لا إيصال قبض
    doc_kind = resolve_sale_doc_kind(sale, requested=doc, db=db)
    doc_title = restaurant_sale_doc_title(
        sale,
        room_hint=room_ctx.get("room_hint"),
        is_room_receipt=bool(room_ctx.get("is_room_receipt")),
    )
    try:
        doc_number = assign_sale_doc_number(db, sale, doc_kind)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        try:
            doc_number = next_doc_number(db, doc_kind, domain="restaurant")
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            prefix = DOC_KIND_PREFIX.get(doc_kind, "DOC")
            doc_number = f"{prefix}-{sale.id:06d}"

    try:
        wa_phone = sale_phone_hint(db, sale)
    except Exception:  # noqa: BLE001
        wa_phone = ""
    back_href = _safe_receipt_back_href(back, default="/pos")
    brand = getattr(request.state, "brand", None) or {}
    pos_label = (
        brand.get("pos_label") if isinstance(brand, dict) else None
    ) or "نقطة البيع"
    back_label = _receipt_back_label(back_href, pos_label=pos_label)
    preserve = {"doc": getattr(doc_kind, "value", str(doc_kind))}
    if embed:
        preserve["embed"] = "1"
    if autoprint:
        preserve["autoprint"] = "1"
    if request.query_params.get("popup") == "1":
        preserve["popup"] = "1"
    if back_href != "/pos":
        preserve["back"] = back_href
    try:
        silent_ctx = _receipt_silent_print_ctx(db)
    except Exception:  # noqa: BLE001
        silent_ctx = {"silent_print_enabled": False, "receipt_printer_name": None}
    try:
        wa_ctx = whatsapp_receipt_ctx(
            db,
            domain="pos",
            phone=wa_phone,
            send_url=f"/pos/receipt/{sale.id}/send-whatsapp",
        )
    except Exception:  # noqa: BLE001
        wa_ctx = {
            "whatsapp_receipt_enabled": False,
            "whatsapp_receipt_phone": "",
            "whatsapp_send_url": "",
        }
    return templates.TemplateResponse(
        "receipt_print.html",
        {
            "request": request,
            "sale": sale,
            "sections": sections,
            "autoprint": autoprint,
            "embed": embed,
            "paper": chosen_paper,
            "orientation": orient,
            "paper_css": paper_css,
            "paper_choices": PAPER_SIZES,
            "orientation_choices": PAPER_ORIENTATIONS,
            "can_choose_paper": can_choose,
            "print_form_action": f"/pos/receipt/{sale.id}",
            "print_preserve_params": preserve,
            "doc_kind": getattr(doc_kind, "value", str(doc_kind)),
            "doc_title": doc_title,
            "doc_number": doc_number,
            "store_name": store_name,
            "cashier_name": cashier_name,
            "show_paid_stamp": True,
            "silent_print_url": f"/pos/receipt/{sale.id}/silent-print",
            "can_edit_invoice": user_has_permission(user, SALES_EDIT_INVOICE),
            "back_href": back_href,
            "back_label": back_label,
            **delivery_ctx,
            **room_ctx,
            **silent_ctx,
            **wa_ctx,
        },
    )


@router.post("/receipt/{sale_id}/send-whatsapp")
async def pos_receipt_send_whatsapp(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    from modules.receipt_whatsapp.service import ReceiptWhatsAppError, send_pos_receipt_whatsapp

    sale = load_completed_sale_for_print(db, sale_id)
    if sale is None:
        return JSONResponse(
            {"ok": False, "error": "الفاتورة غير موجودة أو غير مكتملة."},
            status_code=404,
        )
    image_b64, phone_override = await _read_whatsapp_post_body(request)
    try:
        result = send_pos_receipt_whatsapp(
            db,
            sale,
            image_png_b64=image_b64,
            phone_override=phone_override,
        )
        db.commit()
        return JSONResponse(result)
    except ReceiptWhatsAppError as exc:
        db.rollback()
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/receipt/{sale_id}/silent-print")
async def pos_receipt_silent_print(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    sale = load_completed_sale_for_print(db, sale_id)
    if sale is None:
        return JSONResponse(
            {"ok": False, "error": "الفاتورة غير موجودة أو غير مكتملة."},
            status_code=404,
        )
    image_b64 = await _read_silent_print_image(request)
    return _silent_print_sale(
        db, request, user, sale, is_prebill=False, image_png_b64=image_b64
    )


@router.post("/set-table", response_class=HTMLResponse)
def pos_set_table(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    table_id: str = Form(""),
):
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    sale = _ensure_draft_or_create(request, db, user)
    raw = (table_id or "").strip()
    if not raw:
        sale.table_id = None
    else:
        try:
            tid = int(raw)
        except ValueError:
            tid = None
        if tid is not None:
            t = db.get(DiningTable, tid)
            if t is not None and t.is_active:
                sale.table_id = tid
            else:
                sale.table_id = None
        else:
            sale.table_id = None
    db.commit()
    return RedirectResponse("/pos", status_code=302)


@router.post("/set-context", response_class=HTMLResponse)
def pos_set_context(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    context_type: str = Form("TABLE"),
    table_id: str = Form(""),
    room_id: str = Form(""),
    customer_phone: str = Form(""),
    customer_name: str = Form(""),
    room_guest_name: str = Form(""),
    room_guest_phone: str = Form(""),
    external_order_type: str = Form("PICKUP"),
    delivery_zone_id: str = Form(""),
    save_customer: str = Form(""),
):
    """يضبط سياق الفاتورة: TABLE / ROOM / EXTERNAL.

    - TABLE: يربط بـ table_id
    - ROOM: يربط بـ room_id (سيُحوَّل لحساب غرفة عند checkout)
    - EXTERNAL: يربط بعميل عبر رقم الهاتف (يُنشأ إن لم يوجد)
    """
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    from modules.delivery.service import get_zone
    from modules.sales.models import ExternalOrderType, SaleContext

    sale = _ensure_draft_or_create(request, db, user)
    ctx = (context_type or "TABLE").strip().upper()

    if ctx == "ROOM":
        from modules.authz.permissions import HOTEL_CHARGE
        from modules.hotel.models import HotelRoom

        if not user_has_permission(user, HOTEL_CHARGE):
            return RedirectResponse(
                "/pos?ctx_err=ليست لديك صلاحية القيد على غرفة.",
                status_code=302,
            )

        # نضبط السياق على ROOM دائماً (حتى لو لم تُحدَّد شقة بعد).
        # هذا يسمح بفتح التبويب أولاً ثم اختيار الشقة، ويمنع المتاجرة
        # عَرَضاً تحت سياق طاولة بينما الكاشير يظنّ أنه في تبويب الشقة.
        sale.context_type = SaleContext.ROOM
        if sale.sent_to_kitchen_at is None:
            sale.table_id = None
        sale.customer_id = None
        sale.external_order_type = ExternalOrderType.PICKUP
        sale.delivery_zone_id = None
        sale.delivery_zone_name = None
        sale.delivery_fee = Decimal("0")

        raw_rid = (room_id or "").strip()
        if not raw_rid:
            # تنظيف أي اختيار شقة سابق — الافتراضي: لا اختيار.
            request.session.pop("draft_room_id", None)
            request.session.pop("draft_room_guest", None)
            request.session.pop("draft_room_phone", None)
            db.commit()
            return RedirectResponse("/pos", status_code=302)

        try:
            rid = int(raw_rid)
        except (TypeError, ValueError):
            rid = None
        room = db.get(HotelRoom, rid) if rid else None
        if room is None or not room.is_active:
            request.session.pop("draft_room_id", None)
            request.session.pop("draft_room_guest", None)
            request.session.pop("draft_room_phone", None)
            db.commit()
            return RedirectResponse(
                "/pos?ctx_err=الشقة غير صالحة. اختر شقة من القائمة.",
                status_code=302,
            )
        request.session["draft_room_id"] = rid
        db.commit()
        return RedirectResponse("/pos", status_code=302)

    if ctx == "EXTERNAL":
        from modules.customers.service import (
            CustomersError,
            get_or_create_by_phone,
            normalize_phone,
        )

        sale.context_type = SaleContext.EXTERNAL
        if sale.sent_to_kitchen_at is None:
            sale.table_id = None
        sale.external_order_type = ExternalOrderType.PICKUP
        sale.delivery_zone_id = None
        sale.delivery_zone_name = None
        sale.delivery_fee = Decimal("0")
        request.session.pop("draft_room_id", None)
        request.session.pop("draft_room_guest", None)
        request.session.pop("draft_room_phone", None)
        ext_type_raw = (external_order_type or "PICKUP").strip().upper()
        ext_type = (
            ExternalOrderType.DELIVERY
            if ext_type_raw == ExternalOrderType.DELIVERY.value
            else ExternalOrderType.PICKUP
        )
        sale.external_order_type = ext_type
        if ext_type == ExternalOrderType.DELIVERY:
            zid = _parse_int((delivery_zone_id or "").strip())
            zone = get_zone(db, int(zid)) if zid else None
            if zone is not None and zone.is_active:
                sale.delivery_zone_id = zone.id
                sale.delivery_zone_name = zone.name_ar
                sale.delivery_fee = Decimal(str(zone.fee or 0)).quantize(
                    Decimal("0.001")
                )
            elif save_customer == "1" or zid:
                db.rollback()
                return RedirectResponse(
                    "/pos?ctx_err="
                    + quote(
                        "اختر منطقة توصيل صالحة من القائمة قبل حفظ بيانات العميل."
                    ),
                    status_code=302,
                )
        phone_norm = normalize_phone(customer_phone)
        if not phone_norm:
            if save_customer == "1":
                db.rollback()
                return RedirectResponse(
                    "/pos?ctx_err="
                    + quote(
                        "رقم هاتف العميل مطلوب للطلب الخارجي. "
                        "افتح تبويب «خارجي»، أدخل الهاتف، ثم «حفظ بيانات العميل»."
                    ),
                    status_code=302,
                )
            db.commit()
            return RedirectResponse("/pos", status_code=302)
        try:
            c = get_or_create_by_phone(db, phone=phone_norm, name=customer_name)
            sale.customer_id = c.id
        except CustomersError as e:
            db.rollback()
            return RedirectResponse(f"/pos?ctx_err={quote(str(e))}", status_code=302)
        db.commit()
        return RedirectResponse("/pos", status_code=302)

    # الافتراضي: TABLE
    sale.context_type = SaleContext.TABLE
    sale.external_order_type = ExternalOrderType.PICKUP
    sale.delivery_zone_id = None
    sale.delivery_zone_name = None
    sale.delivery_fee = Decimal("0")
    request.session.pop("draft_room_id", None)
    request.session.pop("draft_room_guest", None)
    request.session.pop("draft_room_phone", None)
    raw = (table_id or "").strip()
    if raw:
        try:
            tid = int(raw)
        except ValueError:
            tid = None
        if tid is not None:
            t = db.get(DiningTable, tid)
            sale.table_id = tid if (t is not None and t.is_active) else None
        else:
            sale.table_id = None
    db.commit()
    return RedirectResponse("/pos", status_code=302)


@router.post("/save-room-guest")
def pos_save_room_guest(
    request: Request,
    user: User = Depends(require_permission(SALES_CREATE)),
    room_guest_name: str = Form(""),
    room_guest_phone: str = Form(""),
):
    """حفظ اسم/هاتف النزيل في الجلسة دون إعادة تحميل الصفحة."""
    request.session["draft_room_guest"] = (room_guest_name or "").strip()
    request.session["draft_room_phone"] = (room_guest_phone or "").strip()
    from starlette.responses import Response

    return Response(status_code=204)


@router.post("/settle-as-room", response_class=HTMLResponse)
def pos_settle_as_room(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    """تحويل الطلب الحالي من طاولة/خارجي إلى حساب شقة."""
    from modules.authz.permissions import HOTEL_CHARGE

    if not user_has_permission(user, HOTEL_CHARGE):
        return RedirectResponse(
            "/pos?ctx_err=" + quote("ليست لديك صلاحية القيد على حساب شقة."),
            status_code=302,
        )
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    sale = _get_current_draft(request, db, user)
    if sale is None or not sale.lines:
        return RedirectResponse(
            "/pos?ctx_err=" + quote("لا يوجد طلب نشط."),
            status_code=302,
        )
    sale.context_type = SaleContext.ROOM
    sale.customer_id = None
    sale.external_order_type = ExternalOrderType.PICKUP
    sale.delivery_zone_id = None
    sale.delivery_zone_name = None
    sale.delivery_fee = Decimal("0")
    if sale.sent_to_kitchen_at is None:
        sale.table_id = None
    db.commit()
    return RedirectResponse(
        "/pos?ctx_ok="
        + quote("تم تحويل الطلب لحساب شقة — اختر الشقة من عمود «الشقق» ثم «قيد على حساب الشقة»."),
        status_code=302,
    )


@router.post("/settle-as-pay", response_class=HTMLResponse)
def pos_settle_as_pay(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    """تحويل طلب شقة إلى دفع مباشر (نقد/بطاقة) بدلاً من قيد الغرفة."""
    from modules.sales.order_policy import load_order_policy, payment_block_reason

    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    sale = _get_current_draft(request, db, user)
    if sale is None or not sale.lines:
        return RedirectResponse(
            "/pos?ctx_err=" + quote("لا يوجد طلب نشط."),
            status_code=302,
        )
    policy = load_order_policy(db)
    pay_block = payment_block_reason(db, sale, policy)
    if pay_block:
        return RedirectResponse(
            "/pos?ctx_err=" + quote(pay_block),
            status_code=302,
        )
    sale.context_type = SaleContext.TABLE
    request.session.pop("draft_room_id", None)
    request.session.pop("draft_room_guest", None)
    request.session.pop("draft_room_phone", None)
    db.commit()
    return RedirectResponse(f"/pos?open_checkout={sale.id}", status_code=302)


@router.post("/charge-room", response_class=HTMLResponse)
def pos_charge_room(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
    guest_name: str = Form(""),
    guest_phone: str = Form(""),
    room_guest_name: str = Form(""),
    room_guest_phone: str = Form(""),
    note: str = Form(""),
):
    """قيد طلب على حساب شقة فندق مباشرة من الـ sidebar — بدون شاشة دفع.

    يتطلّب صلاحية HOTEL_CHARGE وأن تكون مسودة الفاتورة في سياق ROOM
    وأن يكون room_id محفوظاً في الـ session (مختار من تبويب الشقة).

    الكاشير العادي يستخدم هذا الـ endpoint؛ موظف الاستقبال هو من يسوّي
    الحساب لاحقاً عبر `/hotel/settle` (يحتاج صلاحية HOTEL_SETTLE).
    """
    from modules.authz.permissions import HOTEL_CHARGE
    from modules.hotel.service import HotelError, open_room_charge
    from modules.hotel.models import HotelRoom
    from modules.sales.models import SaleContext

    if not user_has_permission(user, HOTEL_CHARGE):
        return RedirectResponse(
            "/pos?ctx_err=ليست لديك صلاحية القيد على حساب شقة.",
            status_code=302,
        )

    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    open_shift = gr

    sale = _ensure_draft_or_create(request, db, user)
    loaded = load_sale_with_lines(db, sale.id)
    if loaded:
        sale = loaded

    if not sale.lines:
        return RedirectResponse(
            "/pos?ctx_err=أضِف صنفاً للسلة أولاً.", status_code=302
        )

    from modules.sales.order_policy import load_order_policy, room_charge_block_reason

    policy = load_order_policy(db)
    charge_block = room_charge_block_reason(db, sale, policy)
    if charge_block:
        return RedirectResponse(
            "/pos?ctx_err=" + quote(charge_block),
            status_code=302,
        )

    # رقم الشقة من الـ session (مُحدَّد سابقاً عبر تبويب الشقة)
    room_id_raw = request.session.get("draft_room_id")
    try:
        room_id = int(room_id_raw) if room_id_raw else None
    except (TypeError, ValueError):
        room_id = None
    if room_id is None:
        return RedirectResponse(
            "/pos?ctx_err=اختر الشقة أولاً من تبويب «شقة».",
            status_code=302,
        )
    room = db.get(HotelRoom, room_id)
    if room is None or not room.is_active:
        return RedirectResponse(
            "/pos?ctx_err=الشقة غير صالحة.", status_code=302
        )

    guest = (guest_name or room_guest_name or "").strip()
    phone = (guest_phone or room_guest_phone or "").strip()
    if guest:
        request.session["draft_room_guest"] = guest
    if phone:
        request.session["draft_room_phone"] = phone

    try:
        from modules.customers.service import attach_customer_to_sale

        attach_customer_to_sale(
            db, sale, phone=phone or None, name=guest or None
        )
        complete_sale(db, sale.id, user.id, pos_shift_id=open_shift.id)
        sale.context_type = SaleContext.ROOM
        open_room_charge(
            db,
            sale_id=sale.id,
            room_id=room_id,
            guest_name=guest or None,
            note=note,
            user_id=user.id,
        )
        db.commit()
    except (SalesError, HotelError) as e:
        db.rollback()
        return RedirectResponse(f"/pos?ctx_err={e}", status_code=302)
    except InsufficientStock as e:
        db.rollback()
        return RedirectResponse(f"/pos?ctx_err={e}", status_code=302)

    # توجيه طلبات الأقسام للمطبخ — مثل checkout العادي
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
    guest_name = guest or None
    guest_phone = phone or None
    room_hint = str(room.number) if room is not None else None
    request.session.pop("draft_sale_id", None)
    request.session.pop("draft_room_id", None)
    request.session.pop("draft_room_guest", None)
    request.session.pop("draft_room_phone", None)
    psid = _session_pos_shift_id(request)
    new_sale = create_draft_sale(db, user.id, pos_shift_id=psid)
    request.session["draft_sale_id"] = new_sale.id
    db.commit()
    from modules.printing.service import checkout_receipt_redirect

    return RedirectResponse(
        checkout_receipt_redirect(
            db,
            receipt_id,
            room_hint=room_hint,
            guest_name=guest_name,
            guest_phone=guest_phone,
        ),
        status_code=302,
    )


@router.post("/cancel-open-order/{sale_id}", response_class=HTMLResponse)
def pos_cancel_open_order(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    """إلغاء طلب مفتوح من شريط التحذير (لم يُرسَل للمطبخ)."""
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    psid = _session_pos_shift_id(request)
    sale = get_draft_sale(db, sale_id)
    if sale is None or sale.created_by_id != user.id:
        return RedirectResponse(
            "/pos?ctx_err=" + quote("الطلب غير موجود أو لا يمكن إلغاؤه."),
            status_code=302,
        )
    if sale.sent_to_kitchen_at is not None:
        return RedirectResponse(
            "/pos?ctx_err="
            + quote("لا يمكن إلغاء طلب أُرسل للمطبخ — أكمل الدفع أو راجع المشرف."),
            status_code=302,
        )
    table_id = sale.table_id
    try:
        cancel_sale(db, sale.id)
        if table_id is not None:
            cancel_unsent_drafts_for_table(
                db,
                user_id=user.id,
                table_id=int(table_id),
                pos_shift_id=psid,
            )
        if _session_draft_sale_id(request) == sale_id:
            request.session.pop("draft_sale_id", None)
            new_sale = create_draft_sale(db, user.id, pos_shift_id=psid)
            request.session["draft_sale_id"] = new_sale.id
        db.commit()
    except SalesError as e:
        db.rollback()
        return RedirectResponse("/pos?ctx_err=" + quote(str(e)), status_code=302)
    return RedirectResponse("/pos", status_code=302)


@router.post("/cancel-draft", response_class=HTMLResponse)
def pos_cancel_draft(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(SALES_CREATE)),
):
    gr = _active_pos_shift_or_redirect(request, db, user)
    if isinstance(gr, RedirectResponse):
        return gr
    psid = _session_pos_shift_id(request)
    raw = request.session.get("draft_sale_id")
    cancelled_table_id = None
    if raw:
        try:
            sid = int(raw)
            cur = db.get(Sale, sid)
            if cur is not None:
                cancelled_table_id = cur.table_id
            cancel_sale(db, sid)
            if cancelled_table_id is not None:
                cancel_unsent_drafts_for_table(
                    db,
                    user_id=user.id,
                    table_id=int(cancelled_table_id),
                    pos_shift_id=psid,
                )
            db.commit()
        except SalesError as e:
            db.rollback()
            return RedirectResponse(
                "/pos?ctx_err=" + quote(str(e)),
                status_code=302,
            )
    request.session.pop("draft_sale_id", None)
    new_sale = create_draft_sale(db, user.id, pos_shift_id=psid)
    request.session["draft_sale_id"] = new_sale.id
    db.commit()
    return RedirectResponse("/pos", status_code=302)


# duplicate cancel-draft end - verify only one
