import sqlite3

c = sqlite3.connect("pos.db")
cur = c.cursor()
cur.execute(
    "SELECT pl.id, pl.item_name, pl.quantity, pl.unit_cost, pl.line_total, p.kind "
    "FROM purchase_lines pl "
    "JOIN purchases p ON p.id = pl.purchase_id "
    "WHERE pl.product_id IS NULL AND p.kind='INVENTORY' LIMIT 20"
)
rows = cur.fetchall()
with open("tools/diag_out.txt", "a", encoding="utf-8") as out:
    out.write("\nnull product inventory lines:\n")
    for r in rows:
        out.write(str(r) + "\n")
    cur.execute(
        "SELECT pl.product_id, p.name_ar, SUM(pl.line_total), SUM(pl.quantity) "
        "FROM purchase_lines pl JOIN products p ON p.id=pl.product_id "
        "GROUP BY pl.product_id LIMIT 20"
    )
    out.write("\nproduct cost agg:\n")
    for r in cur.fetchall():
        out.write(str(r) + "\n")
