"""تحويل تصدير منتجات Odoo إلى CSV استيراد POS (/catalog/import-export)."""
from __future__ import annotations

import csv
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

csv.field_size_limit(sys.maxsize)

STOCK_UOMS = frozenset({"g", "G", "L", "l", "cm", "kg", "KG", "ml", "ML", "mL"})
SUPPLY_KEYWORDS = (
    "ادوات",
    "أدوات",
    "مستهلكة",
    "مطبخ",
    "ورق ألمنيوم",
    "لوح خشب",
    "زيت ",
    "زبدة ",
    "قشطة ",
    "مربى ",
    "تيشرت",
    "غسل",
)
INGREDIENT_ZERO_PRICE = (
    "زبدة",
    "قشطة",
    "مربى",
    "زيت",
    "حليب",
    "بصل",
    "طحين",
    "سكر",
    "ملح",
)

UOM_TO_POS = {
    "Units": "قطعة",
    "Unit": "قطعة",
    "units": "قطعة",
    "g": "جرام",
    "G": "جرام",
    "kg": "كيلو جرام",
    "KG": "كيلو جرام",
    "L": "لتر",
    "l": "لتر",
    "ml": "ملي لتر",
    "ML": "ملي لتر",
    "mL": "ملي لتر",
    "cm": "قطعة",
}

POS_HEADERS = [
    "name_ar",
    "sku",
    "barcode",
    "kind",
    "unit",
    "sell_price",
    "reorder_level",
    "category_path",
    "kitchen_section_code",
    "is_active",
    "notes",
]


def _dec(raw: str) -> Decimal:
    try:
        return Decimal((raw or "0").strip() or "0")
    except InvalidOperation:
        return Decimal("0")


def classify_kind(name: str, uom: str, sales_price: Decimal) -> str:
    name = name.strip()
    uom = (uom or "").strip()
    if uom in STOCK_UOMS:
        return "STOCK_ONLY"
    if any(k in name for k in SUPPLY_KEYWORDS):
        return "STOCK_ONLY"
    if sales_price <= 0 and any(k in name for k in INGREDIENT_ZERO_PRICE):
        return "STOCK_ONLY"
    if sales_price <= 0:
        # لا سعر بيع — الأسلم: مخزني حتى لا يفشل الاستيراد
        return "STOCK_ONLY"
    return "FINAL_SELLABLE"


def make_sku(name: str, kind: str, used: set[str]) -> str:
    slug = re.sub(r"\s+", " ", name.strip())
    slug = re.sub(r"[^\w\u0600-\u06FF\-]+", "-", slug, flags=re.UNICODE)
    slug = re.sub(r"-+", "-", slug).strip("-")[:36]
    if not slug or slug.isdigit():
        slug = "ITEM"
    prefix = "FS" if kind == "FINAL_SELLABLE" else "ST"
    base = f"{prefix}-{slug}"
    candidate = base[:48]
    n = 2
    while candidate in used:
        candidate = f"{base[:40]}-{n}"
        n += 1
    used.add(candidate)
    return candidate


def convert_row(row: dict, used_skus: set[str]) -> dict:
    name = (row.get("Name") or "").strip()
    uom = (row.get("Unit of Measure") or "").strip()
    sales_price = _dec(row.get("Sales Price") or "0")
    cost = (row.get("Cost") or "").strip()
    qty_on_hand = (row.get("Quantity On Hand") or "").strip()

    kind = classify_kind(name, uom, sales_price)
    unit = UOM_TO_POS.get(uom, uom or "قطعة")
    sku = make_sku(name, kind, used_skus)

    notes_parts: list[str] = ["Odoo import"]
    if cost:
        notes_parts.append(f"cost={cost}")
    if qty_on_hand:
        notes_parts.append(f"qty_on_hand={qty_on_hand}")

    sell_price = ""
    if kind == "FINAL_SELLABLE":
        sell_price = str(sales_price.quantize(Decimal("0.001")))
    elif sales_price > 0:
        notes_parts.append(f"odoo_sales_price={sales_price}")

    reorder = "0"
    if kind == "STOCK_ONLY" and qty_on_hand:
        try:
            reorder = str(max(Decimal("0"), _dec(qty_on_hand)))
        except InvalidOperation:
            pass

    return {
        "name_ar": name,
        "sku": sku,
        "barcode": "",
        "kind": kind,
        "unit": unit,
        "sell_price": sell_price,
        "reorder_level": reorder,
        "category_path": "",
        "kitchen_section_code": "",
        "is_active": "1",
        "notes": "; ".join(notes_parts),
        "_sort": 0 if kind == "STOCK_ONLY" else 1,
    }


def convert_file(src: Path, dst: Path) -> tuple[int, int, int]:
    used_skus: set[str] = set()
    out_rows: list[dict] = []
    with src.open(encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("Name") or "").strip()
            if not name:
                continue
            out_rows.append(convert_row(row, used_skus))

    out_rows.sort(key=lambda r: (r["_sort"], r["name_ar"]))
    stock = sum(1 for r in out_rows if r["kind"] == "STOCK_ONLY")
    final = sum(1 for r in out_rows if r["kind"] == "FINAL_SELLABLE")

    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=POS_HEADERS, extrasaction="ignore")
        w.writeheader()
        for row in out_rows:
            w.writerow(row)

    return len(out_rows), stock, final


def main() -> None:
    src = Path(
        r"c:\Users\PIXEL\OneDrive\Desktop\سيف\New folder\Product all.csv"
    )
    dst = src.with_name("products_pos_import.csv")
    if len(sys.argv) >= 2:
        src = Path(sys.argv[1])
    if len(sys.argv) >= 3:
        dst = Path(sys.argv[2])
    total, stock, final = convert_file(src, dst)
    summary = dst.with_name("products_pos_import_summary.txt")
    summary.write_text(
        f"source={src}\noutput={dst}\ntotal={total}\nSTOCK_ONLY={stock}\nFINAL_SELLABLE={final}\n",
        encoding="utf-8",
    )
    print(f"Wrote {dst}")
    print(f"total={total} stock={stock} final={final}")


if __name__ == "__main__":
    main()
