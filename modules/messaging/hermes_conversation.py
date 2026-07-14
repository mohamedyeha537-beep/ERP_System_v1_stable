"""Hermes — فهم عبارات عامية (جوع، عطش، رغبة بالطلب) بردود طبيعية.

ردود البوت باللهجة الليبية أو العربية الفصحى البسيطة — لا خليجية ولا لبنانية.
"""
from __future__ import annotations

import random
import re

from sqlalchemy.orm import Session

from modules.messaging.hermes_catalog import HermesReply, _normalize
from modules.messaging.web_chat_config import web_chat_bot_name
from modules.settings.service import get_setting


def _has_any(t: str, words: tuple[str, ...]) -> bool:
    return any(w in t for w in words)


def _matches_pattern(t: str, pattern: str) -> bool:
    return bool(re.search(pattern, t))


def try_how_to_order_reply(db: Session, text: str) -> HermesReply | None:
    """شرح خطوات الطلب — قبل مسار «اطلب صنف»."""
    from modules.messaging.hermes_intent import is_how_to_order_question

    if not is_how_to_order_question(text):
        return None
    bot = web_chat_bot_name(db)
    r = HermesReply()
    r.add(
        f"الطلب سهل 😊 أنا {bot} معاك:\n\n"
        "1️⃣ اكتب «منيو» لعرض الأصناف\n"
        "2️⃣ أو «اطلب» + اسم الصنف (مثل: اطلب برجر ×2)\n"
        "3️⃣ راجع السلة ثم اكتب «تأكيد»\n"
        "4️⃣ اختر استلام أو توصيل وأكمل البيانات\n\n"
        "تقدر تبدأ الآن — اكتب «منيو» أو اسم اللي تريده."
    )
    return r


def try_conversation_reply(db: Session, text: str) -> HermesReply | None:
    """ردود ذكية محلية قبل رسالة «لم أفهم» — بدون API."""
    t = _normalize(_strip_small_talk(text))
    if not t or len(t) > 120:
        return None

    bot = web_chat_bot_name(db)
    store = (get_setting(db, "store_name", "مطعم ومقهى روف") or "مطعم ومقهى روف").strip()

    if _is_hunger(t):
        return _pick(
            db,
            (
                f"😊 فهمتك! أنا {bot} من {store} — هنا باش نساعدك تشبع.\n"
                "اكتب «منيو» أو شنو نبي ناكل (مثلاً: برجر، مشاوي)، "
                "أو قل «اطلب …» ونكمّل الطلب خطوة بخطوة.",
                f"أكيد، الجوع ما يتحمّل 😄 أنا {bot} معاك — "
                "شوف «منيو» أو اكتب اسم الصنف اللي تريده، ونجهّزلك الطلب.",
                f"وصلت الرسالة 👍 {bot} موجود باش يخدمك.\n"
                "قولّي شنو نبي ناكل أو اكتب «منيو» ونختارو مع بعض.",
            ),
        )

    if _is_thirst(t):
        return _pick(
            db,
            (
                f"🥤 زين! عندنا مشروبات باردة وسخانة.\n"
                f"اكتب «مشروبات» أو «منيو»، أو اسم المشروب اللي تريده — أنا {bot} في الخدمة.",
                f"فهمت — العطش أولوية 😊\n"
                "اسأل على «مشروبات» أو اكتب اسم العصير أو القهوة اللي تريده.",
            ),
        )

    if _is_want_to_eat(t):
        return _pick(
            db,
            (
                f"زين! 🛒 يالّا نطلب.\n"
                "اكتب «اطلب» + اسم الصنف (مثلاً: اطلب برجر ×2)، أو «منيو» للاختيار من القائمة.",
                f"تمام — {bot} جاهز.\n"
                "قولّي الأصناف أو اكتب «منيو» و«تأكيد» لما تخلّي.",
            ),
        )

    if _is_suggest_me(t):
        return _pick(
            db,
            (
                f"اقتراحي: ابدأ بـ «منيو» — أو قول «مشويات» / «مشروبات» / «بيتزا» "
                f"وأعرضلك الأصناف المتوفرة من {store}.",
                "ما عندي مفضّل واحد 😄 — لكن «مشويات» و«برجر» دايماً ناجحين.\n"
                "اكتب «منيو» أو «مشويات» وشوف بنفسك.",
            ),
        )

    if _is_thanks(t):
        return _pick(
            db,
            (
                f"العفو! 😊 {bot} موجود لو احتجت أي حاجة للطلب.",
                "تسلم 🙏 — أي وقت، نكمّل الطلب من هنا.",
            ),
        )

    return None


def _strip_small_talk(text: str) -> str:
    t = (text or "").strip()
    for prefix in ("يا ", "ي ", "please ", "plz "):
        if t.lower().startswith(prefix):
            t = t[len(prefix) :].strip()
    return t


def _is_hunger(t: str) -> bool:
    if t in ("جعان", "جوعان", "مجوع", "جوع", "hungry"):
        return True
    return _has_any(
        t,
        (
            "جعان",
            "جوع",
            "جوعان",
            "مجوع",
            "مفخد",
            "فاضي",
            "بطني",
            "تجوع",
            "hungry",
        ),
    ) or _matches_pattern(t, r"(نبي|نبغ|بغيت|بدي|عايز|ابي|اريد|حاب).*(اكل|ناكل|شي\s*اكل)")


def _is_thirst(t: str) -> bool:
    if t in ("عطشان", "عطش", "thirsty"):
        return True
    return _has_any(t, ("عطش", "عطشان", "thirsty", "تعطش"))


def _is_want_to_eat(t: str) -> bool:
    return _matches_pattern(
        t,
        r"(نبي|نبغ|بغيت|بدي|عايز|ابي|اريد|حاب|نطلب|اطلب|نبي\s*نطلب).*(اكل|ناكل|شي|طلب|غدا|عشا|فطور|غداء|عشاء)",
    ) or _has_any(t, ("نبي ناكل", "نبغى ناكل", "نبغي ناكل", "بدي اكل", "عايز اكل", "نبي نطلب", "بغيت ناكل"))


def _is_suggest_me(t: str) -> bool:
    return _has_any(
        t,
        (
            "اقترح",
            "ترشح",
            "شنو اختار",
            "شنية اختار",
            "مالك تاكل",
            "ما نعرف",
            "ما ادري",
            "ما ندري",
            "شن تنصح",
            "شنو تنصح",
            "اش تنصح",
            "وش تنصح",
        ),
    )


def _is_thanks(t: str) -> bool:
    if len(t.split()) > 4:
        return False
    return _has_any(t, ("شكر", "تسلم", "مشكور", "thanks", "thank you", "thx"))


def _pick(_db: Session, options: tuple[str, ...]) -> HermesReply:
    r = HermesReply()
    r.add(random.choice(options))
    return r
