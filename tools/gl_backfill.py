#!/usr/bin/env python3
"""ترحيل GL للحركات السابقة — python tools/gl_backfill.py"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import modules.authz.models  # noqa: F401
import modules.gl.models  # noqa: F401
import modules.payments.models  # noqa: F401
import modules.refunds.models  # noqa: F401
import modules.sales.models  # noqa: F401
import modules.settings.models  # noqa: F401

from infra.db import get_session_factory
from infra.schema_bootstrap import bootstrap_schema
from infra.db import get_engine
from modules.gl.backfill import run_gl_backfill
from modules.gl.seed import ensure_default_chart_of_accounts
from modules.settings.service import get_bool


def main() -> None:
    bootstrap_schema(get_engine())
    db = get_session_factory()()
    try:
        ensure_default_chart_of_accounts(db)
        if not get_bool(db, "gl_enabled", default=False):
            print("GL معطّل — فعّله من /admin/gl أولاً (gl_enabled=1).")
            return
        counts = run_gl_backfill(db)
        db.commit()
        print("تم:", counts)
    finally:
        db.close()


if __name__ == "__main__":
    main()
