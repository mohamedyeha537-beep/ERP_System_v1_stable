#!/usr/bin/env python
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from infra.background import with_db
from sqlalchemy import select
from modules.hr.models import Employee

with with_db() as db:
    rows = db.scalars(select(Employee).order_by(Employee.id)).all()
    for e in rows:
        print(e.id, e.full_name_ar, e.zk_emp_code, e.status)
