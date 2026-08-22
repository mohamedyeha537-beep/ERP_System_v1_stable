from __future__ import annotations

import os
import time
from threading import Lock

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.settings.models import AppSetting

_SETTINGS_MAP: dict[str, str] | None = None
_SETTINGS_MAP_AT: float = 0.0
_SETTINGS_LOCK = Lock()
_SETTINGS_CACHE_TTL = 60.0

PAPER_SIZES: tuple[tuple[str, str], ...] = (
    ("A4", "A4 (ورقة كاملة 21×29.7 سم)"),
    ("A5", "A5 (نصف ورقة 14.8×21 سم)"),
    ("80mm", "حرارية 80 ملم (8 سم)"),
    ("58mm", "حرارية 58 ملم (5.8 سم)"),
)

PAPER_ORIENTATIONS: tuple[tuple[str, str], ...] = (
    ("portrait", "طولية (عمودي)"),
    ("landscape", "عرضية (أفقي)"),
)

VALID_PAPER = {code for code, _ in PAPER_SIZES}
VALID_ORIENTATION = {code for code, _ in PAPER_ORIENTATIONS}

DEFAULTS: dict[str, str] = {
    "print_paper_size": "A5",
    "print_paper_orientation": "portrait",
    "hotel_print_paper_size": "A5",
    "hotel_print_paper_orientation": "portrait",
    "hotel_receipt_printer_id": "",
    "store_name": "Bayatak — Roof Caffee",
    "public_base_url": "",
    # تنبيهات نفاد المخزون
    "alerts_enabled": "0",
    "alerts_email_to": "",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "smtp_use_tls": "1",
    "whatsapp_webhook_url": "",
    "whatsapp_webhook_method": "GET",
    "whatsapp_webhook_param": "text",
    # مخزن خصم مبيعات نقطة البيع (فارغ = المخزن الرئيسي)
    "default_sales_warehouse_id": "",
    # طابعة الكاشير للطباعة المباشرة (معرّف printers.id، فارغ = نافذة المتصفح)
    "receipt_printer_id": "",
    # إحالة: عدد مرات استخدام نفس الكود لكل زبون (0 = بلا حد)
    "referral_max_uses_per_code": "1",
    # بوت المراسلات
    "messaging_enabled": "0",
    "messaging_webhook_url": "",
    "messaging_webhook_method": "POST",
    "messaging_webhook_param": "text",
    "messaging_use_n8n_json": "1",
    "messaging_telegram_bot_token": "",
    "messaging_admin_phone": "",
    "messaging_test_phone": "",
    # TextMeBot مباشر (بديل n8n لـ WhatsApp)
    "messaging_whatsapp_provider": "webhook",  # webhook | textmebot
    "messaging_textmebot_apikey": "",
    "messaging_textmebot_base_url": "http://api.textmebot.com/send.php",
    "messaging_country_code": "218",
    "messaging_send_delay_seconds": "10",
    "messaging_outbox_batch_size": "1",
    "messaging_worker_interval_seconds": "30",
    "messaging_outbox_worker_enabled": "1",
    "catalog_bom_global_adjust_pct": "0",
    "store_timezone": "Africa/Tripoli",
    # ZKBioTime — مزامنة البصمة (جهاز الكاشير المحلي)
    "zk_sync_enabled": "0",
    "zk_sync_interval_seconds": "30",
    "zk_biotime_host": "",
    "zk_biotime_port": "7496",
    "zk_biotime_db": "",
    "zk_biotime_user": "",
    "zk_biotime_password": "",
    "zk_last_transaction_id": "0",
    "zk_last_sync_at": "",
    "zk_last_sync_error": "",
    # محرك الإشعارات المركزي
    "notifications_enabled": "1",
    "notification_inventory_phones": "",
    "notification_hr_phones": "",
    "notification_supervisor_phones": "",
    "inventory_digest_enabled": "0",
    # دفتر الأستاذ العام — يُفعَّل من /admin/gl
    "gl_enabled": "0",
    "gl_post_mode": "live",
    # الفندق — الحد الأدنى للدفع عند الحجز (0 | 50 | 100)
    "hotel_booking_prepayment_percent": "100",
    "hotel_maintenance_phone": "",
    "hotel_maintenance_name": "",
    "hotel_cleaning_phone": "",
    "hotel_cleaning_name": "",
    "hotel_portal_enabled": "0",
    "hotel_store_enabled": "1",
    "hotel_online_staff_phone": "",
    "hotel_reception_phone": "",
    # واتساب موظف المطعم لطلبات المتجر الأونلاين /shop (والشات)
    "shop_online_staff_phone": "",
    # ساعات عمل طلبات /shop (إنفاذ اختياري)
    "shop_hours_enforce": "0",
    "shop_hours_open": "10:00",
    "shop_hours_close": "23:00",
    "shop_hours_open_weekdays": "0,1,2,3,4,5,6",
    "shop_hours_closed_message": "",
    # مركز الإشعارات → واتساب (تشغيل + أرقام حسب القسم / إشعار)
    "activity_hub_whatsapp_enabled": "0",
    "activity_hub_whatsapp_phone": "",
    "activity_hub_wa_phones_hotel": "",
    "activity_hub_wa_phones_sales": "",
    "activity_hub_wa_phones_inventory": "",
    "activity_hub_wa_phones_finance": "",
    "activity_hub_wa_phones_hr": "",
    "activity_hub_wa_phones_customers": "",
    "activity_hub_wa_phones_messaging": "",
    "activity_hub_wa_phones_system": "",
    "activity_hub_wa_phones_events": "",
    "activity_hub_wa_event_phones_hotel_shift_overdue": "",
    "hotel_guest_id_types_json": "",
    "hotel_shift_schedules_json": "",
    "hotel_shift_overdue_minutes": "30",
    "hotel_shift_supervisor_phone": "",
    "hotel_shift_overdue_alert_enabled": "1",
    "hotel_shift_allow_next_shift_carry": "1",
    "hotel_shift_allow_treasury_close": "1",
    "pos_shift_allow_next_shift_carry": "1",
    "pos_shift_allow_treasury_close": "1",
    # تسكين ويوم التشغيل الفندقي (Business Day)
    "hotel_checkin_stay_policy_enabled": "1",
    "hotel_default_check_in_time": "14:00",
    "hotel_full_night_hours": "12",
    "hotel_half_night_hours": "6",
    "hotel_business_day_cutoff": "12:00",
    "hotel_early_checkin_policy": "FREE",
    "hotel_early_checkin_fixed_amount": "0",
    "hotel_early_checkin_percent": "50",
    # مغادرة متأخرة — تنبيه + احتساب ليلة تلقائي (أدمن فقط عبر إعدادات الحجز)
    "hotel_late_checkout_enabled": "1",
    "hotel_default_check_out_time": "12:00",
    "hotel_late_checkout_grace_hours": "3",
    "hotel_checkout_reminder_lead_hours": "3",
    "hotel_late_checkout_last_tick_at": "",
    "hotel_late_checkout_grace_fee": "FREE",
    "hotel_late_checkout_grace_fee_fixed": "0",
    # No Show / إلغاء — لا حذف للحجز؛ حالات واضحة + سياسة رسوم
    "hotel_auto_no_show_enabled": "1",
    "hotel_no_show_cutoff_time": "23:59",
    "hotel_cancel_before_arrival_fee": "FULL_REFUND",
    "hotel_cancel_after_checkin_fee": "FIRST_NIGHT",
    "hotel_no_show_fee": "FIRST_NIGHT",
    "hotel_no_show_fee_fixed": "0",
    "hotel_no_show_last_tick_at": "",
    # مطالبة يومية لمن عليه متبقٍ (منع تراكم الدين)
    "hotel_guest_reminders_enabled": "1",
    "hotel_daily_balance_claim_enabled": "1",
    "hotel_guest_reminders_last_date": "",
    # إقفال يومي تلقائي بعد انتهاء اليوم التقويمي (توقيت المحل)
    "hotel_daily_close_auto_enabled": "1",
    "hotel_daily_close_last_date": "",
    "pos_send_receipt_whatsapp": "1",
    "hotel_send_receipt_whatsapp": "1",
    # رموز اعتماد المشرف (OTP واتساب) — يتحكم بها الأدمن
    "otp_require_hotel_refund": "0",
    "otp_require_pos_refund": "1",
    "otp_require_hotel_cancel": "1",
    "hotel_cancel_admin_phone": "",
    # أقفال الأبواب / برمجة بطاقات النزلاء (proRFL.dll عبر وكيل محلي)
    "hotel_lock_encoder_enabled": "0",
    "hotel_lock_encoder_url": "http://127.0.0.1:9199",
    "hotel_lock_co_id": "",
    "hotel_lock_usb_flag": "1",
    "hotel_lock_public_doors": "1",
    "hotel_lock_deadbolt": "0",
}


def get_int(db: Session, key: str, default: int = 0) -> int:
    raw = get_setting(db, key, str(default))
    try:
        return int((raw or "").strip())
    except ValueError:
        return default


def get_bool(db: Session, key: str, default: bool = False) -> bool:
    raw = (get_setting(db, key, "1" if default else "0") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def get_public_base_url(db: Session | None = None) -> str:
    """الرابط العام للموقع — يفضّل PUBLIC_BASE_URL من البيئة ثم إعداد قاعدة البيانات.

    بيئات Baytak الشائعة:
    - تجريب: https://pos.baytak.ly
    - إنتاج: https://baytak.baytak.ly
    """
    env = (os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if env:
        return env
    if db is None:
        return ""
    return (get_setting(db, "public_base_url", "") or "").strip().rstrip("/")


def public_base_url_from_env() -> str:
    """قيمة PUBLIC_BASE_URL إن وُجدت (لعرضها في لوحة الإعدادات)."""
    return (os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")


def normalize_paper(code: str | None, fallback: str = "A5") -> str:
    if code and code in VALID_PAPER:
        return code
    return fallback if fallback in VALID_PAPER else "A5"


def normalize_orientation(code: str | None, fallback: str = "portrait") -> str:
    raw = (code or "").strip().lower()
    if raw in VALID_ORIENTATION:
        return raw
    return fallback if fallback in VALID_ORIENTATION else "portrait"


def receipt_paper_setting_key(domain=None) -> str:
    from modules.platform.business_domain import BusinessDomain

    raw = str(domain or BusinessDomain.RESTAURANT.value).strip().lower()
    if raw == BusinessDomain.HOTEL.value:
        return "hotel_print_paper_size"
    return "print_paper_size"


def receipt_orientation_setting_key(domain=None) -> str:
    from modules.platform.business_domain import BusinessDomain

    raw = str(domain or BusinessDomain.RESTAURANT.value).strip().lower()
    if raw == BusinessDomain.HOTEL.value:
        return "hotel_print_paper_orientation"
    return "print_paper_orientation"


def get_receipt_orientation(db: Session, domain=None) -> str:
    key = receipt_orientation_setting_key(domain)
    return normalize_orientation(get_setting(db, key, "portrait"), "portrait")


def receipt_printer_setting_key(domain=None) -> str:
    from modules.platform.business_domain import BusinessDomain

    raw = str(domain or BusinessDomain.RESTAURANT.value).strip().lower()
    if raw == BusinessDomain.HOTEL.value:
        return "hotel_receipt_printer_id"
    return "receipt_printer_id"


def get_receipt_paper_size(db: Session, domain=None) -> str:
    key = receipt_paper_setting_key(domain)
    default = "A5"
    return normalize_paper(get_setting(db, key, default), default)


def get_receipt_printer_id(db: Session, domain=None) -> int:
    key = receipt_printer_setting_key(domain)
    return get_int(db, key, 0)


def invalidate_settings_cache() -> None:
    """يُستدعى بعد تعديل الإعدادات لإعادة تحميل الذاكرة المؤقتة."""
    global _SETTINGS_MAP, _SETTINGS_MAP_AT
    with _SETTINGS_LOCK:
        _SETTINGS_MAP = None
        _SETTINGS_MAP_AT = 0.0


def get_settings_map(db: Session, *, max_age: float = _SETTINGS_CACHE_TTL) -> dict[str, str]:
    """تحميل كل الإعدادات باستعلام واحد — مع ذاكرة مؤقتة قصيرة."""
    global _SETTINGS_MAP, _SETTINGS_MAP_AT
    now = time.monotonic()
    with _SETTINGS_LOCK:
        if _SETTINGS_MAP is not None and now - _SETTINGS_MAP_AT <= max_age:
            return _SETTINGS_MAP
    rows = db.scalars(select(AppSetting)).all()
    loaded = {r.key: r.value for r in rows}
    with _SETTINGS_LOCK:
        _SETTINGS_MAP = loaded
        _SETTINGS_MAP_AT = now
    return loaded


def get_settings_bulk(
    db: Session, keys: dict[str, str], *, max_age: float = _SETTINGS_CACHE_TTL
) -> dict[str, str]:
    """قيم مفاتيح محددة مع افتراضيات — استعلام DB واحد كحد أقصى."""
    m = get_settings_map(db, max_age=max_age)
    return {k: (m.get(k) if m.get(k) is not None else default) for k, default in keys.items()}


def get_setting(db: Session, key: str, default: str = "") -> str:
    m = get_settings_map(db)
    val = m.get(key)
    return val if val is not None else default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    db.flush()
    with _SETTINGS_LOCK:
        if _SETTINGS_MAP is not None:
            _SETTINGS_MAP[key] = value


def ensure_default_settings(db: Session) -> None:
    changed = False
    m = get_settings_map(db, max_age=0)
    for k, v in DEFAULTS.items():
        if k not in m:
            db.add(AppSetting(key=k, value=v))
            changed = True
    if changed:
        db.commit()
        invalidate_settings_cache()


def get_paper_css(paper: str, orientation: str | None = None) -> dict[str, str]:
    """يرجّع قيم CSS المناسبة (size + margin + max_width) لكل مقاس واتجاه."""
    paper = normalize_paper(paper)
    orient = normalize_orientation(orientation, "portrait")
    if paper in ("80mm", "58mm"):
        orient = "portrait"
    landscape = orient == "landscape"

    if paper == "A4":
        return {
            "size": "A4 landscape" if landscape else "A4",
            "margin": "12mm",
            "max_width": "297mm" if landscape else "210mm",
            "orientation": orient,
        }
    if paper == "A5":
        return {
            "size": "A5 landscape" if landscape else "A5",
            "margin": "10mm",
            "max_width": "210mm" if landscape else "148mm",
            "orientation": orient,
        }
    if paper == "80mm":
        return {
            "size": "80mm auto",
            "margin": "3mm",
            "max_width": "80mm",
            "orientation": "portrait",
        }
    if paper == "58mm":
        return {
            "size": "58mm auto",
            "margin": "2mm",
            "max_width": "58mm",
            "orientation": "portrait",
        }
    return {
        "size": "A5",
        "margin": "10mm",
        "max_width": "148mm",
        "orientation": "portrait",
    }
