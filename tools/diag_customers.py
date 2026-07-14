import sqlite3
from pathlib import Path

db = Path("pos.db")
if not db.exists():
    print("no pos.db")
    raise SystemExit(1)
c = sqlite3.connect(db)
cur = c.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = {r[0] for r in cur.fetchall()}
print("has customers", "customers" in tables)
print("has loyalty_transactions", "loyalty_transactions" in tables)
if "customers" in tables:
    cur.execute("PRAGMA table_info(customers)")
    cols = [r[1] for r in cur.fetchall()]
    print("customers cols", cols)
    cur.execute("SELECT COUNT(*) FROM customers")
    print("count", cur.fetchone()[0])

# simulate old schema query failure
try:
    cur.execute("SELECT points_balance FROM customers LIMIT 1")
    print("points_balance ok")
except Exception as e:
    print("points_balance fail", e)
