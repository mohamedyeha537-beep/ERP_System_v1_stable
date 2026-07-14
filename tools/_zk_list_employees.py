#!/usr/bin/env python
"""List ZKBioTime personnel for sync preview."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.hr.zkbio_config import read_attsite_ini


def main() -> None:
    cfg = read_attsite_ini()
    if cfg is None:
        print("NO_CONFIG")
        return
    import psycopg2

    conn = psycopg2.connect(cfg.dsn())
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name LIKE '%employee%'
            ORDER BY 1
            """
        )
        print("TABLES:", [r[0] for r in cur.fetchall()])
        for tbl in ("personnel_employee", "auth_user"):
            try:
                cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                print(f"COUNT {tbl}:", cur.fetchone()[0])
            except Exception as exc:
                print(f"SKIP {tbl}:", exc)
                conn.rollback()
        cur.execute(
            """
            SELECT emp_code, first_name, last_name, is_active
            FROM personnel_employee
            ORDER BY emp_code
            """
        )
        rows = cur.fetchall()
        print("EMPLOYEES:")
        for r in rows:
            print("|".join(str(x) for x in r))
        cur.execute(
            """
            SELECT DISTINCT emp_code FROM iclock_transaction ORDER BY emp_code
            """
        )
        print("PUNCH_CODES:", [r[0] for r in cur.fetchall()])
        cur.execute("SELECT id, emp_code, punch_time, punch_state FROM iclock_transaction ORDER BY id DESC LIMIT 10")
        print("RECENT_PUNCHES:")
        for r in cur.fetchall():
            print("|".join(str(x) for x in r))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
