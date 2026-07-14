"""Hermes — ردود ذكاء اصطناعي (OpenAI-compatible) ضمن نطاق المطعم."""
from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.catalog.models import Product, ProductCategory, ProductKind
from modules.messaging.hermes_catalog import HermesReply
from modules.messaging.models import MessageDirection, WebChatMessage, WebChatSession
from modules.messaging.web_chat_config import web_chat_bot_name
from modules.settings.service import get_bool, get_setting

LOG = logging.getLogger("messaging.hermes_ai")


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def ai_chat_enabled(db: Session) -> bool:
    if not get_bool(db, "web_chat_ai_enabled", False):
        return False
    return bool((get_setting(db, "web_chat_ai_api_key", "") or "").strip())


def _menu_hint(db: Session) -> str:
    try:
        roots = db.execute(
            select(ProductCategory.name_ar, func.count(Product.id))
            .join(Product, Product.category_id == ProductCategory.id)
            .where(Product.is_active.is_(True), Product.kind == ProductKind.FINISHED)
            .group_by(ProductCategory.name_ar)
            .limit(6)
        ).all()
    except Exception:
        return "مشويات، مشروبات، بيتزا، سلطات"
    if not roots:
        return "اسأل عن «منيو» لعرض الأقسام"
    return "، ".join(f"{name} ({cnt} صنف)" for name, cnt in roots)


def _system_prompt(db: Session) -> str:
    store = (get_setting(db, "store_name", "مطعم ومقهى روف") or "مطعم ومقهى روف").strip()
    bot = web_chat_bot_name(db)
    menu = _menu_hint(db)
    return (
        f"أنت {bot}، مساعد ذكي لمطعم «{store}» على محادثة الويب.\n"
        "قواعد صارمة:\n"
        "1) تفهم كل اللهجات العربية (ليبية، خليجية، مصرية…) لكن تردّ فقط باللهجة الليبية "
        "أو العربية الفصحى البسيطة والواضحة.\n"
        "2) في ردودك استخدم مفردات ليبية مثل: شنو، نبي/نبيه، زين، باش، قولّي، معاك، "
        "نجهّزلك، يالّا — أو فصحى بسيطة: «ما الذي ترغب؟»، «الذي تريده»، «نحن هنا لمساعدتك».\n"
        "3) ممنوع في الردود: وش، تبيه، تبي، تبغى، تبغاه، نبغى، ابغى، حلو (تعجب خليجي)، يا بطل، خلينا، "
        "بدي، هيدا، شو (لبناني)، و أي ألفاظ خليجية أو لبنانية.\n"
        "4) ردّ قصيراً (2–4 جمل)، ودوداً، طبيعياً — ليس روبوتاً جامداً.\n"
        "5) نطاقك فقط: طعام، مشروبات، منيو، أسعار تقريبية، طلب، توصيل، استلام، ولاء.\n"
        "6) لا تختلق أصناف أو أسعاراً — وجّه للمنيو أو اطلب اسم صنف.\n"
        "7) إذا سؤال خارج المطعم: اعتذر بلطف وأعد للطلب.\n"
        "8) لا JSON ولا markdown معقد — نص عادي للزبون.\n"
        "9) إذا جوع/عطش: تعاطف + خطوة تالية («منيو» أو «اطلب …»).\n"
        f"أقسام تقريبية في النظام: {menu}.\n"
        "لا تذكر أنك نموذج ذكاء اصطناعي."
    )


def _recent_messages(db: Session, session: WebChatSession, *, limit: int = 10) -> list[dict]:
    rows = list(
        db.scalars(
            select(WebChatMessage)
            .where(WebChatMessage.session_id == session.id)
            .order_by(WebChatMessage.id.desc())
            .limit(limit)
        ).all()
    )
    rows.reverse()
    out: list[dict] = []
    for m in rows:
        role = "user" if m.direction == MessageDirection.INBOUND.value else "assistant"
        body = (m.body or "").strip()
        if body:
            out.append({"role": role, "content": body[:2000]})
    return out


def try_ai_reply(db: Session, session: WebChatSession, text: str) -> HermesReply | None:
    if not ai_chat_enabled(db):
        return None
    api_key = (get_setting(db, "web_chat_ai_api_key", "") or "").strip()
    base = (
        get_setting(db, "web_chat_ai_base_url", "https://api.openai.com/v1") or ""
    ).strip().rstrip("/")
    model = (get_setting(db, "web_chat_ai_model", "gpt-4o-mini") or "gpt-4o-mini").strip()
    if not api_key or not base:
        return None

    messages = [{"role": "system", "content": _system_prompt(db)}]
    messages.extend(_recent_messages(db, session))
    guest = (text or "").strip()
    if not guest:
        return None
    if not messages or messages[-1].get("content") != guest:
        messages.append({"role": "user", "content": guest})

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 280,
        "temperature": 0.75,
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25, context=_ssl_context()) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
        LOG.warning("hermes AI call failed: %s", exc)
        return None

    try:
        content = (raw["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return None
    if not content:
        return None
    r = HermesReply()
    r.add(content.replace("**", ""))
    return r
