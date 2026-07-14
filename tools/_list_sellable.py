import csv
from pathlib import Path

p = Path(r"c:\Users\PIXEL\OneDrive\Desktop\سيف\New folder\New folder\products_pos_import.csv")
out = Path(r"c:\Users\PIXEL\OneDrive\Desktop\pos\tools\_sellable_products.txt")
rows = list(csv.DictReader(p.open(encoding="utf-8-sig")))
sell = [r for r in rows if r.get("kind") == "FINAL_SELLABLE"]
with out.open("w", encoding="utf-8") as w:
    w.write(f"count={len(sell)}\n\n")
    for r in sorted(sell, key=lambda x: x["name_ar"]):
        w.write(f"{r['name_ar']}\n")
print(len(sell))
