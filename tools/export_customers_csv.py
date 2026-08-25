#!/usr/bin/env python3
"""تصدير العملاء إلى CSV — يعمل مباشرة بدون إعادة تشغيل السيرفر."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    out_name = (
        f"customers-export-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.csv"
    )
    out = ROOT / out_name
    if len(sys.argv) > 1:
        out = Path(sys.argv[1]).expanduser().resolve()

    from infra.db import get_session_factory
    from modules.customers.export import write_customers_csv_file

    Session = get_session_factory()
    db = Session()
    try:
        path = write_customers_csv_file(db, out, only_active=False)
    finally:
        db.close()

    print(f"تم الحفظ: {path}")
    print(f"عدد البايت: {path.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
