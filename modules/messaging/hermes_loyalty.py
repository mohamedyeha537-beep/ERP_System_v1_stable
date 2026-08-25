"""Hermes — الولاء، الإحالة، وملفات الشرح."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from modules.customers.models import Customer
from modules.customers.referral_service import ensure_referral_code, referral_settings
from modules.customers.service import (
    get_by_phone,
    loyalty_settings,
    points_to_dinars,
)
from modules.messaging.hermes_catalog import HermesReply, _normalize, _strip_fillers
from modules.messaging.models import WebChatSession
from modules.messaging.web_chat_guides import append_guides_footer
from modules.settings.service import get_setting, set_setting

_LOYALTY_WORDS = (
    "ولاء",
    "نقاط",
    "نقاطي",
    "رصيد",
    "مكافآت",
    "مكافات",
    "loyalty",
    "points",
)

_REFERRAL_WORDS = (
    "احالة",
    "إحالة",
    "كود",
    "كودي",
    "referral",
    "invite",
    "دعوة",
)

LOYALTY_INTRO_SETTING_KEY = "loyalty_intro_text"

DEFAULT_LOYALTY_INTRO_TEXT = (
    "⭐ برنامج الولاء — {store_name}\n\n"
    "• بعد تأكيد الطلب والدفع تكسب {earn_per_dinar} نقطة لكل 1 د.ل.\n"
    "• كل نقطة = {redeem_value_per_point} د.ل خصم لاحقاً.\n"
    "• الحد الأدنى للاستبدال: {min_points_to_redeem} نقطة.\n"
    "• النقاط تُحسب بعد تأكيد الطلب والدفع."
)

# جملة قديمة في قوالب محفوظة — الرصيد يُرسل تلقائياً بعد الطلب فلا داعي لها.
_OBSOLETE_NUGATI_HINT_MARKERS = (
    "اكتب «نقاطي»",
    "اكتب <نقاطي>",
    "اكتب \"نقاطي\"",
    "اكتب 'نقاطي'",
    "لمعرفة رصيدك",
)


def _strip_obsolete_nugati_hint(text: str) -> str:
    lines = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s and any(m in s for m in _OBSOLETE_NUGATI_HINT_MARKERS):
            continue
        lines.append(line)
    return "\n".join(lines).rstrip()


def wants_loyalty_info(text: str) -> bool:
    t = _normalize(text)
    return any(w in t for w in _LOYALTY_WORDS)


def wants_referral_info(text: str) -> bool:
    t = _normalize(text)
    return any(w in t for w in _REFERRAL_WORDS)


def _fmt_pts(value: Decimal) -> str:
    return f"{Decimal(str(value or 0)).quantize(Decimal('0.001'))}"


def _fmt_dinar(value: Decimal) -> str:
    return f"{Decimal(str(value or 0)).quantize(Decimal('0.001'))} د.ل"


def loyalty_summary_for_phone(db: Session, phone: str) -> str | None:
    ls = loyalty_settings(db)
    if not ls.get("enabled"):
        return None
    cust = get_by_phone(db, phone, active_only=False)
    if cust is None:
        return None
    pts = Decimal(str(cust.points_balance or 0))
    value = points_to_dinars(db, pts)
    lines = [
        f"⭐ رصيد نقاطك: {_fmt_pts(pts)} نقطة",
        f"💰 قيمتها: {_fmt_dinar(value)}",
    ]
    return "\n".join(lines)


def referral_summary_for_customer(db: Session, cust: Customer) -> str:
    rs = referral_settings(db)
    lines: list[str] = []
    if rs.get("referral_enabled"):
        code = ensure_referral_code(db, cust)
        lines.append(f"🎁 كود إحالتك: {code}")
        lines.append(
            "شاركه مع صديق — عند أول طلب له يستفيدان من نقاط إضافية "
            "(حسب إعدادات المطعم)."
        )
        lines.append("عند الدفع، يذكر صديقك الكود في الطلب أو نقطة البيع.")
    else:
        lines.append("برنامج الإحالة غير مفعّل حالياً.")
    return "\n".join(lines)


def _loyalty_intro_placeholders(db: Session) -> dict[str, str]:
    store = (get_setting(db, "store_name", "مطعم ومقهى روف") or "").strip()
    ls = loyalty_settings(db)
    earn = ls.get("earn_per_dinar") or Decimal("0")
    redeem = ls.get("redeem_value_per_point") or Decimal("0")
    min_redeem = ls.get("min_points_to_redeem") or Decimal("0")
    return {
        "store_name": store or "المطعم",
        "earn_per_dinar": _fmt_pts(earn),
        "redeem_value_per_point": _fmt_pts(redeem),
        "min_points_to_redeem": _fmt_pts(min_redeem),
    }


def get_loyalty_intro_template(db: Session) -> str:
    raw = (get_setting(db, LOYALTY_INTRO_SETTING_KEY, "") or "").strip()
    cleaned = _strip_obsolete_nugati_hint(raw) if raw else ""
    return cleaned or DEFAULT_LOYALTY_INTRO_TEXT


def save_loyalty_intro_template(db: Session, text: str) -> None:
    set_setting(
        db,
        LOYALTY_INTRO_SETTING_KEY,
        _strip_obsolete_nugati_hint((text or "").strip()),
    )


def render_loyalty_intro_text(db: Session) -> str:
    """نص شرح نظام الولاء (قابل للتعديل من لوحة الإدارة)."""
    ls = loyalty_settings(db)
    values = _loyalty_intro_placeholders(db)
    if not ls.get("enabled"):
        return (
            f"برنامج الولاء في {values['store_name']} غير مفعّل حالياً.\n"
            "اسأل الموظف أو اكتب «3» للمساعدة."
        )
    template = get_loyalty_intro_template(db)
    try:
        body = template.format(**values)
    except Exception:
        body = DEFAULT_LOYALTY_INTRO_TEXT.format(**values)
    body = _strip_obsolete_nugati_hint(body.strip())
    return append_guides_footer(db, body, "loyalty", "general")


def loyalty_intro(db: Session) -> str:
    return render_loyalty_intro_text(db)


def customer_received_loyalty_intro(db: Session, phone: str) -> bool:
    cust = get_by_phone(db, phone, active_only=False)
    if cust is None:
        return False
    return getattr(cust, "loyalty_intro_sent_at", None) is not None


def mark_loyalty_intro_sent(db: Session, phone: str) -> None:
    cust = get_by_phone(db, phone, active_only=False)
    if cust is None:
        return
    if getattr(cust, "loyalty_intro_sent_at", None) is not None:
        return
    cust.loyalty_intro_sent_at = datetime.now(timezone.utc)


def take_loyalty_intro_if_needed(db: Session, phone: str) -> str | None:
    """يرجع شرح الولاء مرة واحدة فقط للزبون، ثم يعلّمه كمُرسَل."""
    phone = (phone or "").strip()
    if not phone:
        return None
    ls = loyalty_settings(db)
    if not ls.get("enabled"):
        return None
    if customer_received_loyalty_intro(db, phone):
        return None
    text = render_loyalty_intro_text(db).strip()
    if not text:
        return None
    mark_loyalty_intro_sent(db, phone)
    return text


def handle_loyalty_message(
    db: Session, session: WebChatSession, text: str
) -> HermesReply | None:
    cleaned = _strip_fillers(text)
    if not (wants_loyalty_info(cleaned) or wants_referral_info(cleaned)):
        return None

    r = HermesReply()
    phone = (session.guest_phone or "").strip()

    if wants_referral_info(cleaned):
        if not phone:
            r.add(
                "🎁 كود الإحالة\n"
                "أضف رقم هاتفك من زر 👤 لعرض كودك الشخصي."
            )
            body = append_guides_footer(db, r.parts[0].body, "referral")
            r.parts[0].body = body
            return r
        cust = get_by_phone(db, phone, active_only=False)
        if cust is None:
            r.add(
                "🎁 كود الإحالة\n"
                "بعد أول طلب مؤكّد يُنشأ حسابك ويظهر كودك هنا.\n"
                "أكمل طلباً أو اسأل عن «الولاء»."
            )
            body = append_guides_footer(db, r.parts[0].body, "referral")
            r.parts[0].body = body
            return r
        body = referral_summary_for_customer(db, cust)
        body = append_guides_footer(db, body, "referral")
        r.add(body)
        return r

    # ولاء — طلب صريح: أرسل الشرح وعلّمه كمُرسَل حتى لا يُعاد مع كل طلب
    parts: list[str] = [loyalty_intro(db)]
    if phone:
        mark_loyalty_intro_sent(db, phone)
        summary = loyalty_summary_for_phone(db, phone)
        if summary:
            parts.append("")
            parts.append("— حسابك:")
            parts.append(summary)
        else:
            parts.append("")
            parts.append(
                "📱 رقمك غير مسجّل بعد — أكمل طلباً وسيُربط حسابك تلقائياً."
            )
    else:
        parts.append("")
        parts.append("📱 أضف رقم هاتفك من زر 👤 لمعرفة رصيد نقاطك.")

    r.add("\n".join(parts))
    return r


def loyalty_order_event_note(db: Session, phone: str, sale_total: Decimal) -> str | None:
    """ملاحظة قصيرة خاصة بالطلب الحالي فقط (بدون إعادة شرح النظام)."""
    ls = loyalty_settings(db)
    if not ls.get("enabled"):
        return None
    earn = ls.get("earn_per_dinar") or Decimal("0")
    lines: list[str] = []
    try:
        total = Decimal(str(sale_total or 0))
        if total > 0 and earn > 0:
            est = (total * earn).quantize(Decimal("0.001"))
            lines.append(f"⭐ تقدير نقاط هذا الطلب: {_fmt_pts(est)} نقطة.")
    except Exception:
        pass
    summary = loyalty_summary_for_phone(db, phone)
    if summary:
        lines.append(summary)
    elif phone:
        lines.append("• رقمك مسجّل — ستُضاف النقاط لحسابك بعد التأكيد.")
    return "\n".join(lines) if lines else None


def loyalty_order_confirmation_note(db: Session, phone: str, sale_total: Decimal) -> str | None:
    """توافق خلفي — ملاحظة الطلب فقط (بدون شرح النظام)."""
    return loyalty_order_event_note(db, phone, sale_total)


def profile_saved_loyalty_message(db: Session, phone: str) -> str | None:
    """رسالة قصيرة بعد حفظ الملف الشخصي."""
    ls = loyalty_settings(db)
    if not ls.get("enabled"):
        return None
    lines = ["✓ تم حفظ بياناتك."]
    summary = loyalty_summary_for_phone(db, phone)
    if summary:
        lines.append(summary)
    else:
        lines.append("⭐ ستُربط نقاط الولاء بأول طلب مؤكّد.")
    cust = get_by_phone(db, phone, active_only=False)
    if cust is not None and referral_settings(db).get("referral_enabled"):
        code = ensure_referral_code(db, cust)
        lines.append(f"🎁 كود إحالتك: {code}")
    footer = append_guides_footer(db, "", "loyalty", "referral")
    if footer.strip():
        lines.append(footer.strip())
    return "\n".join(lines)
