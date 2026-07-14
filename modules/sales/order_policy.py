"""سياسات مسار الطلب — بوابات الدفع/القيد وإعدادات KDS/POS (قابلة للضبط من الأدمن)."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from modules.sales.models import (
    ExternalOrderType,
    Sale,
    SaleContext,
    SaleStatus,
    TicketStatus,
)
from modules.sales.order_pipeline import _kitchen_agg, build_pipeline_view
from modules.settings.service import get_bool, get_int, get_setting, set_setting

KEY_KITCHEN_WORKFLOW_ENABLED = "pos_kitchen_workflow_enabled"
KEY_REQUIRE_SEND_BEFORE_PAY = "pos_require_kitchen_send_before_pay"
KEY_REQUIRE_SEND_BEFORE_ROOM = "pos_require_kitchen_send_before_room_charge"
KEY_REQUIRE_READY_TABLE = "pos_require_kds_ready_table"
KEY_REQUIRE_READY_PICKUP = "pos_require_kds_ready_pickup"
KEY_REQUIRE_READY_DELIVERY = "pos_require_kds_ready_delivery"
KEY_REQUIRE_READY_ROOM = "pos_require_kds_ready_room"
KEY_PAY_AFTER_SEND_PICKUP = "pos_pay_redirect_after_send_pickup"
KEY_SEND_CONFIRM = "pos_send_confirm_enabled"
KEY_SEND_CONFIRM_MIN = "pos_send_confirm_min_total"
KEY_SHOW_PIPELINE = "pos_show_pipeline_bar"
KEY_KDS_SOUND = "pos_kds_sound_enabled"
KEY_KDS_SOUND_REPEAT = "pos_kds_sound_repeat_sec"
KEY_KDS_SOUND_PROFILE = "pos_kds_sound_profile"
KEY_KDS_SOUND_VOLUME = "pos_kds_sound_volume"
KEY_KDS_POLL = "pos_kds_poll_sec"
KEY_UNSENT_REMINDER = "pos_unsent_reminder_sec"
KEY_TABLE_COMPLIMENTARY_ENABLED = "pos_table_complimentary_enabled"
KEY_TABLE_COMPLIMENTARY_PRODUCT_ID = "pos_table_complimentary_product_id"
KEY_TABLE_COMPLIMENTARY_NAME = "pos_table_complimentary_name"

DEFAULT_TABLE_COMPLIMENTARY_NAME = "ضيافة ترحيبية مجانية"

KDS_SOUND_PROFILES: tuple[str, ...] = ("beep", "chime", "bell", "kitchen", "alert")

KDS_SOUND_PROFILE_LABELS: dict[str, str] = {
    "beep": "نغمة قصيرة (افتراضي)",
    "chime": "جرس لطيف",
    "bell": "جرس مطبخ",
    "kitchen": "ثلاث نغمات",
    "alert": "تنبيه قوي",
}

POLICY_DEFAULTS: dict[str, str] = {
    KEY_KITCHEN_WORKFLOW_ENABLED: "1",
    KEY_REQUIRE_SEND_BEFORE_PAY: "1",
    KEY_REQUIRE_SEND_BEFORE_ROOM: "1",
    KEY_REQUIRE_READY_TABLE: "0",
    KEY_REQUIRE_READY_PICKUP: "0",
    KEY_REQUIRE_READY_DELIVERY: "0",
    KEY_REQUIRE_READY_ROOM: "0",
    KEY_PAY_AFTER_SEND_PICKUP: "0",
    KEY_SEND_CONFIRM: "1",
    KEY_SEND_CONFIRM_MIN: "0",
    KEY_SHOW_PIPELINE: "1",
    KEY_KDS_SOUND: "1",
    KEY_KDS_SOUND_REPEAT: "30",
    KEY_KDS_SOUND_PROFILE: "beep",
    KEY_KDS_SOUND_VOLUME: "80",
    KEY_KDS_POLL: "15",
    KEY_UNSENT_REMINDER: "0",
    KEY_TABLE_COMPLIMENTARY_ENABLED: "1",
    KEY_TABLE_COMPLIMENTARY_PRODUCT_ID: "0",
    KEY_TABLE_COMPLIMENTARY_NAME: DEFAULT_TABLE_COMPLIMENTARY_NAME,
}


@dataclass
class OrderPolicy:
    kitchen_workflow_enabled: bool = True
    require_kitchen_send_before_pay: bool = True
    require_kitchen_send_before_room_charge: bool = True
    require_kds_ready_table: bool = False
    require_kds_ready_pickup: bool = False
    require_kds_ready_delivery: bool = False
    require_kds_ready_room: bool = False
    pay_redirect_after_send_pickup: bool = False
    send_confirm_enabled: bool = True
    send_confirm_min_total: Decimal = Decimal("0")
    show_pipeline_bar: bool = True
    kds_sound_enabled: bool = True
    kds_sound_repeat_sec: int = 30
    kds_sound_profile: str = "beep"
    kds_sound_volume: int = 80
    kds_poll_sec: int = 15
    unsent_reminder_sec: int = 0
    table_complimentary_enabled: bool = True
    table_complimentary_product_id: int | None = None
    table_complimentary_name: str = DEFAULT_TABLE_COMPLIMENTARY_NAME

    def to_dict(self) -> dict:
        d = asdict(self)
        d["send_confirm_min_total"] = str(self.send_confirm_min_total)
        return d


def ensure_order_policy_defaults(db: Session) -> None:
    changed = False
    for k, v in POLICY_DEFAULTS.items():
        from modules.settings.models import AppSetting

        if db.get(AppSetting, k) is None:
            db.add(AppSetting(key=k, value=v))
            changed = True
    if changed:
        db.commit()


def _normalize_sound_profile(raw: str | None) -> str:
    p = (raw or "beep").strip().lower()
    return p if p in KDS_SOUND_PROFILES else "beep"


def load_order_policy(db: Session) -> OrderPolicy:
    min_raw = (get_setting(db, KEY_SEND_CONFIRM_MIN, "0") or "0").strip()
    try:
        min_total = Decimal(min_raw)
    except Exception:  # noqa: BLE001
        min_total = Decimal("0")
    comp_raw = (get_setting(db, KEY_TABLE_COMPLIMENTARY_PRODUCT_ID, "0") or "0").strip()
    try:
        comp_product_id = int(comp_raw)
    except ValueError:
        comp_product_id = 0
    return OrderPolicy(
        kitchen_workflow_enabled=get_bool(db, KEY_KITCHEN_WORKFLOW_ENABLED, True),
        require_kitchen_send_before_pay=get_bool(
            db, KEY_REQUIRE_SEND_BEFORE_PAY, True
        ),
        require_kitchen_send_before_room_charge=get_bool(
            db, KEY_REQUIRE_SEND_BEFORE_ROOM, True
        ),
        require_kds_ready_table=get_bool(db, KEY_REQUIRE_READY_TABLE, False),
        require_kds_ready_pickup=get_bool(db, KEY_REQUIRE_READY_PICKUP, False),
        require_kds_ready_delivery=get_bool(
            db, KEY_REQUIRE_READY_DELIVERY, False
        ),
        require_kds_ready_room=get_bool(db, KEY_REQUIRE_READY_ROOM, False),
        pay_redirect_after_send_pickup=get_bool(
            db, KEY_PAY_AFTER_SEND_PICKUP, False
        ),
        send_confirm_enabled=get_bool(db, KEY_SEND_CONFIRM, True),
        send_confirm_min_total=min_total,
        show_pipeline_bar=get_bool(db, KEY_SHOW_PIPELINE, True),
        kds_sound_enabled=get_bool(db, KEY_KDS_SOUND, True),
        kds_sound_repeat_sec=max(0, get_int(db, KEY_KDS_SOUND_REPEAT, 30)),
        kds_sound_profile=_normalize_sound_profile(
            get_setting(db, KEY_KDS_SOUND_PROFILE, "beep")
        ),
        kds_sound_volume=max(5, min(100, get_int(db, KEY_KDS_SOUND_VOLUME, 80))),
        kds_poll_sec=max(5, get_int(db, KEY_KDS_POLL, 15)),
        unsent_reminder_sec=max(0, get_int(db, KEY_UNSENT_REMINDER, 0)),
        table_complimentary_enabled=get_bool(
            db, KEY_TABLE_COMPLIMENTARY_ENABLED, True
        ),
        table_complimentary_product_id=comp_product_id if comp_product_id > 0 else None,
        table_complimentary_name=(
            get_setting(
                db, KEY_TABLE_COMPLIMENTARY_NAME, DEFAULT_TABLE_COMPLIMENTARY_NAME
            )
            or DEFAULT_TABLE_COMPLIMENTARY_NAME
        ).strip()[:120],
    )


def save_order_policy(db: Session, policy: OrderPolicy) -> None:
    set_setting(
        db,
        KEY_KITCHEN_WORKFLOW_ENABLED,
        "1" if policy.kitchen_workflow_enabled else "0",
    )
    set_setting(
        db,
        KEY_REQUIRE_SEND_BEFORE_PAY,
        "1" if policy.require_kitchen_send_before_pay else "0",
    )
    set_setting(
        db,
        KEY_REQUIRE_SEND_BEFORE_ROOM,
        "1" if policy.require_kitchen_send_before_room_charge else "0",
    )
    set_setting(
        db, KEY_REQUIRE_READY_TABLE, "1" if policy.require_kds_ready_table else "0"
    )
    set_setting(
        db, KEY_REQUIRE_READY_PICKUP, "1" if policy.require_kds_ready_pickup else "0"
    )
    set_setting(
        db,
        KEY_REQUIRE_READY_DELIVERY,
        "1" if policy.require_kds_ready_delivery else "0",
    )
    set_setting(
        db, KEY_REQUIRE_READY_ROOM, "1" if policy.require_kds_ready_room else "0"
    )
    set_setting(
        db,
        KEY_PAY_AFTER_SEND_PICKUP,
        "1" if policy.pay_redirect_after_send_pickup else "0",
    )
    set_setting(db, KEY_SEND_CONFIRM, "1" if policy.send_confirm_enabled else "0")
    set_setting(db, KEY_SEND_CONFIRM_MIN, str(policy.send_confirm_min_total))
    set_setting(db, KEY_SHOW_PIPELINE, "1" if policy.show_pipeline_bar else "0")
    set_setting(db, KEY_KDS_SOUND, "1" if policy.kds_sound_enabled else "0")
    set_setting(
        db, KEY_KDS_SOUND_REPEAT, str(max(0, policy.kds_sound_repeat_sec))
    )
    set_setting(
        db,
        KEY_KDS_SOUND_PROFILE,
        _normalize_sound_profile(policy.kds_sound_profile),
    )
    set_setting(
        db,
        KEY_KDS_SOUND_VOLUME,
        str(max(5, min(100, policy.kds_sound_volume))),
    )
    set_setting(db, KEY_KDS_POLL, str(max(5, policy.kds_poll_sec)))
    set_setting(
        db, KEY_UNSENT_REMINDER, str(max(0, policy.unsent_reminder_sec))
    )
    set_setting(
        db,
        KEY_TABLE_COMPLIMENTARY_ENABLED,
        "1" if policy.table_complimentary_enabled else "0",
    )
    set_setting(
        db,
        KEY_TABLE_COMPLIMENTARY_PRODUCT_ID,
        str(int(policy.table_complimentary_product_id or 0)),
    )
    set_setting(
        db,
        KEY_TABLE_COMPLIMENTARY_NAME,
        (policy.table_complimentary_name or DEFAULT_TABLE_COMPLIMENTARY_NAME).strip()[:120],
    )


def table_complimentary_product_id_for_sale(
    sale: Sale,
    policy: OrderPolicy | None,
) -> int | None:
    """منتج ضيافة يُحضّر للطاولات فقط ولا يظهر في فاتورة الزبون."""
    if policy is None or not policy.table_complimentary_enabled:
        return None
    if sale.context_type != SaleContext.TABLE:
        return None
    pid = int(policy.table_complimentary_product_id or 0)
    return pid if pid > 0 else None


def _sale_pay_context_key(sale: Sale) -> str:
    if sale.context_type == SaleContext.TABLE:
        return "table"
    if sale.context_type == SaleContext.ROOM:
        return "room"
    if sale.context_type == SaleContext.EXTERNAL:
        if sale.external_order_type == ExternalOrderType.DELIVERY:
            return "delivery"
        return "pickup"
    return "table"


def _require_ready_for_context(policy: OrderPolicy, ctx_key: str) -> bool:
    return {
        "table": policy.require_kds_ready_table,
        "pickup": policy.require_kds_ready_pickup,
        "delivery": policy.require_kds_ready_delivery,
        "room": policy.require_kds_ready_room,
    }.get(ctx_key, False)


def _kitchen_ready_for_pay(agg: dict[str, int]) -> bool:
    if not agg:
        return False
    pending = agg.get(TicketStatus.PENDING.value, 0) + agg.get("PENDING", 0)
    in_prog = (
        agg.get(TicketStatus.IN_PROGRESS.value, 0) + agg.get("IN_PROGRESS", 0)
    )
    if pending > 0 or in_prog > 0:
        return False
    return sum(agg.values()) > 0


def payment_block_reason(
    db: Session,
    sale: Sale,
    policy: OrderPolicy | None = None,
) -> str | None:
    if policy is None:
        policy = load_order_policy(db)
    if sale.status != SaleStatus.DRAFT:
        return "الطلب مغلق أو غير متاح للدفع."
    if not sale.lines:
        return "الطلب فارغ."

    if not policy.kitchen_workflow_enabled:
        return None

    ctx_key = _sale_pay_context_key(sale)

    if policy.require_kitchen_send_before_pay and not sale.sent_to_kitchen_at:
        return "يجب إرسال الطلب للتجهيز قبل التحصيل."

    if _require_ready_for_context(policy, ctx_key):
        agg = _kitchen_agg(db, [sale.id]).get(sale.id, {})
        if not _kitchen_ready_for_pay(agg):
            pipe = build_pipeline_view(db, sale, kitchen_agg=agg)
            return (
                f"بانتظار جاهزية المطبخ — {pipe.label}. "
                "انتظر اكتمال التجهيز ثم حاول التحصيل."
            )

    return None


def can_pay_sale(
    db: Session,
    sale: Sale,
    policy: OrderPolicy | None = None,
) -> bool:
    return payment_block_reason(db, sale, policy) is None


def room_charge_block_reason(
    db: Session,
    sale: Sale,
    policy: OrderPolicy | None = None,
) -> str | None:
    if policy is None:
        policy = load_order_policy(db)
    if sale.status != SaleStatus.DRAFT:
        return "الطلب مغلق."
    if not sale.lines:
        return "أضِف صنفاً للسلة أولاً."
    if not policy.kitchen_workflow_enabled:
        return None
    if (
        policy.require_kitchen_send_before_room_charge
        and not sale.sent_to_kitchen_at
    ):
        return "يجب إرسال الطلب للتجهيز قبل قيد حساب الشقة."
    if policy.require_kds_ready_room:
        agg = _kitchen_agg(db, [sale.id]).get(sale.id, {})
        if not _kitchen_ready_for_pay(agg):
            return "بانتظار جاهزية المطبخ قبل قيد حساب الشقة."
    return None


def can_charge_room(
    db: Session,
    sale: Sale,
    policy: OrderPolicy | None = None,
) -> bool:
    return room_charge_block_reason(db, sale, policy) is None


def policy_from_form(form: dict) -> OrderPolicy:
    def _on(key: str) -> bool:
        return str(form.get(key) or "").strip().lower() in (
            "on",
            "1",
            "true",
            "yes",
        )

    min_raw = str(form.get("send_confirm_min_total") or "0").strip()
    try:
        min_total = Decimal(min_raw)
    except Exception:  # noqa: BLE001
        min_total = Decimal("0")

    def _int_field(name: str, default: int, lo: int = 0) -> int:
        raw = str(form.get(name) or default).strip()
        try:
            return max(lo, int(raw))
        except ValueError:
            return default

    return OrderPolicy(
        kitchen_workflow_enabled=_on("kitchen_workflow_enabled"),
        require_kitchen_send_before_pay=_on("require_kitchen_send_before_pay"),
        require_kitchen_send_before_room_charge=_on(
            "require_kitchen_send_before_room_charge"
        ),
        require_kds_ready_table=_on("require_kds_ready_table"),
        require_kds_ready_pickup=_on("require_kds_ready_pickup"),
        require_kds_ready_delivery=_on("require_kds_ready_delivery"),
        require_kds_ready_room=_on("require_kds_ready_room"),
        pay_redirect_after_send_pickup=_on("pay_redirect_after_send_pickup"),
        send_confirm_enabled=_on("send_confirm_enabled"),
        send_confirm_min_total=min_total,
        show_pipeline_bar=_on("show_pipeline_bar"),
        kds_sound_enabled=_on("kds_sound_enabled"),
        kds_sound_repeat_sec=_int_field("kds_sound_repeat_sec", 30, 0),
        kds_sound_profile=_normalize_sound_profile(
            str(form.get("kds_sound_profile") or "beep")
        ),
        kds_sound_volume=max(
            5, min(100, _int_field("kds_sound_volume", 80, 5))
        ),
        kds_poll_sec=_int_field("kds_poll_sec", 15, 5),
        unsent_reminder_sec=_int_field("unsent_reminder_sec", 0, 0),
        table_complimentary_enabled=_on("table_complimentary_enabled"),
        table_complimentary_product_id=_int_field(
            "table_complimentary_product_id", 0, 0
        )
        or None,
        table_complimentary_name=(
            str(form.get("table_complimentary_name") or DEFAULT_TABLE_COMPLIMENTARY_NAME)
            .strip()[:120]
        ),
    )
