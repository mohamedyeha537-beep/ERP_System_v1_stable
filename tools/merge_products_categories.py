"""دمج فئات POS مع ملف products_pos_import — category_path + kitchen_section."""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

csv.field_size_limit(sys.maxsize)

CATEGORY_RULES: list[tuple[str, str]] = [
    (r"شوربة", "المطعم/الشورية"),
    (r"عرض\s*بيتزا|بيتزا|وجبة\s*بيتزا", "المطعم/البيتزا"),
    (r"عرض\s*زنجر|زنجر|بوم\s*برجر|دبل\s*تشيز|برغر|برجر|classy\s*mega", "المطعم/البرجر"),
    (r"سلطة|صلطات\s*مشكلة|فواكه\s*مشكلة", "المطعم/السلطات"),
    (r"باستا|سباغيتي|سباقيتي|لينغويني|لازانيا|فريدو|ارابياتا|بيستو|بولونيز", "المطعم/الباستا"),
    (r"فخارة|فخارات", "المطعم/الفخارات"),
    (r"سمك|قاروص|قاجوج|جمبري|سيفود|طبق\s*جمبري", "المطعم/الاسماك"),
    (r"اجنحة\s*دجاج|شيش\s*طاووق|دجاج\s*كريمي", "المطعم/الدجاج"),
    (r"ستيك|ريب\s*أ?ي|تي\s*بون|توماهوك|كفته|كفتة|ريش\s*خاروف|طبق\s*روف|وجبة\s*ريب|لحم\s*عجل", "المطعم/اللحوم"),
    (r"حمص|محمرة|لفائف|أصابع|اصابع|بابا\s*غنوج|متبل|سمبوسة|سبرينق\s*رول|بطاطا\s*مقلية", "المطعم/المقبلات"),
    (r"^آيس|آيس\s|موخيتو|سموزي|ميلك\s*شيك|فروب|عصير\s", "المقهى/مشروبات باردة"),
    (r"موكا$|لاتيه|كابتش|اسبريسو|قهوة|شاهي|نسكاف|هوت\s*شوكلت|أمريكان|امريكان|كافي\s*لاتي|ميكياتo|سبانش", "المقهى/مشروبات ساخنة"),
    (r"كريب|بان\s*كيك|وافل|كيك|جاتو|افوكادو|نوتيلا|اوفالت|أوفالتين|سان\s*سباستيان|ميني\s*بن", "المقهى/حلويات"),
    (r"بيبسي|سفن\s*أ?ب|مياه|ميرند|صودا|دايت|غاز|مشروب\s*طاقة|ترمس|كوكتيل|فورب", "المقهى/مشروبات غازية و مياه"),
]

SUPPLY_SKIP = re.compile(
    r"اكواب|اكياس|علب\s|طبق\s*خ|قفاز|منديل|ورق\s|صابون|كلور|كمام|معطر|كرتون|شمعة|فاتورة|قرط|"
    r"مخالف|معدات|مواد\s|مستلزم|ملابس|دواء|كشف|طبعة|غطاء|شكاير|"
    r"فحم|طبق\s*متعد|طراحة|طواس|عبوة|سلاكة|مصب|معالق|ليفة|ماجي|مسحوق|"
    r"101$|^103$|اسياخ|اطباق\s|بكرج|بيض$|جبنة\s*جود|خدمة|قزير|كاسات|لبنة\s*كرات|"
    r"وجبة\s*افطار\s*نزيل|القيم|الريد\s*فلفت|ورق\s*A4|ورق\s*زبدة|ورق\s*ساند|"
    r"اسفنج|بخور|العاب|توبر|حوض|خلاط|ميكرويف|مجمع|سفرة\s*قزير|نسكويك",
    re.I,
)


def load_categories_export(path: Path) -> dict[str, str]:
    paths: dict[str, str] = {}
    if not path.is_file():
        return paths
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            parent = (row.get("parent_name") or "").strip()
            name = (row.get("name_ar") or "").strip()
            sec = (row.get("kitchen_section_code") or "").strip()
            if not name:
                continue
            full = f"{parent}/{name}" if parent else name
            paths[full] = sec
    return paths


def norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())


def guess_category(name: str) -> str | None:
    n = norm_name(name)
    if not n or SUPPLY_SKIP.search(n):
        return None
    for pattern, path in CATEGORY_RULES:
        if re.search(pattern, n, re.IGNORECASE):
            return path
    return None


def merge_products(
    products_path: Path,
    categories_path: Path,
    output_path: Path,
    summary_path: Path,
) -> None:
    cat_map = load_categories_export(categories_path)
    rows = list(csv.DictReader(products_path.open(encoding="utf-8-sig")))
    fieldnames = rows[0].keys() if rows else []

    mapped = 0
    skipped_stock = 0
    unmapped_sellable: list[str] = []

    for row in rows:
        kind = (row.get("kind") or "").strip()
        name = norm_name(row.get("name_ar") or "")
        if kind != "FINAL_SELLABLE":
            skipped_stock += 1
            continue
        path = guess_category(name)
        if path:
            row["category_path"] = path
            sec = cat_map.get(path, "")
            if sec and not (row.get("kitchen_section_code") or "").strip():
                row["kitchen_section_code"] = sec
            mapped += 1
        else:
            unmapped_sellable.append(name)

    with output_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    sellable = sum(1 for r in rows if r.get("kind") == "FINAL_SELLABLE")
    lines = [
        f"products={len(rows)}",
        f"sellable={sellable}",
        f"stock_only={skipped_stock}",
        f"mapped_sellable={mapped}",
        f"unmapped_sellable={len(unmapped_sellable)}",
        f"output={output_path}",
        "",
        "الفئات من: categories_export.csv (موجودة مسبقاً في POS — لا حاجة لإعادة استيرادها).",
        "Odoo لم يصدّر POS Category — رُبطت الوجبات تلقائياً من الاسم.",
        "راجع products_categories_merge_summary.txt للأصناف غير المربوطة.",
        "",
    ]
    if unmapped_sellable:
        lines.append("أصnaف للبيع بدون فئة (غالباً مستلزمات — اتركها فارغة):")
        for n in sorted(unmapped_sellable):
            lines.append(f"  - {n}")
    summary_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    base = Path(r"c:\Users\PIXEL\OneDrive\Desktop\سيف\New folder\New folder")
    products = base / "products_pos_import.csv"
    categories = base / "categories_export.csv"
    out = base / "products_pos_import_with_categories.csv"
    summary = base / "products_categories_merge_summary.txt"
    if len(sys.argv) >= 2:
        base = Path(sys.argv[1])
        products = base / "products_pos_import.csv"
        categories = base / "categories_export.csv"
        out = base / "products_pos_import_with_categories.csv"
        summary = base / "products_categories_merge_summary.txt"
    merge_products(products, categories, out, summary)


if __name__ == "__main__":
    main()
