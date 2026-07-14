#!/usr/bin/env python
"""مزامنة موظفي ZKBioTime إلى POS — يُشغَّل بـ Python الخاص بالمشروع."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infra.background import with_db
from modules.hr.zkbio_employee_sync import sync_employees_from_zkbio
from modules.settings.service import set_setting


def main() -> None:
    with with_db() as db:
        set_setting(db, "zk_sync_enabled", "1")
        result = sync_employees_from_zkbio(db)
        db.commit()
        print(result.message)
        for e in result.errors[:10]:
            print("!", e)
    if not result.ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
