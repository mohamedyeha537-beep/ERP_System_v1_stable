"""Hermes — تقييم الخدمة بعد الطلب."""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from modules.messaging.chat_order_service import PHASE_FEEDBACK, PHASE_COMPLETED
from modules.messaging.hermes_catalog import HermesReply, _normalize, _strip_fillers
from modules.messaging.models import WebChatSession

_RATING_WORDS = ("قيّم", "قيم", "تقييم", "rating", "rate", "رأيك", "رايك", "feedback")


def wants_feedback_intent(text: str) -> bool:
    t = _normalize(text)
    return any(w in t for w in _RATING_WORDS)


def _parse_rating(text: str) -> int | None:
    t = _normalize(text)
    m = re.search(r"\b([1-5])\b", t)
    if m:
        return int(m.group(1))
    stars = text.count("⭐") + text.count("★")
    if 1 <= stars <= 5:
        return stars
    word_map = {
        "واحد": 1,
        "اثنين": 2,
        "ثلاثة": 3,
        "ثلاث": 3,
        "اربعة": 4,
        "أربعة": 4,
        "خمسة": 5,
        "خمس": 5,
    }
    for w, n in word_map.items():
        if w in t:
            return n
    return None


def feedback_prompt() -> str:
    return (
        "⭐ نقدّر رأيك!\n"
        "اضغط على النجوم الصفراء أسفل المحادثة لتقييم تجربتك.\n"
        "يمكنك بعد ذلك كتابة تعليق اختياري في خانة الرسائل."
    )


def _looks_like_feedback_comment(text: str) -> bool:
    """تعليق اختياري واضح — وليس سؤالاً أو طلباً جديداً."""
    cleaned = _strip_fillers(text)
    t = _normalize(cleaned)
    if not t or len(t) < 3:
        return False
    if "؟" in text or "?" in text:
        return False
    from modules.messaging.hermes_order_flow import wants_order_intent

    if wants_order_intent(cleaned):
        return False
    from modules.messaging.hermes_loyalty import wants_loyalty_info, wants_referral_info
    from modules.messaging.hermes_catalog import wants_human_agent

    if wants_loyalty_info(cleaned) or wants_referral_info(cleaned) or wants_human_agent(text):
        return False
    question_words = (
        "هل", "كيف", "متى", "وين", "اين", "أين", "لماذا", "ليه", "ممكن",
        "عندكم", "عندك", "في", "فين", "كم", "وش", "ايش", "what", "how", "when",
    )
    if any(t.startswith(w) or f" {w} " in f" {t} " for w in question_words):
        return False
    menu_words = ("منيو", "menu", "قائمة", "مشروبات", "مشويات", "بيتزا", "برجر")
    if any(w in t for w in menu_words):
        return False
    feedback_words = (
        "شكر", "ممتاز", "رائع", "سيء", "بطي", "سريع", "خدمة", "توصيل",
        "لذيذ", "طعم", "نظيف", "ودود", "تأخير", "تأخر", "ممتازة", "جميل",
    )
    return any(w in t for w in feedback_words)


def handle_feedback(
    db: Session, session: WebChatSession, text: str
) -> HermesReply | None:
    phase = (session.order_phase or "").strip()
    cleaned = _strip_fillers(text)
    rating = _parse_rating(cleaned)

    if phase == PHASE_FEEDBACK:
        if rating is None:
            if session.feedback_rating is not None:
                if _looks_like_feedback_comment(cleaned):
                    session.feedback_comment = cleaned.strip()[:2000]
                    session.order_phase = PHASE_COMPLETED
                    db.flush()
                    r = HermesReply()
                    r.add("🙏 شكراً لتعليقك — وصل للفريق.")
                    return r
                return None
            r = HermesReply()
            r.add("اضغط على النجوم أسفل المحادثة، أو اكتب رقماً من 1 إلى 5.")
            return r
        session.feedback_rating = rating
        session.order_phase = PHASE_COMPLETED
        t = cleaned.strip()
        m = re.search(r"[1-5]\s+(.+)", t)
        if m and len(m.group(1).strip()) >= 2:
            session.feedback_comment = m.group(1).strip()[:2000]
        db.flush()
        r = HermesReply()
        r.add(
            f"🙏 شكراً! تقييمك: {rating}/5\n"
            "نقدّر وقتك ونسعى دائماً للأفضل.\n\n"
            "للطلب مجدداً: «اطلب …» — للمنيو: «منيو» — للولاء: «نقاطي»."
        )
        return r

    if wants_feedback_intent(cleaned) and session.feedback_rating is None:
        if (session.order_phase or "") in ("submitted", "completed", "feedback"):
            r = HermesReply()
            r.add(feedback_prompt())
            session.order_phase = PHASE_FEEDBACK
            db.flush()
            return r

    return None


def start_feedback_after_order(db: Session, session: WebChatSession) -> None:
    session.order_phase = PHASE_FEEDBACK
