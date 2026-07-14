"""توافق SQL بين SQLite و MySQL/MariaDB و PostgreSQL."""

from __future__ import annotations

from sqlalchemy import ColumnElement, case


def order_by_nulls_last(column: ColumnElement, *, descending: bool = True):
    """ترتيب عمود مع NULL في النهاية — بدون NULLS LAST (غير مدعوم في MySQL/MariaDB)."""
    null_rank = case((column.is_(None), 1), else_=0)
    ordered = column.desc() if descending else column.asc()
    return null_rank, ordered
