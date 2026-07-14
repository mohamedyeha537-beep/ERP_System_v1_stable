"""قواعد ربط الفئات والمنتجات بأقسام المطبخ — تلقائي للمنتجات الجديدة."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.kds.sections_seed import DEFAULT_KITCHEN_SECTIONS
from modules.printing.models import KitchenSection

if TYPE_CHECKING:
    from modules.catalog.models import Product, ProductCategory

# (رمز القسم, كلمات مفتاحية في اسم الفئة — عربي)
_SECTION_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "DRINKS",
        (
            "مشروب",
            "مشروبات",
            "قهو",
            "قهوه",
            "قهوة",
            "شاي",
            "شاى",
            "عصير",
            "عصائر",
            "مياه",
            "ماء",
            "مقهى",
            "مقهي",
            "مقه",
            "كافيه",
            "كافي",
            "بار",
            "سموذي",
            "كوكتيل",
            "كوكتيلات",
            "نسكافيه",
            "نسكاف",
            "ماتيه",
            "لاتيه",
            "كابتشينو",
            "اسبريسو",
            "ايس",
            "آيس",
            "موكا",
            "فريش",
            "عصيرات",
            "مشروبات باردة",
            "مشروبات ساخنة",
            "عصائر طبيعية",
        ),
    ),
    (
        "GRILL",
        (
            "مشويات",
            "مشوي",
            "شواء",
            "شواية",
            "لحم",
            "لحوم",
            "برجر",
            "برغر",
            "ستيك",
            "كباب",
            "كفتة",
            "كفتة",
            "شيش",
            "تكة",
            "تكا",
            "دجاج مشوي",
            "فيليه",
            "ريش",
            "ضلوع",
            "مشاوي",
            "جريل",
            "grill",
        ),
    ),
    (
        "SALAD",
        (
            "سلط",
            "سلطة",
            "سلطات",
            "مقبلات",
            "مقبلات باردة",
            "حمص",
            "تبولة",
            "فتوش",
            "ذرة",
            "سلطة خضر",
            "باردة",
            "خضار طازج",
            "سلطه",
        ),
    ),
    (
        "PASTA",
        (
            "أرز",
            "ارز",
            "رز",
            "مكرونة",
            "مكرونه",
            "معكرونة",
            "معكرونه",
            "باستا",
            "بيتزا",
            "بيتز",
            "معجنات",
            "لازانيا",
            "سباغيتي",
            "سباجيتي",
            "نودلز",
            "برياني",
            "كشري",
            "مندي",
            "كبسة",
            "رز بخاري",
            "مكرون",
            "pasta",
            "rice",
        ),
    ),
)


def _normalize_name(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t


def suggest_section_code(name_ar: str) -> str | None:
    """يُخمّن قسم المطبخ من اسم الفئة أو الصنف."""
    name = _normalize_name(name_ar)
    if not name:
        return None
    for code, keywords in _SECTION_KEYWORDS:
        for kw in keywords:
            if kw in name:
                return code
    return None


def section_id_by_code(db: Session, code: str) -> int | None:
    sid = db.execute(
        select(KitchenSection.id).where(
            KitchenSection.code == code,
            KitchenSection.is_active.is_(True),
        )
    ).scalar_one_or_none()
    return int(sid) if sid is not None else None


def resolve_kitchen_section_for_category(
    db: Session, category: ProductCategory
) -> int | None:
    """قسم الفئة: من الاسم، أو يرث من الفئة الأب."""
    from modules.catalog.models import ProductCategory

    code = suggest_section_code(category.name_ar)
    if code:
        return section_id_by_code(db, code)
    if category.parent_id:
        parent = db.get(ProductCategory, category.parent_id)
        if parent is not None:
            if parent.kitchen_section_id is not None:
                return int(parent.kitchen_section_id)
            return resolve_kitchen_section_for_category(db, parent)
    return None


def assign_category_kitchen_section(
    db: Session, category: ProductCategory, *, force: bool = False
) -> bool:
    """يحدّث kitchen_section_id للفئة حسب القواعد."""
    if not force and category.kitchen_section_id is not None:
        # احترام اختيار يدوي سابق — إلا عند المزامنة الشاملة force=True
        return False
    sec_id = resolve_kitchen_section_for_category(db, category)
    if sec_id is None:
        return False
    if category.kitchen_section_id == sec_id:
        return False
    category.kitchen_section_id = sec_id
    return True


def assign_product_kitchen_section_from_category(
    db: Session, product: Product, *, force: bool = False
) -> bool:
    """منتج جديد بدون قسم محدد: يرث من فئته (للعرض والطباعة)."""
    if not force and product.kitchen_section_id is not None:
        return False
    if product.category_id is None:
        return False
    from modules.catalog.models import ProductCategory

    cat = db.get(ProductCategory, product.category_id)
    if cat is None:
        return False
    sec_id = resolve_kitchen_section_for_category(db, cat)
    if sec_id is None:
        return False
    if product.kitchen_section_id == sec_id:
        return False
    product.kitchen_section_id = sec_id
    return True


def sync_all_categories_kitchen_sections(db: Session) -> int:
    """مزامنة كل الفئات — يُشغَّل عند بدء السيرفر."""
    from modules.catalog.models import ProductCategory

    changed = 0
    cats = list(db.scalars(select(ProductCategory).order_by(ProductCategory.id)).all())
    # الجذور أولاً ثم الفروع ليسهل الوراثة
    cats.sort(key=lambda c: (c.parent_id is not None, c.parent_id or 0, c.id))
    for cat in cats:
        if assign_category_kitchen_section(db, cat, force=True):
            changed += 1
    db.flush()
    return changed


def sync_products_without_section(db: Session) -> int:
    """منتجات بلا قسم محدد: نسخ من الفئة (للمنتجات الحالية والجديدة)."""
    from modules.catalog.models import Product

    changed = 0
    rows = list(
        db.scalars(
            select(Product).where(
                Product.is_active.is_(True),
                Product.kitchen_section_id.is_(None),
                Product.category_id.isnot(None),
            )
        ).all()
    )
    for p in rows:
        if assign_product_kitchen_section_from_category(db, p, force=True):
            changed += 1
    db.flush()
    return changed


def match_menu_section_code(text: str) -> str | None:
    """يُطابق نص الزبون بقسم منيو (GRILL, DRINKS, …) — لـ Hermes /chat."""
    t = _normalize_name(text)
    if not t:
        return None
    best_code: str | None = None
    best_len = 0
    for code, keywords in _SECTION_KEYWORDS:
        for kw in keywords:
            if kw in t and len(kw) > best_len:
                best_code = code
                best_len = len(kw)
    return best_code


def section_display_name(code: str) -> str:
    names = {c: n for n, c, _ in DEFAULT_KITCHEN_SECTIONS}
    return names.get(code, code)


def keyword_hints_for_admin() -> list[tuple[str, str, str]]:
    """للعرض في لوحة الإدارة."""
    names = {c: n for n, c, _ in DEFAULT_KITCHEN_SECTIONS}
    out: list[tuple[str, str, str]] = []
    for code, keywords in _SECTION_KEYWORDS:
        out.append((names.get(code, code), code, "، ".join(keywords[:8]) + "…"))
    return out
