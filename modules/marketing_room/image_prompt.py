"""بناء برومبت صور تسويقية بالإنجليزية — بدون نص داخل الصورة."""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from modules.marketing_room.ai_client import chat_text

# تلميحات عربية/إنجليزية → وصف بصري إنجليزي واضح
_FOOD_HINTS: list[tuple[tuple[str, ...], str]] = [
    (
        ("برجر", "burger", "همبرجر", "hamburger"),
        "a juicy grilled beef burger sandwich in a toasted sesame bun with melted cheese, "
        "fresh lettuce, tomato, and pickles, appetizing close-up food photography",
    ),
    (
        ("شاورما", "shawarma"),
        "a freshly wrapped shawarma sandwich with meat, garlic sauce, and vegetables, "
        "close-up appetizing food photography",
    ),
    (
        ("بيتزا", "pizza"),
        "a hot freshly baked pizza with melted cheese and toppings, restaurant food photography",
    ),
    (
        ("كباب", "كفته", "kebab"),
        "grilled kebab skewers with herbs and sides, appetizing restaurant photography",
    ),
    (
        ("دجاج", "chicken", "فراخ"),
        "a delicious grilled chicken dish plated for a restaurant menu photo",
    ),
    (
        ("قهوة", "coffee", "كابتشينو", "لاتيه"),
        "a beautiful cafe latte or coffee cup on a wooden table, cozy cafe photography",
    ),
    (
        ("عصير", "juice", "سموذي", "smoothie"),
        "a fresh colorful juice or smoothie in a glass, bright cafe photography",
    ),
    (
        ("إفطار", "فطور", "breakfast"),
        "a generous hotel breakfast platter with eggs, bread, and fresh sides",
    ),
    (
        ("ساندويتش", "sandwich", "سندويتش"),
        "a tasty restaurant sandwich cut in half showing fresh fillings, food photography",
    ),
    (
        ("حلو", "dessert", "كيك", "cake", "آيس كريم"),
        "an elegant dessert plated for cafe social media, soft light food photography",
    ),
]

_NO_TEXT = (
    "Absolutely NO text, NO letters, NO words, NO Arabic script, NO English writing, "
    "NO typography, NO captions, NO watermarks, NO logos, NO signs on the image. "
    "Photorealistic, natural lighting, high detail, social-media ready food photo."
)


def _hint_visual(topic: str) -> str | None:
    t = (topic or "").lower()
    for keys, visual in _FOOD_HINTS:
        if any(k.lower() in t for k in keys):
            return visual
    return None


def _strip_for_english_hint(topic: str) -> str:
    """أزل الرموز؛ إن وُجد لاتيني استخدمه كتلميح."""
    latin = re.findall(r"[A-Za-z][A-Za-z0-9 \-]{2,}", topic or "")
    if latin:
        return " ".join(latin)[:120]
    return "restaurant food dish"


def build_marketing_image_prompt(
    db: Session,
    *,
    topic: str,
    brand: str = "",
) -> str:
    """
    يبني برومبت إنجليزي للصورة فقط.
    النص العربي يبقى في كابشن المنشور — لا يُكتب على الصورة.
    """
    topic_s = (topic or "").strip()
    brand_s = (brand or "").strip()
    hinted = _hint_visual(topic_s)

    ai_visual = chat_text(
        db,
        system=(
            "You write ONE English image-generation prompt for a restaurant/hotel social photo. "
            "Describe ONLY the visual subject (food, plating, setting). "
            "Never include any text, letters, logos, or Arabic words in the prompt content. "
            "If the topic mentions a burger, the image MUST clearly show a burger sandwich. "
            "Max 60 words. Output the prompt only."
        ),
        user=(
            f"Topic (Arabic or mixed, for meaning only): {topic_s}\n"
            f"Brand name (do NOT render as text in image): {brand_s or 'local restaurant'}\n"
            f"Preferred visual if relevant: {hinted or 'match the topic food item precisely'}"
        ),
        max_tokens=180,
    )
    if ai_visual:
        # أزل أي اقتباسات وأسطر زائدة
        visual = ai_visual.strip().strip('"').strip("'")
        visual = re.sub(r"\s+", " ", visual)
        # إن رجع النموذج عربياً بالخطأ — استخدم التلميح
        if re.search(r"[\u0600-\u06FF]", visual):
            visual = hinted or (
                f"appetizing restaurant photo of {_strip_for_english_hint(topic_s)}"
            )
    else:
        visual = hinted or (
            f"appetizing professional restaurant food photography of "
            f"{_strip_for_english_hint(topic_s)}"
        )

    # تعزيز صريح للبرجر إن وُجد في الموضوع
    if any(k in topic_s.lower() for k in ("برجر", "burger", "همبرجر")):
        if "burger" not in visual.lower():
            visual = (
                "a juicy beef burger sandwich in a sesame bun with cheese, lettuce and tomato, "
                + visual
            )

    brand_bit = (
        f"Premium hospitality brand mood for {brand_s}, without showing any brand text. "
        if brand_s
        else ""
    )
    return f"{visual}. {brand_bit}{_NO_TEXT}"
