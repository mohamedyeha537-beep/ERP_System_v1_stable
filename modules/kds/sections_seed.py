"""أقسام المطبخ الافتراضية — أربعة أقسام (المقهى = مشروبات)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.printing.models import KitchenSection

# (الاسم المعروض, الرمز, مفتاح الطابعة في config.json للوكيل)
DEFAULT_KITCHEN_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("مشروبات (مقهى)", "DRINKS", "drinks"),
    ("مشويات", "GRILL", "grill"),
    ("سلطات", "SALAD", "salad"),
    ("أرز ومكرونة", "PASTA", "pasta"),
)


def ensure_default_kitchen_sections(db: Session) -> list[KitchenSection]:
    """يضمن وجود الأقسام الأربعة — يحدّث الاسم إن وُجد الرمز."""
    out: list[KitchenSection] = []
    for name, code, _agent_key in DEFAULT_KITCHEN_SECTIONS:
        sec = db.execute(
            select(KitchenSection).where(KitchenSection.code == code)
        ).scalar_one_or_none()
        if sec is None:
            sec = KitchenSection(name=name, code=code, is_active=True)
            db.add(sec)
        else:
            sec.name = name
            sec.is_active = True
        out.append(sec)
    db.flush()
    return out


def sync_catalog_kitchen_routing(db: Session) -> tuple[int, int]:
    """ربط الفئات والمنتجات بأقسام المطبخ حسب القواعد."""
    from modules.kds.section_rules import (
        sync_all_categories_kitchen_sections,
        sync_products_without_section,
    )

    cat_n = sync_all_categories_kitchen_sections(db)
    prod_n = sync_products_without_section(db)
    return cat_n, prod_n


def section_agent_keys() -> dict[str, str]:
    """رمز القسم → local_printer_key المقترح في config.json."""
    return {code: key for _, code, key in DEFAULT_KITCHEN_SECTIONS}
