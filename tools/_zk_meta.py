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
cur.execute("SELECT id, dept_code, dept_name FROM personnel_department")
print("DEPTS:", cur.fetchall())
cur.execute("SELECT id, area_code, area_name FROM personnel_area")
print("AREAS:", cur.fetchall())
cur.execute("SELECT id, sn, alias, ip_address, state FROM iclock_terminal")
print("TERMS:", cur.fetchall())
