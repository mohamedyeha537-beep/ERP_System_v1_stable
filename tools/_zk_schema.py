#!/usr/bin/env python
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modules.hr.zkbio_config import read_attsite_ini
import psycopg2
cfg = read_attsite_ini()
conn = psycopg2.connect(cfg.dsn())
cur = conn.cursor()
cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='personnel_employee' ORDER BY ordinal_position")
print([r[0] for r in cur.fetchall()])
cur.execute("SELECT * FROM personnel_employee LIMIT 1")
cols = [d[0] for d in cur.description]
row = cur.fetchone()
print(dict(zip(cols, row)))
