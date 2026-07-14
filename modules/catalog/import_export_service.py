"""تصدير واستيراد قوالب CSV للفئات والأصناف ووصفات التركيب."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from modules.catalog.models import (
    BillOfMaterialsLine,
    Product,
    ProductCategory,
    ProductKind,
)
from modules.catalog.service import CatalogError, assert_barcode_available, can_have_bom
from modules.printing.models import KitchenSection

CSV_ENCODING = "utf-8-sig"  # Excel + العربية


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return not self.errors


def _writer() -> tuple[io.StringIO, csv.writer]:
    buf = io.StringIO()
    buf.write("\ufeff")
    return buf, csv.writer(buf)


def categories_csv_template() -> str:
    buf, w = _writer()
    w.writerow(
        [
            "parent_name",
            "name_ar",
            "sort_order",
            "color_hex",
            "kitchen_section_code",
            "show_in_shop",
        ]
    )
    w.writerow(["", "مطعم", "10", "#3b82f6", "GRILL", "1"])
    w.writerow(["مطعم", "برجر", "0", "#3b82f6", "", "1"])
    w.writerow(["", "مقهى", "20", "#dc2626", "", "1"])
    w.writerow(["مقهى", "قهوة", "0", "#dc2626", "", "1"])
    return buf.getvalue()


def categories_csv_export(db: Session) -> str:
    buf, w = _writer()
    w.writerow(
        [
            "parent_name",
            "name_ar",
            "sort_order",
            "color_hex",
            "kitchen_section_code",
            "show_in_shop",
        ]
    )
    roots = list(
        db.scalars(
            select(ProductCategory)
            .where(ProductCategory.parent_id.is_(None))
            .options(selectinload(ProductCategory.children))
            .order_by(ProductCategory.sort_order, ProductCategory.id)
        ).all()
    )
    sec_codes = _section_code_map(db)
    for root in roots:
        w.writerow(
            [
                "",
                root.name_ar,
                root.sort_order,
                root.color_hex,
                sec_codes.get(root.kitchen_section_id, ""),
                "1" if root.show_in_shop else "0",
            ]
        )
        for ch in sorted(root.children, key=lambda c: (c.sort_order, c.id)):
            w.writerow(
                [
                    root.name_ar,
                    ch.name_ar,
                    ch.sort_order,
                    ch.color_hex,
                    sec_codes.get(ch.kitchen_section_id, ""),
                    "1" if ch.show_in_shop else "0",
                ]
            )
    return buf.getvalue()


def products_csv_template() -> str:
    buf, w = _writer()
    w.writerow(
        [
            "name_ar",
            "sku",
            "barcode",
            "kind",
            "unit",
            "sell_price",
            "reference_unit_cost",
            "reorder_level",
            "category_path",
            "kitchen_section_code",
            "is_active",
            "show_in_pos",
            "direct_purchase_enabled",
            "notes",
        ]
    )
    w.writerow(
        [
            "برجر لحم",
            "MEAL-BRG",
            "",
            "FINAL_SELLABLE",
            "قطعة",
            "25",
            "0",
            "مطعم/برجر",
            "GRILL",
            "1",
            "1",
            "0",
            "وجبة للبيع",
        ]
    )
    w.writerow(
        [
            "طماطم",
            "ING-TOM",
            "",
            "STOCK_ONLY",
            "جرام",
            "",
            "500",
            "",
            "",
            "1",
            "0",
            "0",
            "مكوّن مخزني",
        ]
    )
    return buf.getvalue()


def _category_path(db: Session, cat: ProductCategory | None) -> str:
    if cat is None:
        return ""
    if cat.parent_id is None:
        return cat.name_ar
    parent = cat.parent or db.get(ProductCategory, cat.parent_id)
    if parent:
        return f"{parent.name_ar}/{cat.name_ar}"
    return cat.name_ar


def products_csv_export(db: Session) -> str:
    buf, w = _writer()
    w.writerow(
        [
            "name_ar",
            "sku",
            "barcode",
            "kind",
            "unit",
            "sell_price",
            "reference_unit_cost",
            "reorder_level",
            "category_path",
            "kitchen_section_code",
            "is_active",
            "show_in_pos",
            "direct_purchase_enabled",
            "notes",
        ]
    )
    sec_codes = _section_code_map(db)
    products = list(
        db.scalars(
            select(Product)
            .options(selectinload(Product.category).selectinload(ProductCategory.parent))
            .order_by(Product.kind, Product.name_ar)
        ).all()
    )
    for p in products:
        w.writerow(
            [
                p.name_ar,
                p.sku or "",
                p.barcode or "",
                p.kind.value,
                p.unit,
                str(p.sell_price) if p.sell_price is not None else "",
                str(p.reference_unit_cost) if p.reference_unit_cost is not None else "",
                str(p.reorder_level),
                _category_path(db, p.category),
                sec_codes.get(p.kitchen_section_id, ""),
                "1" if p.is_active else "0",
                "1" if p.show_in_pos else "0",
                "1" if p.direct_purchase_enabled else "0",
                (p.notes or "").replace("\n", " "),
            ]
        )
    return buf.getvalue()


def bom_csv_template() -> str:
    buf, w = _writer()
    w.writerow(
        [
            "parent_sku",
            "parent_name",
            "component_sku",
            "component_name",
            "qty_per_parent",
        ]
    )
    w.writerow(["MEAL-BRG", "برجر لحم", "ING-TOM", "طماطم", "50"])
    w.writerow(["MEAL-BRG", "", "ING-BUN", "خبز", "1"])
    return buf.getvalue()


def bom_csv_export(db: Session) -> str:
    buf, w = _writer()
    w.writerow(
        [
            "parent_sku",
            "parent_name",
            "component_sku",
            "component_name",
            "qty_per_parent",
        ]
    )
    lines = list(
        db.scalars(
            select(BillOfMaterialsLine)
            .options(
                selectinload(BillOfMaterialsLine.parent_product),
                selectinload(BillOfMaterialsLine.component_product),
            )
            .order_by(BillOfMaterialsLine.parent_product_id)
        ).all()
    )
    for ln in lines:
        p = ln.parent_product
        c = ln.component_product
        w.writerow(
            [
                p.sku if p else "",
                p.name_ar if p else "",
                c.sku if c else "",
                c.name_ar if c else "",
                str(ln.qty_per_parent),
            ]
        )
    return buf.getvalue()


def _section_code_map(db: Session) -> dict[int | None, str]:
    rows = db.scalars(select(KitchenSection)).all()
    return {s.id: s.code for s in rows}


def _resolve_section(db: Session, code: str) -> int | None:
    c = (code or "").strip().upper()
    if not c:
        return None
    sec = db.execute(
        select(KitchenSection).where(KitchenSection.code == c)
    ).scalar_one_or_none()
    return sec.id if sec else None


def _find_category_by_path(db: Session, path: str) -> ProductCategory | None:
    path = (path or "").strip()
    if not path:
        return None
    parts = [p.strip() for p in path.replace("\\", "/").split("/") if p.strip()]
    if not parts:
        return None
    root = db.execute(
        select(ProductCategory).where(
            ProductCategory.parent_id.is_(None),
            ProductCategory.name_ar == parts[0],
        )
    ).scalar_one_or_none()
    if root is None:
        return None
    if len(parts) == 1:
        return root
    sub = db.execute(
        select(ProductCategory).where(
            ProductCategory.parent_id == root.id,
            ProductCategory.name_ar == parts[1],
        )
    ).scalar_one_or_none()
    return sub


def _get_or_create_category(
    db: Session,
    parent_name: str,
    name_ar: str,
    *,
    sort_order: int,
    color_hex: str,
    kitchen_section_id: int | None,
    show_in_shop: bool | None = None,
    update_existing: bool,
    result: ImportResult,
) -> ProductCategory | None:
    name = (name_ar or "").strip()
    if not name:
        result.errors.append("اسم الفئة فارغ.")
        return None
    parent_name = (parent_name or "").strip()
    if parent_name:
        parent = db.execute(
            select(ProductCategory).where(
                ProductCategory.parent_id.is_(None),
                ProductCategory.name_ar == parent_name,
            )
        ).scalar_one_or_none()
        if parent is None:
            result.errors.append(f"الفئة الرئيسية غير موجودة: {parent_name}")
            return None
        existing = db.execute(
            select(ProductCategory).where(
                ProductCategory.parent_id == parent.id,
                ProductCategory.name_ar == name,
            )
        ).scalar_one_or_none()
        if existing:
            if update_existing:
                existing.sort_order = sort_order
                existing.color_hex = color_hex or existing.color_hex
                if show_in_shop is not None:
                    existing.show_in_shop = show_in_shop
                if kitchen_section_id is not None:
                    existing.kitchen_section_id = kitchen_section_id
                result.updated += 1
            else:
                result.skipped += 1
            return existing
        cat = ProductCategory(
            name_ar=name,
            parent_id=parent.id,
            sort_order=sort_order,
            color_hex=color_hex or "#3b82f6",
            kitchen_section_id=kitchen_section_id,
            show_in_shop=True if show_in_shop is None else show_in_shop,
        )
        db.add(cat)
        result.created += 1
        return cat

    existing = db.execute(
        select(ProductCategory).where(
            ProductCategory.parent_id.is_(None),
            ProductCategory.name_ar == name,
        )
    ).scalar_one_or_none()
    if existing:
        if update_existing:
            existing.sort_order = sort_order
            existing.color_hex = color_hex or existing.color_hex
            if show_in_shop is not None:
                existing.show_in_shop = show_in_shop
            if kitchen_section_id is not None:
                existing.kitchen_section_id = kitchen_section_id
            result.updated += 1
        else:
            result.skipped += 1
        return existing
    cat = ProductCategory(
        name_ar=name,
        parent_id=None,
        sort_order=sort_order,
        color_hex=color_hex or "#3b82f6",
        kitchen_section_id=kitchen_section_id,
        show_in_shop=True if show_in_shop is None else show_in_shop,
    )
    db.add(cat)
    result.created += 1
    return cat


def import_categories_csv(
    db: Session, text: str, *, update_existing: bool = True
) -> ImportResult:
    result = ImportResult()
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        result.errors.append("الملف فارغ أو بدون عناوين أعمدة.")
        return result
    for i, row in enumerate(reader, start=2):
        try:
            sort_val = int((row.get("sort_order") or "0").strip() or "0")
        except ValueError:
            sort_val = 0
        sec_id = _resolve_section(db, row.get("kitchen_section_code") or "")
        show_shop_raw = (row.get("show_in_shop") or "").strip().lower()
        show_in_shop = None
        if show_shop_raw:
            show_in_shop = show_shop_raw not in ("0", "false", "no")
        _get_or_create_category(
            db,
            row.get("parent_name") or "",
            row.get("name_ar") or "",
            sort_order=sort_val,
            color_hex=(row.get("color_hex") or "#3b82f6").strip(),
            kitchen_section_id=sec_id,
            show_in_shop=show_in_shop,
            update_existing=update_existing,
            result=result,
        )
    db.flush()
    return result


def _find_product(db: Session, sku: str, name: str) -> Product | None:
    sku = (sku or "").strip()
    if sku:
        return db.execute(select(Product).where(Product.sku == sku)).scalar_one_or_none()
    name = (name or "").strip()
    if name:
        return db.execute(select(Product).where(Product.name_ar == name)).scalar_one_or_none()
    return None


def import_products_csv(
    db: Session, text: str, *, update_existing: bool = True
) -> ImportResult:
    result = ImportResult()
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        result.errors.append("الملف فارغ أو بدون عناوين أعمدة.")
        return result
    for i, row in enumerate(reader, start=2):
        name = (row.get("name_ar") or "").strip()
        if not name:
            result.errors.append(f"سطر {i}: اسم الصنف فارغ.")
            continue
        sku = (row.get("sku") or "").strip() or None
        barcode = (row.get("barcode") or "").strip() or None
        try:
            kind = ProductKind((row.get("kind") or "FINAL_SELLABLE").strip().upper())
        except ValueError:
            result.errors.append(f"سطر {i}: kind غير صالح (FINAL_SELLABLE أو STOCK_ONLY).")
            continue
        unit = (row.get("unit") or "قطعة").strip() or "قطعة"
        sell_raw = (row.get("sell_price") or "").strip()
        sell_price = None
        if sell_raw:
            try:
                sell_price = Decimal(sell_raw)
            except InvalidOperation:
                result.errors.append(f"سطر {i}: sell_price غير صالح.")
                continue
        if kind == ProductKind.FINAL_SELLABLE and sell_price is None:
            result.errors.append(f"سطر {i}: سعر البيع مطلوب للمنتج النهائي.")
            continue
        ref_raw = (row.get("reference_unit_cost") or "").strip()
        reference_unit_cost = None
        if ref_raw:
            try:
                reference_unit_cost = Decimal(ref_raw)
                if reference_unit_cost <= 0:
                    reference_unit_cost = None
            except InvalidOperation:
                result.errors.append(f"سطر {i}: reference_unit_cost غير صالح.")
                continue
        try:
            reorder = Decimal((row.get("reorder_level") or "0").strip() or "0")
        except InvalidOperation:
            reorder = Decimal("0")
        cat = _find_category_by_path(db, row.get("category_path") or "")
        sec_id = _resolve_section(db, row.get("kitchen_section_code") or "")
        is_active = (row.get("is_active") or "1").strip() not in ("0", "false", "no")
        show_raw = (row.get("show_in_pos") or "").strip()
        if show_raw:
            show_in_pos = show_raw not in ("0", "false", "no")
        else:
            show_in_pos = kind == ProductKind.FINAL_SELLABLE and is_active
        direct_raw = (row.get("direct_purchase_enabled") or "").strip()
        if direct_raw:
            direct_purchase_enabled = direct_raw not in ("0", "false", "no")
        else:
            direct_purchase_enabled = kind == ProductKind.FINAL_SELLABLE
        notes = (row.get("notes") or "").strip() or None

        existing = _find_product(db, sku or "", name)
        if existing:
            if not update_existing:
                result.skipped += 1
                continue
            try:
                if barcode:
                    assert_barcode_available(db, barcode, existing.id)
            except CatalogError as e:
                result.errors.append(f"سطر {i}: {e}")
                continue
            existing.name_ar = name
            existing.sku = sku
            existing.barcode = barcode
            existing.kind = kind
            existing.unit = unit
            existing.sell_price = sell_price
            if "reference_unit_cost" in (row.keys() or []):
                existing.reference_unit_cost = reference_unit_cost
            existing.reorder_level = reorder
            existing.category_id = cat.id if cat else None
            existing.kitchen_section_id = sec_id
            existing.is_active = is_active
            existing.show_in_pos = show_in_pos if kind == ProductKind.FINAL_SELLABLE else False
            existing.direct_purchase_enabled = (
                direct_purchase_enabled if kind == ProductKind.FINAL_SELLABLE else False
            )
            existing.notes = notes
            result.updated += 1
            continue

        try:
            if barcode:
                assert_barcode_available(db, barcode, None)
        except CatalogError as e:
            result.errors.append(f"سطر {i}: {e}")
            continue
        p = Product(
            name_ar=name,
            sku=sku,
            barcode=barcode,
            kind=kind,
            unit=unit,
            sell_price=sell_price,
            reference_unit_cost=reference_unit_cost,
            reorder_level=reorder,
            category_id=cat.id if cat else None,
            kitchen_section_id=sec_id,
            is_active=is_active,
            show_in_pos=show_in_pos if kind == ProductKind.FINAL_SELLABLE else False,
            direct_purchase_enabled=(
                direct_purchase_enabled if kind == ProductKind.FINAL_SELLABLE else False
            ),
            notes=notes,
        )
        db.add(p)
        result.created += 1
    db.flush()
    return result


def import_bom_csv(
    db: Session, text: str, *, update_existing: bool = True
) -> ImportResult:
    result = ImportResult()
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        result.errors.append("الملف فارغ أو بدون عناوين أعمدة.")
        return result
    for i, row in enumerate(reader, start=2):
        parent = _find_product(
            db, row.get("parent_sku") or "", row.get("parent_name") or ""
        )
        if parent is None:
            result.errors.append(f"سطر {i}: الوجبة/المنتج الأب غير موجود.")
            continue
        if not can_have_bom(parent):
            result.errors.append(f"سطر {i}: الأب يجب أن يكون منتجاً نهائياً أو مخزنياً وسيطاً.")
            continue
        comp = _find_product(
            db, row.get("component_sku") or "", row.get("component_name") or ""
        )
        if comp is None:
            result.errors.append(f"سطر {i}: المكوّن غير موجود.")
            continue
        if comp.kind != ProductKind.STOCK_ONLY:
            result.errors.append(f"سطر {i}: المكوّن يجب أن يكون STOCK_ONLY.")
            continue
        try:
            qty = Decimal((row.get("qty_per_parent") or "1").strip() or "1")
        except InvalidOperation:
            result.errors.append(f"سطر {i}: qty_per_parent غير صالح.")
            continue
        if qty <= 0:
            result.errors.append(f"سطر {i}: الكمية يجب أن تكون أكبر من صفر.")
            continue
        existing = db.execute(
            select(BillOfMaterialsLine).where(
                BillOfMaterialsLine.parent_product_id == parent.id,
                BillOfMaterialsLine.component_product_id == comp.id,
            )
        ).scalar_one_or_none()
        if existing:
            if update_existing:
                existing.qty_per_parent = qty
                result.updated += 1
            else:
                result.skipped += 1
            continue
        db.add(
            BillOfMaterialsLine(
                parent_product_id=parent.id,
                component_product_id=comp.id,
                qty_per_parent=qty,
            )
        )
        result.created += 1
    db.flush()
    return result
