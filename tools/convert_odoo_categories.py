"""Convert Odoo category export (xlsx) to POS categories CSV."""
from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

import openpyxl

ROOT_COLORS = {
    "مطعم": "#3b82f6",
    "مقهى": "#bd0003",
}
DEFAULT_COLOR = "#64748b"


def kitchen_section(parent: str, name: str) -> str:
    text = f"{parent} {name}".strip()
    if "سلط" in text:
        return "SALAD"
    if any(k in text for k in ("باست", "مكرون", "أرز", "ارز")):
        return "PASTA"
    if parent == "مقهى" or name == "مقهى" or any(k in text for k in ("مشرو", "حلو", "قهو", "كيك")):
        return "DRINKS"
    if parent == "مطعم" or name == "مطعم" or any(
        k in text for k in ("لحم", "برجر", "دجاج", "بيتز", "شور", "مقبل", "فخار", "سمك", "مشو")
    ):
        return "GRILL"
    return ""


def convert(src: Path, out: Path) -> list[tuple[str, str, int, str, str]]:
    wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
    rows = list(wb.active.iter_rows(min_row=2, values_only=True))

    roots: dict[str, int] = {}
    children: list[tuple[str, str, int]] = []

    for display_name, sequence in rows:
        if not display_name:
            continue
        name = str(display_name).strip()
        try:
            sort_order = int(sequence) if sequence is not None else 0
        except (TypeError, ValueError):
            sort_order = 0

        if " / " in name:
            parent, child = [p.strip() for p in name.split(" / ", 1)]
            if parent:
                roots.setdefault(parent, sort_order)
            children.append((parent, child, sort_order))
        else:
            # صف الجذر في Odoo له الأولوية على ترتيب مستمد من الأبناء
            roots[name] = sort_order

    for parent, _child, sort_order in children:
        if parent not in roots:
            roots[parent] = sort_order

    child_names = {child for _parent, child, _sort in children}
    skip_roots = {
        name
        for name in roots
        if name in child_names and name not in ("مطعم", "مقهى")
    }
    for name in skip_roots:
        del roots[name]

    out_rows: list[tuple[str, str, int, str, str]] = []
    for root_name in sorted(roots, key=lambda n: (roots[n], n)):
        out_rows.append(
            (
                "",
                root_name,
                roots[root_name],
                ROOT_COLORS.get(root_name, DEFAULT_COLOR),
                kitchen_section("", root_name),
            )
        )

    for parent, child, sort_order in sorted(children, key=lambda x: (x[0], x[2], x[1])):
        out_rows.append(
            (
                parent,
                child,
                sort_order,
                ROOT_COLORS.get(parent, DEFAULT_COLOR),
                kitchen_section(parent, child),
            )
        )

    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(
        ["parent_name", "name_ar", "sort_order", "color_hex", "kitchen_section_code"]
    )
    writer.writerows(out_rows)
    out.write_text(buf.getvalue(), encoding="utf-8-sig")
    return out_rows


def main() -> None:
    src = Path(
        sys.argv[1]
        if len(sys.argv) > 1
        else r"c:\Users\PIXEL\Downloads\New folder (4)\category.xlsx"
    )
    out = Path(
        sys.argv[2]
        if len(sys.argv) > 2
        else src.with_name("categories_import.csv")
    )
    rows = convert(src, out)
    print(f"Wrote {len(rows)} rows to {out}")
    for row in rows:
        print(row)


if __name__ == "__main__":
    main()
