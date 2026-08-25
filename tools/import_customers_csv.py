#!/usr/bin/env python3
"""استيراد العملاء من CSV — يعمل مباشرة بدون إعادة تشغيل السيرفر."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    if len(sys.argv) < 2:
        print("الاستخدام: python tools/import_customers_csv.py <ملف.csv>")
        return 1
    src = Path(sys.argv[1]).expanduser().resolve()
    if not src.is_file():
        print(f"الملف غير موجود: {src}")
        return 1

    from infra.db import get_session_factory
    from modules.customers.import_service import import_customers_csv

    text = src.read_text(encoding="utf-8-sig")
    Session = get_session_factory()
    db = Session()
    try:
        result = import_customers_csv(db, text)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print(f"تم: {result.summary()}")
    if result.warnings:
        print("تحذيرات:")
        for w in result.warnings[:20]:
            print(f"  - {w}")
    if result.errors:
        print("أخطاء:")
        for e in result.errors[:20]:
            print(f"  - {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
