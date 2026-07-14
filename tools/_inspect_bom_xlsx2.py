import openpyxl

path = r"c:\Users\PIXEL\OneDrive\Desktop\سيف\New folder\Bill of Material (mrp.bom) (1).xlsx"
out = r"c:\Users\PIXEL\OneDrive\Desktop\pos\tools\_odoo_bom2_analysis2.txt"
wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
ws = wb.active
rows = list(ws.iter_rows(values_only=True))
with open(out, "w", encoding="utf-8") as w:
    w.write(f"total_rows={len(rows)}\n\n")
    current_parent = None
    for i, row in enumerate(rows[1:], start=2):
        product = row[2] if len(row) > 2 else None
        line = row[5] if len(row) > 5 else None
        if product:
            current_parent = str(product).strip()
            w.write(f"\n=== PARENT row {i}: {current_parent} | first_line={line}\n")
        elif line:
            w.write(f"  L{i}: {line}\n")
        if i > 120:
            w.write("\n... truncated ...\n")
            break
print("ok")
