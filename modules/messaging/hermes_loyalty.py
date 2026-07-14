"""Hermes — الولاء، الإحالة، وملفات الشرح."""
from __future__ import annotations

import re
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
from modules.settings.service import get_setting

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
        f"⭐ **رصيد نقاطك:** {_fmt_pts(pts)} نقطة",
        f"💰 **قيمتها:** {_fmt_dinar(value)}",
    ]
    earn = ls.get("earn_per_dinar") or Decimal("0")
    redeem = ls.get("redeem_value_per_point") or Decimal("0")
    lines.append(
        f"ℹ️ تكسب {_fmt_pts(earn)} نقطة لكل 1 د.ل — "
        f"كل نقطة = {_fmt_dinar(redeem).replace(' د.ل', '')} د.ل عند الاستبدال."
    )
    return "\n".join(lines).replace("**", "")


def referral_summary_for_customer(db: Session, cust: Customer) -> str:
    rs = referral_settings(db)
    lines: list[str] = []
    if rs.get("referral_enabled"):
        code = ensure_referral_code(db, cust)
        lines.append(f"🎁 **كود إحالتك:** `{code}`")
        lines.append(
            "شاركه مع صديق — عند أول طلب له يستفيدان من نقاط إضافية "
            "(حسب إعدادات المطعم)."
        )
        lines.append("عند الدفع، يذكر صديقك الكود في الطلب أو نقطة البيع.")
    else:
        lines.append("برنامج الإحالة غير مفعّل حالياً.")
    return "\n".join(lines).replace("**", "").replace("`", "")


def loyalty_intro(db: Session) -> str:
    store = (get_setting(db, "store_name", "مطعم ومقهى روف") or "").strip()
    ls = loyalty_settings(db)
    if not ls.get("enabled"):
        return (
            f"برنامج الولاء في {store} غير مفعّل حالياً.\n"
            "اسأل الموظف أو اكتب «3» للمساعدة."
        )
    earn = ls.get("earn_per_dinar") or Decimal("0")
    redeem = ls.get("redeem_value_per_point") or Decimal("0")
    min_redeem = ls.get("min_points_to_redeem") or Decimal("0")
    body = (
        f"⭐ **برنامج الولاء — {store}**\n\n"
        f"• سجّل رقم هاتفك (زر 👤) لربط نقاطك.\n"
        f"• تكسب {_fmt_pts(earn)} نقطة لكل 1 د.ل مشتريات.\n"
        f"• كل نقطة = {_fmt_dinar(redeem).replace(' د.ل', '')} د.ل خصم.\n"
        f"• الحد الأدنى للاستبدال: {_fmt_pts(min_redeem)} نقطة.\n"
        f"• النقاط تُحسب بعد تأكيد الطلب والدفع."
    )
    return append_guides_footer(body.replace("**", ""), db, "loyalty", "general")


def handle_loyalty_message(
    db: Session, session: WebChatSession, text: str
) -> HermesReply | None:
    cleaned = _strip_fillers(text)
    t = _normalize(cleaned)
    if not (wants_loyalty_info(cleaned) or wants_referral_info(cleaned)):
        return None

    r = HermesReply()
    phone = (session.guest_phone or "").strip()

    if wants_referral_info(cleaned):
        if not phone:
            r.add(
                "🎁 **كود الإحالة**\n"
                "أضف رقم هاتفك من زر 👤 لعرض كودك الشخصي."
            )
            body = append_guides_footer(r.parts[0].body.replace("**", ""), db, "referral")
            r.parts[0].body = body
            return r
        cust = get_by_phone(db, phone, active_only=False)
        if cust is None:
            r.add(
                "🎁 **كود الإحالة**\n"
                "بعد أول طلب مؤكّد يُنشأ حسابك ويظهر كودك هنا.\n"
                "أكمل طلباً أو اسأل عن «الولاء»."
            )
            body = append_guides_footer(r.parts[0].body.replace("**", ""), db, "referral")
            r.parts[0].body = body
            return r
        body = referral_summary_for_customer(db, cust)
        body = append_guides_footer(body, db, "referral")
        r.add(body)
        return r

    # ولاء
    parts: list[str] = [loyalty_intro(db)]
    if phone:
        summary = loyalty_summary_for_phone(db, phone)
        if summary:
            parts.append("")
            parts.append("— **حسابك:**")
            parts.append(summary.replace("**", ""))
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


def loyalty_order_confirmation_note(db: Session, phone: str, sale_total: Decimal) -> str | None:
    """ملخص قصير عن النقاط يُعرض بعد تأكيد الطلب."""
    ls = loyalty_settings(db)
    if not ls.get("enabled"):
        return None
    earn = ls.get("earn_per_dinar") or Decimal("0")
    redeem = ls.get("redeem_value_per_point") or Decimal("0")
    lines = [
        "⭐ **برنامج الولاء:**",
        f"• بعد تأكيد الطلب والدفع تكسب {_fmt_pts(earn)} نقطة لكل 1 د.ل.",
        f"• كل نقطة = {_fmt_dinar(redeem).replace(' د.ل', '')} د.ل خصم لاحقاً.",
    ]
    try:
        total = Decimal(str(sale_total or 0))
        if total > 0 and earn > 0:
            est = (total * earn).quantize(Decimal("0.001"))
            lines.append(f"• تقدير نقاط هذا الطلب: {_fmt_pts(est)} نقطة.")
    except Exception:
        pass
    summary = loyalty_summary_for_phone(db, phone)
    if summary:
        lines.append("")
        lines.append(summary)
    else:
        lines.append("• رقمك مسجّل — ستُضاف النقاط لحسابك بعد التأكيد.")
    lines.append("اكتب «نقاطي» في أي وقت لمعرفة رصيدك.")
    return "\n".join(lines).replace("**", "")


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
    footer = append_guides_footer("", db, "loyalty", "referral")
    if footer.strip():
        lines.append(footer.strip())
    return "\n".join(lines)
