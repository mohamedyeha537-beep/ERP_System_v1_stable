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
cur.execute("SELECT id, area_code, area_name FROM personnel_area ORDER BY id")
print("AREAS:", cur.fetchall())
cur.execute("""
SELECT e.emp_code, e.first_name, a.area_code, a.area_name
FROM personnel_employee e
LEFT JOIN personnel_assignareaemployee ae ON ae.employee_id = e.id
LEFT JOIN personnel_area a ON a.id = ae.area_id
WHERE e.emp_code IN ('200','300','400')
""")
print("EMP_AREAS:", cur.fetchall())
cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='personnel_assignareaemployee'")
print("assign cols:", [r[0] for r in cur.fetchall()])
conn.close()
