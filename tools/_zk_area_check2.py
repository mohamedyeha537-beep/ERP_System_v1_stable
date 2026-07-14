#!/usr/bin/env python
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modules.hr.zkbio_config import read_attsite_ini
import psycopg2
cfg = read_attsite_ini()
conn = psycopg2.connect(cfg.dsn())
cur = conn.cursor()
for tbl in ("personnel_employee_area", "personnel_assignareaemployee", "iclock_terminalemployee"):
    try:
        cur.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name='{tbl}'")
        cols = [r[0] for r in cur.fetchall()]
        if cols:
            cur.execute(f"SELECT * FROM {tbl} LIMIT 5")
            print(tbl, cols, cur.fetchall())
    except Exception as e:
        conn.rollback()
        print(tbl, "err", e)
cur.execute("SELECT id, emp_code FROM personnel_employee WHERE emp_code IN ('200','300','400')")
emps = cur.fetchall()
print("emps", emps)
conn.close()
