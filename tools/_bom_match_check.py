"""تحليل مطابقة أسماء BOM مع products_pos_import.csv"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

import openpyxl

csv.field_size_limit(sys.maxsize)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def load_names(path: Path) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            name = norm(row.get("name_ar") or "")
            sku = norm(row.get("sku") or "")
            if name:
                out[name.casefold()] = (sku, name)
    return out


def parse_hierarchical(path: Path) -> list[tuple[str, str]]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    pairs: list[tuple[str, str]] = []
    parent = ""
    for raw in rows[1:]:
        product = norm(str(raw[2])) if len(raw) > 2 and raw[2] is not None else ""
        line = norm(str(raw[5])) if len(raw) > 5 and raw[5] is not None else ""
        if product:
            parent = product
            if line:
                pairs.append((parent, line))
        elif line and parent:
            pairs.append((parent, line))
    return pairs


def main() -> None:
    base = Path(r"c:\Users\PIXEL\OneDrive\Desktop\سيف\New folder")
    bom = base / "Bill of Material (mrp.bom) (1).xlsx"
    products = base / "products_pos_import.csv"
    by_name = load_names(products)
    pairs = parse_hierarchical(bom)
    miss_parent: set[str] = set()
    miss_comp: set[str] = set()
    for p, c in pairs:
        if p.casefold() not in by_name:
            miss_parent.add(p)
        if c.casefold() not in by_name:
            miss_comp.add(c)
    out = Path(__file__).with_name("_bom_match_report.txt")
    with out.open("w", encoding="utf-8") as w:
        w.write(f"pairs={len(pairs)}\n")
        w.write(f"miss_parent={len(miss_parent)}\n")
        for x in sorted(miss_parent): w.write(f"  P: {x}\n")
        w.write(f"miss_comp={len(miss_comp)}\n")
        for x in sorted(miss_comp): w.write(f"  C: {x}\n")
    print(len(pairs), len(miss_parent), len(miss_comp))


if __name__ == "__main__":
    main()
