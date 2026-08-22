from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import ADMIN_SETTINGS
from modules.inventory.loss_reasons import (
    add_loss_reason,
    get_loss_reasons,
    remove_loss_reason,
    update_loss_reason,
)
from modules.inventory.service import get_main_warehouse, list_warehouses
from modules.sales.line_notes import (
    add_modifier_preset,
    get_modifier_presets,
    remove_modifier_preset,
    update_modifier_preset,
)
from modules.settings.refund_auth import (
    RefundAuthError,
    refund_auth_configured,
    set_refund_authorization_code,
)
from modules.settings.service import (
    PAPER_ORIENTATIONS,
    PAPER_SIZES,
    get_bool,
    get_int,
    get_public_base_url,
    get_setting,
    invalidate_settings_cache,
    normalize_orientation,
    normalize_paper,
    public_base_url_from_env,
    set_setting,
)

router = APIRouter(prefix="/admin/settings", tags=["settings"])
_admin = require_permission(ADMIN_SETTINGS)


@router.get("", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_admin),
):
    main_wh = get_main_warehouse(db)
    wh_raw = get_setting(db, "default_sales_warehouse_id", "")
    try:
        sales_wh_id = int(wh_raw) if wh_raw else main_wh.id
    except ValueError:
        sales_wh_id = main_wh.id
    from modules.printing.models import Printer, PrinterConnectionType

    receipt_printer_id = get_int(db, "receipt_printer_id", 0)
    saved_rp_q = (request.query_params.get("rp") or "").strip()
    if saved_rp_q.isdigit():
        receipt_printer_id = int(saved_rp_q)
    receipt_printers = list(
        db.scalars(
            select(Printer)
            .where(
                Printer.is_active.is_(True),
                Printer.connection_type.in_(
                    (
                        PrinterConnectionType.LOCAL_AGENT.value,
                        PrinterConnectionType.DIRECT_TCP.value,
                    )
                ),
            )
            .order_by(Printer.name)
        ).all()
    )
    from modules.sales.order_policy import (
        KDS_SOUND_PROFILE_LABELS,
        OrderPolicy,
        load_order_policy,
    )
    from modules.catalog.models import Product, ProductKind

    try:
        order_policy = load_order_policy(db)
    except Exception:  # noqa: BLE001
        order_policy = OrderPolicy()
    complimentary_products = list(
        db.scalars(
            select(Product)
            .where(
                Product.kind == ProductKind.FINAL_SELLABLE,
                Product.is_active.is_(True),
            )
            .order_by(Product.name_ar)
        ).all()
    )

    return templates.TemplateResponse(
        "admin_settings.html",
        {
            "request": request,
            "paper_sizes": PAPER_SIZES,
            "paper_orientations": PAPER_ORIENTATIONS,
            "current_paper": get_setting(db, "print_paper_size", "A5"),
            "current_orientation": get_setting(db, "print_paper_orientation", "portrait"),
            "store_name": get_setting(db, "store_name", "نقطة البيع"),
            "public_base_url": get_setting(db, "public_base_url", ""),
            "public_base_url_effective": get_public_base_url(db),
            "public_base_url_env": public_base_url_from_env(),
            "shop_order_confirmation_text": get_setting(
                db,
                "shop_order_confirmation_text",
                "تم استقبال طلبك رقم {order_id}.\nسيتم التواصل معك لاحقاً عبر واتساب لتأكيد التفاصيل والدفع.",
            ),
            "shop_send_order_confirmation_whatsapp": get_bool(
                db, "shop_send_order_confirmation_whatsapp", True
            ),
            "pos_send_receipt_whatsapp": get_bool(db, "pos_send_receipt_whatsapp", True),
            "hotel_send_receipt_whatsapp": get_bool(db, "hotel_send_receipt_whatsapp", True),
            "warehouses": list_warehouses(db),
            "sales_warehouse_id": sales_wh_id,
            "saved": request.query_params.get("saved") == "1",
            "modifier_presets": get_modifier_presets(db),
            "modifier_saved": request.query_params.get("modifier_saved") == "1",
            "loss_reasons": get_loss_reasons(db),
            "loss_saved": request.query_params.get("loss_saved") == "1",
            "receipt_printers": receipt_printers,
            "receipt_printer_id": receipt_printer_id,
            "saved_receipt_printer_id": saved_rp_q if saved_rp_q.isdigit() else None,
            "refund_auth_configured": refund_auth_configured(db),
            "refund_auth_saved": request.query_params.get("refund_auth") == "1",
            "refund_auth_error": request.query_params.get("refund_auth_err"),
            "order_policy": order_policy,
            "complimentary_products": complimentary_products,
            "kds_sound_profiles": KDS_SOUND_PROFILE_LABELS,
            "order_policy_saved": request.query_params.get("order_policy_saved")
            == "1",
            "catalog_bom_global_adjust_pct": get_setting(
                db, "catalog_bom_global_adjust_pct", "0"
            ),
            "bom_pricing_saved": request.query_params.get("bom_pricing_saved") == "1",
            "bom_recalc_count": request.query_params.get("bom_recalc"),
            "bom_pricing_error": request.query_params.get("bom_pricing_err"),
            "treasury_notifications_enabled": get_bool(
                db, "treasury_notifications_enabled", True
            ),
            "treasury_notify_shift_close_enabled": get_bool(
                db, "treasury_notify_shift_close_enabled", True
            ),
            "treasury_notify_handoff_pending_enabled": get_bool(
                db, "treasury_notify_handoff_pending_enabled", False
            ),
            "treasury_notify_movements_enabled": get_bool(
                db, "treasury_notify_movements_enabled", True
            ),
            "treasury_notify_balance_updates_enabled": get_bool(
                db, "treasury_notify_balance_updates_enabled", True
            ),
            "hotel_shift_allow_next_shift_carry": get_bool(
                db, "hotel_shift_allow_next_shift_carry", True
            ),
            "hotel_shift_allow_treasury_close": get_bool(
                db, "hotel_shift_allow_treasury_close", True
            ),
            "pos_shift_allow_next_shift_carry": get_bool(
                db, "pos_shift_allow_next_shift_carry", True
            ),
            "pos_shift_allow_treasury_close": get_bool(
                db, "pos_shift_allow_treasury_close", True
            ),
            **(
                __import__(
                    "modules.security.supervisor_otp",
                    fromlist=["otp_policy_context"],
                ).otp_policy_context(db)
            ),
            "otp_policy_saved": request.query_params.get("otp_saved") == "1",
        },
    )


def _save_refund_authorization_settings(
    db: DBSession,
    *,
    refund_code: str,
    clear_refund_code: str,
) -> None:
    if (clear_refund_code or "").strip().lower() in ("on", "1", "true", "yes"):
        set_refund_authorization_code(db, plain=None, clear=True)
    elif (refund_code or "").strip():
        set_refund_authorization_code(db, plain=refund_code.strip())
    else:
        raise RefundAuthError("أدخل كوداً جديداً (4–12 حرفاً) أو فعّل «حذف الكود الحالي».")


@router.post("", response_class=HTMLResponse)
def settings_save(
    request: Request,
    db: DBSession,
    _: User = Depends(_admin),
    print_paper_size: str = Form("A5"),
    print_paper_orientation: str = Form("portrait"),
    store_name: str = Form(""),
    public_base_url: str = Form(""),
    shop_order_confirmation_text: str = Form(""),
    shop_send_order_confirmation_whatsapp: str = Form(""),
    pos_send_receipt_whatsapp: str = Form(""),
    hotel_send_receipt_whatsapp: str = Form(""),
    receipt_printer_id: str = Form(""),
    default_sales_warehouse_id: str = Form(""),
    treasury_notifications_enabled: str = Form(""),
    treasury_notify_shift_close_enabled: str = Form(""),
    treasury_notify_handoff_pending_enabled: str = Form(""),
    treasury_notify_movements_enabled: str = Form(""),
    treasury_notify_balance_updates_enabled: str = Form(""),
    hotel_shift_allow_next_shift_carry: str = Form(""),
    hotel_shift_allow_treasury_close: str = Form(""),
    pos_shift_allow_next_shift_carry: str = Form(""),
    pos_shift_allow_treasury_close: str = Form(""),
    refund_code_save: str = Form(""),
    refund_code: str = Form(""),
    clear_refund_code: str = Form(""),
):
    from urllib.parse import quote

    if (refund_code_save or "").strip():
        try:
            _save_refund_authorization_settings(
                db,
                refund_code=refund_code,
                clear_refund_code=clear_refund_code,
            )
            db.commit()
        except RefundAuthError as exc:
            db.rollback()
            return RedirectResponse(
                "/admin/settings?refund_auth_err=" + quote(str(exc)),
                status_code=302,
            )
        return RedirectResponse("/admin/settings?refund_auth=1", status_code=302)

    set_setting(db, "print_paper_size", normalize_paper(print_paper_size, "A5"))
    set_setting(
        db,
        "print_paper_orientation",
        normalize_orientation(print_paper_orientation, "portrait"),
    )
    set_setting(db, "store_name", store_name.strip() or "نقطة البيع")
    set_setting(db, "public_base_url", public_base_url.strip().rstrip("/"))
    set_setting(
        db,
        "shop_order_confirmation_text",
        shop_order_confirmation_text.strip()
        or "تم استقبال طلبك رقم {order_id}.\nسيتم التواصل معك لاحقاً عبر واتساب لتأكيد التفاصيل والدفع.",
    )
    set_setting(
        db,
        "shop_send_order_confirmation_whatsapp",
        "1" if shop_send_order_confirmation_whatsapp == "1" else "0",
    )
    set_setting(
        db,
        "pos_send_receipt_whatsapp",
        "1" if pos_send_receipt_whatsapp == "1" else "0",
    )
    set_setting(
        db,
        "hotel_send_receipt_whatsapp",
        "1" if hotel_send_receipt_whatsapp == "1" else "0",
    )
    rp = (receipt_printer_id or "").strip()
    saved_rp = ""
    if rp.isdigit():
        from modules.printing.models import Printer

        pid = int(rp)
        if db.get(Printer, pid) is not None:
            saved_rp = str(pid)
    set_setting(db, "receipt_printer_id", saved_rp)
    set_setting(
        db,
        "treasury_notifications_enabled",
        "1" if treasury_notifications_enabled == "1" else "0",
    )
    set_setting(
        db,
        "treasury_notify_shift_close_enabled",
        "1" if treasury_notify_shift_close_enabled == "1" else "0",
    )
    set_setting(
        db,
        "treasury_notify_handoff_pending_enabled",
        "1" if treasury_notify_handoff_pending_enabled == "1" else "0",
    )
    set_setting(
        db,
        "treasury_notify_movements_enabled",
        "1" if treasury_notify_movements_enabled == "1" else "0",
    )
    set_setting(
        db,
        "treasury_notify_balance_updates_enabled",
        "1" if treasury_notify_balance_updates_enabled == "1" else "0",
    )
    from modules.payments.shift_carry import (
        save_hotel_shift_close_policy,
        save_pos_shift_close_policy,
    )

    save_hotel_shift_close_policy(
        db,
        allow_carry=hotel_shift_allow_next_shift_carry == "1",
        allow_treasury=hotel_shift_allow_treasury_close == "1",
    )
    save_pos_shift_close_policy(
        db,
        allow_carry=pos_shift_allow_next_shift_carry == "1",
        allow_treasury=pos_shift_allow_treasury_close == "1",
    )
    invalidate_settings_cache()
    wh_raw = (default_sales_warehouse_id or "").strip()
    if wh_raw:
        from modules.inventory.service import resolve_warehouse_id

        try:
            wid = resolve_warehouse_id(db, int(wh_raw))
            set_setting(db, "default_sales_warehouse_id", str(wid))
        except Exception:
            set_setting(db, "default_sales_warehouse_id", "")
    else:
        set_setting(db, "default_sales_warehouse_id", "")
    db.commit()
    qs = "saved=1"
    if saved_rp:
        qs += f"&rp={saved_rp}"
    return RedirectResponse(f"/admin/settings?{qs}", status_code=302)


@router.post("/order-policy", response_class=HTMLResponse)
def settings_order_policy_save(
    request: Request,
    db: DBSession,
    _: User = Depends(_admin),
    kitchen_workflow_enabled: str = Form(""),
    require_kitchen_send_before_pay: str = Form(""),
    require_kitchen_send_before_room_charge: str = Form(""),
    require_kds_ready_table: str = Form(""),
    require_kds_ready_pickup: str = Form(""),
    require_kds_ready_delivery: str = Form(""),
    require_kds_ready_room: str = Form(""),
    pay_redirect_after_send_pickup: str = Form(""),
    send_confirm_enabled: str = Form(""),
    send_confirm_min_total: str = Form("0"),
    show_pipeline_bar: str = Form(""),
    kds_sound_enabled: str = Form(""),
    kds_sound_repeat_sec: str = Form("30"),
    kds_sound_profile: str = Form("beep"),
    kds_sound_volume: str = Form("80"),
    kds_poll_sec: str = Form("15"),
    unsent_reminder_sec: str = Form("0"),
    table_complimentary_enabled: str = Form(""),
    table_complimentary_product_id: str = Form("0"),
    table_complimentary_name: str = Form("ضيافة ترحيبية مجانية"),
):
    from modules.sales.order_policy import policy_from_form, save_order_policy

    policy = policy_from_form(
        {
            "kitchen_workflow_enabled": kitchen_workflow_enabled,
            "require_kitchen_send_before_pay": require_kitchen_send_before_pay,
            "require_kitchen_send_before_room_charge": require_kitchen_send_before_room_charge,
            "require_kds_ready_table": require_kds_ready_table,
            "require_kds_ready_pickup": require_kds_ready_pickup,
            "require_kds_ready_delivery": require_kds_ready_delivery,
            "require_kds_ready_room": require_kds_ready_room,
            "pay_redirect_after_send_pickup": pay_redirect_after_send_pickup,
            "send_confirm_enabled": send_confirm_enabled,
            "send_confirm_min_total": send_confirm_min_total,
            "show_pipeline_bar": show_pipeline_bar,
            "kds_sound_enabled": kds_sound_enabled,
            "kds_sound_repeat_sec": kds_sound_repeat_sec,
            "kds_sound_profile": kds_sound_profile,
            "kds_sound_volume": kds_sound_volume,
            "kds_poll_sec": kds_poll_sec,
            "unsent_reminder_sec": unsent_reminder_sec,
            "table_complimentary_enabled": table_complimentary_enabled,
            "table_complimentary_product_id": table_complimentary_product_id,
            "table_complimentary_name": table_complimentary_name,
        }
    )
    save_order_policy(db, policy)
    db.commit()
    return RedirectResponse(
        "/admin/settings?order_policy_saved=1#order-policy-section",
        status_code=302,
    )


@router.post("/otp-policy", response_class=HTMLResponse)
def settings_otp_policy_save(
    request: Request,
    db: DBSession,
    _: User = Depends(_admin),
    otp_require_hotel_refund: str = Form(""),
    otp_require_pos_refund: str = Form(""),
    otp_require_hotel_cancel: str = Form(""),
):
    from modules.security.supervisor_otp import save_otp_policy

    save_otp_policy(
        db,
        hotel_refund=otp_require_hotel_refund == "on",
        pos_refund=otp_require_pos_refund == "on",
        hotel_cancel=otp_require_hotel_cancel == "on",
    )
    db.commit()
    return RedirectResponse(
        "/admin/settings?otp_saved=1#supervisor-otp-section",
        status_code=302,
    )


@router.post("/refund-code", response_class=HTMLResponse)
def settings_refund_code_save(
    request: Request,
    db: DBSession,
    _: User = Depends(_admin),
    refund_code: str = Form(""),
    clear_refund_code: str = Form(""),
):
    from urllib.parse import quote

    try:
        _save_refund_authorization_settings(
            db,
            refund_code=refund_code,
            clear_refund_code=clear_refund_code,
        )
        db.commit()
    except RefundAuthError as exc:
        db.rollback()
        return RedirectResponse(
            "/admin/settings?refund_auth_err=" + quote(str(exc)),
            status_code=302,
        )
    return RedirectResponse("/admin/settings?refund_auth=1", status_code=302)


@router.post("/line-modifier/add", response_class=HTMLResponse)
def settings_line_modifier_add(
    request: Request,
    db: DBSession,
    _: User = Depends(_admin),
    label: str = Form(""),
):
    add_modifier_preset(db, label)
    db.commit()
    return RedirectResponse("/admin/settings?modifier_saved=1#modifier-presets-section", status_code=302)


@router.post("/line-modifier/remove/{index}", response_class=HTMLResponse)
def settings_line_modifier_remove(
    request: Request,
    index: int,
    db: DBSession,
    _: User = Depends(_admin),
):
    remove_modifier_preset(db, index)
    db.commit()
    return RedirectResponse("/admin/settings?modifier_saved=1#modifier-presets-section", status_code=302)


@router.post("/line-modifier/edit/{index}", response_class=HTMLResponse)
def settings_line_modifier_edit(
    request: Request,
    index: int,
    db: DBSession,
    _: User = Depends(_admin),
    label: str = Form(""),
):
    update_modifier_preset(db, index, label)
    db.commit()
    return RedirectResponse("/admin/settings?modifier_saved=1#modifier-presets-section", status_code=302)


@router.post("/loss-reason/add", response_class=HTMLResponse)
def settings_loss_reason_add(
    request: Request,
    db: DBSession,
    _: User = Depends(_admin),
    label: str = Form(""),
):
    add_loss_reason(db, label)
    db.commit()
    return RedirectResponse("/admin/settings?loss_saved=1#loss-reasons-section", status_code=302)


@router.post("/loss-reason/remove/{index}", response_class=HTMLResponse)
def settings_loss_reason_remove(
    request: Request,
    index: int,
    db: DBSession,
    _: User = Depends(_admin),
):
    remove_loss_reason(db, index)
    db.commit()
    return RedirectResponse("/admin/settings?loss_saved=1#loss-reasons-section", status_code=302)


@router.post("/loss-reason/edit/{index}", response_class=HTMLResponse)
def settings_loss_reason_edit(
    request: Request,
    index: int,
    db: DBSession,
    _: User = Depends(_admin),
    label: str = Form(""),
):
    update_loss_reason(db, index, label)
    db.commit()
    return RedirectResponse("/admin/settings?loss_saved=1#loss-reasons-section", status_code=302)


@router.post("/bom-pricing", response_class=HTMLResponse)
def settings_bom_pricing_save(
    db: DBSession,
    _: User = Depends(_admin),
    catalog_bom_global_adjust_pct: str = Form("0"),
):
    from decimal import Decimal, InvalidOperation
    from urllib.parse import quote

    from modules.catalog.bom_pricing import recalculate_all_bom_linked_prices

    raw = (catalog_bom_global_adjust_pct or "0").strip().replace(",", ".")
    try:
        Decimal(raw)
    except InvalidOperation:
        return RedirectResponse(
            f"/admin/settings?bom_pricing_err={quote('النسبة العامة يجب أن تكون رقماً.')}",
            status_code=302,
        )
    set_setting(db, "catalog_bom_global_adjust_pct", raw)
    updated = recalculate_all_bom_linked_prices(db)
    db.commit()
    return RedirectResponse(
        f"/admin/settings?bom_pricing_saved=1&bom_recalc={updated}#bom-pricing-section",
        status_code=302,
    )
