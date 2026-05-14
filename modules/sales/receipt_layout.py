"""ترتيب بنود الفاتورة للطباعة: جذر (مطعم/مقهى) ثم فئة فرعية ثم الأسطر."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from modules.catalog.models import ProductCategory
from modules.sales.models import Sale, SaleLine


def resolve_root_category(db: Session, cat: ProductCategory | None) -> ProductCategory | None:
    if cat is None:
        return None
    while cat.parent_id is not None:
        nxt = db.get(ProductCategory, cat.parent_id)
        if nxt is None:
            break
        cat = nxt
    return cat


def line_receipt_sort_key(db: Session, line: SaleLine) -> tuple:
    """نفس منطق ترتيب الفاتورة: جذر (sort_order) ثم فئة مباشرة ثم اسم الصنف."""
    prod = line.product
    if prod is None:
        return (10**9, 0, 10**6, "", "", line.id)
    direct = prod.category
    root = resolve_root_category(db, direct)
    if root is None:
        root_order = 10**9
        root_id = 0
    else:
        root_order = root.sort_order
        root_id = root.id
    if direct is None:
        direct_order = 10**6
        direct_name = ""
    else:
        direct_order = direct.sort_order
        direct_name = direct.name_ar or ""
    pname = prod.name_ar or ""
    return (root_order, root_id, direct_order, direct_name, pname, line.id)


def sort_sale_lines_for_display(db: Session, lines: list[SaleLine]) -> list[SaleLine]:
    """ترتيب بنود السلة/العرض: منتجات المطعم معاً ثم المقهى (حسب ترتيب الفئات الجذرية والفرعية)."""
    return sorted(lines, key=lambda ln: line_receipt_sort_key(db, ln))


@dataclass
class ReceiptSubgroup:
    """مجموعة داخل قسم الجذر (مثلاً: مشروبات، برجر)."""

    title: str | None
    lines: list[SaleLine]
    subtotal: Decimal


@dataclass
class ReceiptSection:
    """قسم يطبع ككتلة (مثلاً: المطعم ثم المقهى)."""

    root_title: str
    subgroups: list[ReceiptSubgroup]
    section_total: Decimal


def build_receipt_sections(db: Session, sale: Sale) -> list[ReceiptSection]:
    """
    يجمّع الأسطر حسب الفئة الجذرية (ترتيب sort_order للجذر: المطعم أولاً ثم المقهى إذا رتّبت هكذا)،
    وداخل كل جذر حسب الفئة المباشرة للمنتج (الفرعية أو الجذر نفسه).
    """
    # root_id -> direct_cat_id -> lines
    buckets: dict[int | None, dict[int | None, list[SaleLine]]] = defaultdict(lambda: defaultdict(list))

    for line in sale.lines:
        prod = line.product
        direct = prod.category
        root = resolve_root_category(db, direct)
        root_id = root.id if root else None
        direct_id = direct.id if direct else None
        buckets[root_id][direct_id].append(line)

    # بناء قائمة الجذور المرتبة
    root_ids = list(buckets.keys())
    root_meta: list[tuple[int | None, ProductCategory | None, int]] = []
    for rid in root_ids:
        if rid is None:
            root_meta.append((None, None, 10**9))
        else:
            r = db.get(ProductCategory, rid)
            if r:
                root_meta.append((rid, r, r.sort_order))
            else:
                root_meta.append((rid, None, 10**6))
    root_meta.sort(key=lambda x: (x[2], x[0] or 0))

    sections: list[ReceiptSection] = []
    for rid, root_cat, _ in root_meta:
        root_title = root_cat.name_ar if root_cat else "غير مصنّف"

        sub_map = buckets[rid]
        direct_ids = list(sub_map.keys())

        def subgroup_sort_key(did: int | None) -> tuple:
            if did is None:
                return (10**6, "")
            c = db.get(ProductCategory, did)
            if c is None:
                return (10**6, "")
            return (c.sort_order, c.name_ar or "")

        direct_ids.sort(key=subgroup_sort_key)

        subgroups: list[ReceiptSubgroup] = []
        section_total = Decimal("0")
        for did in direct_ids:
            lines = list(sub_map[did])
            lines.sort(key=lambda ln: ln.product.name_ar)
            st = sum((ln.line_total for ln in lines), Decimal("0")).quantize(Decimal("0.001"))
            section_total += st

            direct = db.get(ProductCategory, did) if did is not None else None
            if direct is None:
                title = "بدون فئة"
            elif root_cat and direct.id == root_cat.id:
                title = None
            else:
                title = direct.name_ar

            subgroups.append(ReceiptSubgroup(title=title, lines=lines, subtotal=st))

        sections.append(
            ReceiptSection(
                root_title=root_title,
                subgroups=subgroups,
                section_total=section_total.quantize(Decimal("0.001")),
            )
        )

    return sections
