"""تصدير التقارير إلى CSV (يُفتَح مباشرةً في Excel/LibreOffice/Google Sheets).

كل دالة تأخذ بياناتٍ جاهزة وتعيد نص CSV بـ UTF-8-BOM ليعرض العربية بشكل صحيح
في Excel على نظام Windows افتراضياً.

ملاحظة: Excel يفتح CSV لكنه قد يطبع أحرف عربية بشكل خاطئ ما لم يكن الملف
يبدأ بعلامة BOM (\\ufeff). نضيفها يدوياً.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Sequence


_CSV_BOM = "\ufeff"


def _fmt(value) -> str:
    """تحويل أي قيمة إلى نص مناسب لـ CSV."""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return f"{value:.3f}"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def make_csv(
    headers: Sequence[str], rows: Iterable[Sequence]
) -> str:
    """يبني نص CSV مع BOM.
    headers: ['اسم', 'كمية', ...]
    rows: قائمة من tuples/lists.
    """
    buf = io.StringIO()
    buf.write(_CSV_BOM)
    writer = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    for r in rows:
        writer.writerow([_fmt(c) for c in r])
    return buf.getvalue()


def csv_response(
    filename: str,
    headers: Sequence[str],
    rows: Iterable[Sequence],
):
    """يُعيد Response جاهزاً لـ FastAPI كمرفق تنزيل."""
    from fastapi.responses import Response

    body = make_csv(headers, rows).encode("utf-8")
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}.csv"',
            "Content-Length": str(len(body)),
        },
    )
