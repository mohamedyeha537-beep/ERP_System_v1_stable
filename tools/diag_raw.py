import sqlite3
from decimal import Decimal

c = sqlite3.connect("pos.db")
cur = c.cursor()

cur.execute("SELECT id, name_ar FROM products")
products = cur.fetchall()
onion = [p for p in products if "بصل" in (p[1] or "")]
out = open("tools/diag_out.txt", "w", encoding="utf-8")
out.write(f"onion products: {onion}\n")
pid = onion[0][0] if onion else products[0][0]
out.write(f"using pid={pid}\n\n")

cur.execute(
    "SELECT warehouse_id, quantity FROM stock_balances WHERE product_id=?",
    (pid,),
)
out.write(f"stock_balances: {cur.fetchall()}\n")

cur.execute(
    "SELECT warehouse_id, SUM(quantity), COUNT(*) FROM stock_movements WHERE product_id=? GROUP BY warehouse_id",
    (pid,),
)
mov = cur.fetchall()
out.write(f"movement sums: {mov}\n")

cur.execute("SELECT SUM(quantity) FROM stock_movements WHERE product_id=?", (pid,))
out.write(f"total movement sum: {cur.fetchone()[0]}\n")

cur.execute(
    "SELECT COUNT(*) FROM stock_movements WHERE product_id=? AND warehouse_id IS NULL",
    (pid,),
)
out.write(f"null warehouse movements: {cur.fetchone()[0]}\n")

cur.execute(
    "SELECT id, warehouse_id, movement_type, quantity FROM stock_movements WHERE product_id=? ORDER BY id DESC LIMIT 8",
    (pid,),
)
out.write(f"recent movements: {cur.fetchall()}\n\n")

cur.execute(
    "SELECT id, product_id, quantity, line_total, unit_cost FROM purchase_lines WHERE product_id=?",
    (pid,),
)
pls = cur.fetchall()
out.write(f"purchase_lines ({len(pls)}): {pls[:10]}\n")

cur.execute("SELECT COUNT(*), SUM(line_total), SUM(quantity) FROM purchase_lines WHERE product_id IS NOT NULL")
out.write(f"all purchase lines with product_id: {cur.fetchone()}\n")

cur.execute(
    "SELECT product_id, SUM(line_total), SUM(quantity) FROM purchase_lines WHERE product_id=? GROUP BY product_id",
    (pid,),
)
out.write(f"purchase agg: {cur.fetchall()}\n")

out.close()
print("written tools/diag_out.txt")
