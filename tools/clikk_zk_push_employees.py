#!/usr/bin/env python
"""دفع موظفي Clikk النشطين إلى ZKBioTime."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infra.background import with_db
from modules.hr.zkbio_employee_sync import sync_employees_to_zkbio


def main() -> None:
    with with_db() as db:
        result = sync_employees_to_zkbio(db)
        if result.ok:
            db.commit()
        else:
            db.rollback()
            print("ERROR:", result.message)
            sys.exit(1)
    print(result.message)
    if result.errors:
        for err in result.errors:
            print("!", err)


if __name__ == "__main__":
    main()
