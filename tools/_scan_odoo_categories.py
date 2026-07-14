import csv
import sys
from pathlib import Path

csv.field_size_limit(sys.maxsize)
out = Path(__file__).with_name("_odoo_product_category_fields.txt")
lines = []
for path in [
    Path(r"c:\Users\PIXEL\OneDrive\Desktop\سيف\New folder\Product (product.template).csv"),
    Path(r"c:\Users\PIXEL\OneDrive\Desktop\سيف\New folder\Product all.csv"),
]:
    if not path.is_file():
        lines.append(f"missing: {path}")
        continue
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as f:
        r = csv.DictReader(f)
        lines.append(f"FILE: {path.name}")
        lines.append(f"fields: {r.fieldnames}")
        row = next(iter(r), None)
        if row:
            for k in r.fieldnames or []:
                kl = (k or "").lower()
                if any(x in kl for x in ["categ", "pos", "tag", "public"]):
                    lines.append(f"  {k} = {(row.get(k) or '')[:200]}")
        lines.append("")
out.write_text("\n".join(lines), encoding="utf-8")
print("written", out)
