"""Hermes — فهم المنيو من الكatalog (فئات، أصناف، أسعار، صور)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

if TYPE_CHECKING:
    from modules.messaging.models import WebChatSession

from modules.catalog.models import Product, ProductCategory, ProductKind
from modules.kds.section_rules import (
    match_menu_section_code,
    section_display_name,
)
from modules.printing.models import KitchenSection
from modules.settings.service import get_setting

_SECTION_LABELS = {
    "GRILL": "المشويات",
    "DRINKS": "المشروبات",
    "SALAD": "السلطات والمقبلات",
    "PASTA": "الأرز والمكرونة والبيتزا",
}

_MENU_INTENT_WORDS = (
    "منيو",
    "menu",
    "قائمة",
    "قائمه",
    "الاقسام",
    "الأقسام",
    "اقسام",
    "أقسام",
    "عندكم",
    "عندك",
    "ايه عند",
    "إيه عند",
    "ماذا عند",
    "وش عند",
    "فيه عند",
    "تبيع",
    "تبيعون",
    "متوفر",
)

_GREETING_WORDS = (
    "مرحب",
    "اهلا",
    "أهلا",
    "السلام",
    "سلام",
    "صباح",
    "مساء",
    "hi",
    "hello",
    "hey",
)

_PRICE_WORDS = ("سعر", "كم", "بكام", "بكم", "ثمن", "تكلف")

_FILLER_PREFIX = re.compile(
    r"^(?:طيب|تمام|اوك|أوك|ok|okay|please|pls|لو\s*سمحت|ممكن|"
    r"عادي|حلو|please)\s+",
    re.IGNORECASE,
)


def _strip_fillers(text: str) -> str:
    """يزيل مقدمات محادثة («طيب»، «تمام»…) قبل فهم السؤال."""
    t = (text or "").strip()
    if not t:
        return t
    for _ in range(4):
        norm = _normalize(t)
        m = _FILLER_PREFIX.match(norm)
        if not m:
            break
        # قص بنفس طول المقدمة على النص الأصلي تقريباً
        cut = len(t) - len(norm) + m.end()
        t = t[cut:].strip()
        if not t:
            break
    return t or (text or "").strip()


def wants_human_agent(text: str) -> bool:
    """هل يطلب الزبون التحدث مع موظف؟"""
    t = _normalize(text)
    if not t:
        return False
    if t in ("3", "٣"):
        return True
    if "موظف" in t or "موظفه" in t:
        return True
    if "خدمه عمل" in t or "خدمة عمل" in t:
        return True
    if "شخص حقيق" in t or "انسان" in t or "إنسان" in t:
        return True
    phrases = (
        "كلموني",
        "كلمني",
        "يكلموني",
        "يكلمني",
        "حد يرد",
        "حد يكلمن",
        "حد يكلم",
        "تكلم مع",
        "تواصل مع",
        "حاب اكلم",
        "حابب اكلم",
        "عايز اكلم",
        "ابغى اكلم",
        "اريد اكلم",
        "بدي احكي",
        "كلمني موظف",
        "خلي موظف",
        "خلى موظف",
        "خليني موظف",
    )
    for p in phrases:
        if _normalize(p) in t:
            return True
    if re.search(r"خ(لى|li|لي).*(كلم|رد)", t):
        return True
    if re.search(r"(عايز|ابغ|ابي|اريد|حاب|بدي).*(موظف|حد|انسان|شخص)", t):
        return True
    return False


def human_handoff_reply(db: Session) -> HermesReply:
    store = (get_setting(db, "store_name", "مطعم ومقهى روف") or "").strip()
    r = HermesReply()
    r.add(
        f"تمام 👤\n"
        f"وصلنا طلبك للتحويل لموظف من {store}.\n"
        f"سيرد عليك هنا في المحادثة — غالباً خلال دقائق.\n"
        f"💡 أضف اسمك ورقم هاتفك من زر 👤 بالأعلى لتسريع التواصل."
    )
    return r


def human_waiting_reply() -> HermesReply:
    r = HermesReply()
    r.add(
        "✓ رسالتك وصلت للفريق.\n"
        "موظف سيطّلع عليها ويرد هنا قريباً — شكراً لانتظارك 🙏"
    )
    return r


@dataclass
class HermesReplyPart:
    body: str
    image_url: str | None = None


@dataclass
class HermesReply:
    parts: list[HermesReplyPart] = field(default_factory=list)

    def add(self, body: str, image_url: str | None = None) -> None:
        text = (body or "").strip()
        if not text and not image_url:
            return
        self.parts.append(HermesReplyPart(body=text or " ", image_url=image_url))


def _normalize(text: str) -> str:
    t = (text or "").strip().lower()
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    t = re.sub(r"\s+", " ", t)
    return t


def _tokens(text: str) -> list[str]:
    return [w for w in re.split(r"[\s,،.!?؟]+", _normalize(text)) if len(w) >= 2]


def _fmt_price(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{Decimal(str(value)).quantize(Decimal('0.001'))} د.ل"


def _product_image_url(product: Product) -> str | None:
    from modules.catalog.uploads import product_image_public_url

    return product_image_public_url(product.image_filename)


def _active_sellable_products(db: Session) -> list[Product]:
    return list(
        db.scalars(
            select(Product)
            .options(selectinload(Product.category))
            .where(
                Product.is_active.is_(True),
                Product.kind == ProductKind.FINAL_SELLABLE,
            )
            .order_by(Product.name_ar.asc())
        ).all()
    )


def _category_ids_with_children(db: Session, root_id: int) -> set[int]:
    ids = {root_id}
    pending = [root_id]
    while pending:
        pid = pending.pop()
        for cid in db.scalars(
            select(ProductCategory.id).where(ProductCategory.parent_id == pid)
        ).all():
            cid = int(cid)
            if cid not in ids:
                ids.add(cid)
                pending.append(cid)
    return ids


def _products_in_category(db: Session, category_id: int) -> list[Product]:
    ids = _category_ids_with_children(db, category_id)
    return [
        p
        for p in _active_sellable_products(db)
        if p.category_id in ids and p.sell_price is not None
    ]


def _match_category_by_name(db: Session, text: str) -> ProductCategory | None:
    t = _normalize(text)
    cats = db.scalars(select(ProductCategory).order_by(ProductCategory.sort_order)).all()
    best: ProductCategory | None = None
    best_len = 0
    for cat in cats:
        name = _normalize(cat.name_ar or "")
        if not name:
            continue
        if name in t or t in name:
            if len(name) > best_len:
                best = cat
                best_len = len(name)
        for tok in _tokens(name):
            if tok in t and len(tok) > best_len:
                best = cat
                best_len = len(tok)
    return best


def _kitchen_section_id(db: Session, code: str) -> int | None:
    row = db.execute(
        select(KitchenSection.id).where(
            KitchenSection.code == code,
            KitchenSection.is_active.is_(True),
        )
    ).scalar_one_or_none()
    return int(row) if row is not None else None


def _products_for_section_code(db: Session, code: str) -> list[Product]:
    sec_id = _kitchen_section_id(db, code)
    products = _active_sellable_products(db)
    if not sec_id:
        return [p for p in products if p.sell_price is not None]

    matched: list[Product] = []
    for p in products:
        if p.sell_price is None:
            continue
        if p.kitchen_section_id == sec_id:
            matched.append(p)
            continue
        cat = p.category
        if cat is not None and cat.kitchen_section_id == sec_id:
            matched.append(p)
            continue
        if cat is not None and match_menu_section_code(cat.name_ar or "") == code:
            matched.append(p)
            continue
        if match_menu_section_code(p.name_ar or "") == code:
            matched.append(p)
    return matched


def _score_product(text: str, product: Product) -> int:
    t = _normalize(text)
    name = _normalize(product.name_ar or "")
    if not name:
        return 0
    score = 0
    if name == t:
        score += 200
    elif name in t:
        score += 150
    elif t in name:
        score += 120
    for tok in _tokens(t):
        if len(tok) >= 3 and tok in name:
            score += 25
    if product.category:
        cname = _normalize(product.category.name_ar or "")
        if cname and cname in t:
            score += 15
    return score


def find_products_by_text(db: Session, text: str, *, limit: int = 8) -> list[Product]:
    scored: list[tuple[int, Product]] = []
    for p in _active_sellable_products(db):
        s = _score_product(text, p)
        if s > 0:
            scored.append((s, p))
    scored.sort(key=lambda x: (-x[0], x[1].name_ar or ""))
    return [p for _, p in scored[:limit]]


def _root_categories_with_counts(db: Session) -> list[tuple[ProductCategory, int]]:
    products = _active_sellable_products(db)
    counts: dict[int, int] = {}
    for p in products:
        if p.category_id is None:
            continue
        counts[p.category_id] = counts.get(p.category_id, 0) + 1
    roots = db.scalars(
        select(ProductCategory)
        .where(ProductCategory.parent_id.is_(None))
        .order_by(ProductCategory.sort_order, ProductCategory.name_ar)
    ).all()
    out: list[tuple[ProductCategory, int]] = []
    for root in roots:
        ids = _category_ids_with_children(db, root.id)
        total = sum(counts.get(cid, 0) for cid in ids)
        if total > 0:
            out.append((root, total))
    return out


def _format_product_list(products: list[Product], *, title: str, max_lines: int = 15) -> str:
    if not products:
        return f"📋 {title}\n\nلا توجد أصناف نشطة في هذا القسم حالياً."
    lines = [f"📋 {title}", ""]
    for p in products[:max_lines]:
        lines.append(f"• {p.name_ar} — {_fmt_price(p.sell_price)}")
    if len(products) > max_lines:
        lines.append(f"\n… و{len(products) - max_lines} صنف آخر")
    lines.append("\n💡 اكتب اسم أي صنف للسعر والصورة، أو «منيو» لكل الأقسام.")
    return "\n".join(lines)


def _reply_for_product(db: Session, product: Product) -> HermesReply:
    reply = HermesReply()
    price = _fmt_price(product.sell_price)
    cat_line = ""
    if product.category:
        cat_line = f"\n📂 الفئة: {product.category.name_ar}"
    body = f"🍽 **{product.name_ar}**\n💰 السعر: {price}{cat_line}"
    img = _product_image_url(product)
    if img:
        body += "\n📷 صورة الصنف أدناه."
    reply.add(body.replace("**", ""), image_url=img)
    return reply


def _reply_for_section(db: Session, code: str) -> HermesReply:
    title = _SECTION_LABELS.get(code) or section_display_name(code)
    products = _products_for_section_code(db, code)
    reply = HermesReply()
    reply.add(_format_product_list(products, title=title))
    shown = 0
    for p in products:
        if shown >= 3:
            break
        img = _product_image_url(p)
        if img:
            reply.add(f"🍽 {p.name_ar} — {_fmt_price(p.sell_price)}", image_url=img)
            shown += 1
    return reply


def _reply_for_category(db: Session, category: ProductCategory) -> HermesReply:
    products = _products_in_category(db, category.id)
    reply = HermesReply()
    reply.add(_format_product_list(products, title=f"قسم {category.name_ar}"))
    shown = 0
    for p in products:
        if shown >= 3:
            break
        img = _product_image_url(p)
        if img:
            reply.add(f"🍽 {p.name_ar} — {_fmt_price(p.sell_price)}", image_url=img)
            shown += 1
    return reply


def _reply_menu_overview(db: Session) -> HermesReply:
    store = (get_setting(db, "store_name", "مطعم ومقهى روف") or "").strip()
    roots = _root_categories_with_counts(db)
    lines = [
        f"📋 منيو {store}",
        "",
        "يمكنك السؤال عن:",
        "• **مشويات** — برجر، ستيك، كباب…",
        "• **مشروبات** — قهوة، عصائر…",
        "• **سلطات** — مقبلات وسلطات",
        "• **بيتزا / مكرونة / أرز**",
        "",
    ]
    if roots:
        lines.append("أقسام المنيو في النظام:")
        for root, count in roots:
            lines.append(f"• {root.name_ar} ({count} صنف)")
    lines.append("\nمثال: «عندكم إيه مشاوي؟» أو «سعر توماهوك»")
    reply = HermesReply()
    reply.add("\n".join(lines).replace("**", ""))
    return reply


def _static_info(db: Session, text: str) -> HermesReply | None:
    t = _normalize(text)
    store = (get_setting(db, "store_name", "مطعم ومقهى روف") or "").strip()
    hours = (get_setting(db, "web_chat_hermes_hours", "9 صباحاً – 9 مساءً") or "").strip()
    address = (get_setting(db, "web_chat_hermes_address", store) or "").strip()

    if any(w in t for w in ("وقت", "ساعة", "ساعات", "دوام", "فاتح")):
        r = HermesReply()
        r.add(f"🕒 أوقات العمل: {hours}")
        return r
    if any(w in t for w in ("عنوان", "موقع", "فين", "اين", "أين", "مكان")):
        r = HermesReply()
        r.add(f"📍 {address}")
        return r
    if any(w in t for w in ("توصيل", "delivery")):
        r = HermesReply()
        r.add("🚚 للتوصيل: أرسل عنوانك ورقم هاتفك والأصناف المطلوبة.")
        return r
    if any(w in t for w in ("طلب", "اطلب", "order")):
        rest = t
        for w in ("اطلب", "طلب", "order"):
            rest = rest.replace(w, " ").strip()
        if _tokens(rest):
            return None
        r = HermesReply()
        r.add(
            "🛒 للطلب: اكتب «اطلب برجر» أو اسم الصنف، ثم «تأكيد».\n"
            "مثال: «برجر ×2» → «تأكيد»."
        )
        return r
    if any(w in t for w in ("عرض", "عروض", "خصم")):
        r = HermesReply()
        r.add("🔥 للعروض الحالية اسأل عن قسم معيّن أو تابع صفحاتنا.")
        return r
    return None


def build_hermes_reply(
    text: str, db: Session, session: WebChatSession | None = None
) -> HermesReply:
    if wants_human_agent(text):
        return human_handoff_reply(db)

    cleaned = _strip_fillers(text)
    t = _normalize(cleaned)
    if not t:
        r = HermesReply()
        r.add("اكتب سؤالك أو «منيو» لعرض الأقسام.")
        return r

    if any(g in t for g in _GREETING_WORDS) and len(_tokens(t)) <= 3:
        store = (get_setting(db, "store_name", "مطعم ومقهى روف") or "").strip()
        r = HermesReply()
        r.add(
            f"👋 أهلاً بك في {store}!\n"
            "اسأل عن: مشويات، مشروبات، بيتزا، أو اكتب «منيو».\n"
            "يمكنك أيضاً كتابة اسم أي صنف (مثل: توماهوك، بيتزا)."
        )
        return r

    from modules.messaging.hermes_conversation import try_conversation_reply

    conv = try_conversation_reply(db, cleaned)
    if conv is not None:
        return conv

    static = _static_info(db, cleaned)
    if static is not None:
        return static

    exact_products = find_products_by_text(db, cleaned, limit=1)
    if exact_products and _score_product(cleaned, exact_products[0]) >= 100:
        return _reply_for_product(db, exact_products[0])

    from modules.messaging.hermes_intent import wants_menu, wants_price

    if wants_menu(cleaned) and not match_menu_section_code(cleaned):
        cat = _match_category_by_name(db, cleaned)
        if cat is not None:
            return _reply_for_category(db, cat)
        return _reply_menu_overview(db)

    section_code = match_menu_section_code(cleaned)
    if section_code:
        return _reply_for_section(db, section_code)

    cat = _match_category_by_name(db, cleaned)
    if cat is not None:
        return _reply_for_category(db, cat)

    products = find_products_by_text(db, cleaned, limit=5)
    if len(products) == 1:
        return _reply_for_product(db, products[0])
    if len(products) > 1:
        r = HermesReply()
        lines = ["🔍 وجدت أكثر من صنف:", ""]
        for p in products:
            lines.append(f"• {p.name_ar} — {_fmt_price(p.sell_price)}")
        lines.append("\nاكتب الاسم كاملاً للتفاصيل والصورة.")
        r.add("\n".join(lines))
        img = _product_image_url(products[0])
        if img:
            r.add(f"🍽 {products[0].name_ar}", image_url=img)
        return r

    if wants_price(cleaned) or any(w in t for w in _PRICE_WORDS):
        stripped = t
        for w in _PRICE_WORDS:
            stripped = stripped.replace(w, " ")
        stripped = re.sub(r"\s+", " ", stripped).strip()
        if len(stripped) >= 2:
            sub = find_products_by_text(db, stripped, limit=1)
            if sub:
                return _reply_for_product(db, sub[0])

    if session is not None:
        from modules.messaging.hermes_ai import try_ai_reply

        ai = try_ai_reply(db, session, cleaned)
        if ai is not None:
            return ai

    store = (get_setting(db, "store_name", "مطعم ومقهى روف") or "").strip()
    from modules.messaging.web_chat_config import web_chat_menu_options

    menu_lines = web_chat_menu_options(db)
    menu_hint = (
        "\n".join(f"• {opt.label} — اكتب {opt.num}" for opt in menu_lines)
        if menu_lines
        else "• المبيعات والطلبات — اكتب 1"
    )
    r = HermesReply()
    r.add(
        "عذراً، لم أتأكد مما تقصده 😊\n\n"
        "يمكنني مساعدتك في:\n"
        "• المنيو — «منيو» أو «مشروبات» / «مشويات»\n"
        "• سعر صنف — «سعر برجر»\n"
        "• الطلب — «اطلب برجر» ثم «تأكيد»\n"
        f"{menu_hint}\n\n"
        f"📍 {store}"
    )
    return r
