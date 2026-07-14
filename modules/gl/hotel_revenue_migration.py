"""ترحيل قيود GL القديمة: تحصيلات الفندق من 1200 (ذمم) إلى 4150 (إيراد)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from modules.gl.models import GlAccount, GlJournalEntry, GlJournalLine
from modules.gl.posting import CODE_AR, CODE_HOTEL_REVENUE
from modules.gl.seed import ensure_hotel_revenue_account


def migrate_hotel_gl_to_revenue_account(db: Session) -> dict[str, int]:
    """يُحدّث سطور قيود hotel_* التي كانت على AR لتصبح على 4150."""
    ensure_hotel_revenue_account(db)
    ar_id = db.scalar(select(GlAccount.id).where(GlAccount.code == CODE_AR))
    rev_id = db.scalar(select(GlAccount.id).where(GlAccount.code == CODE_HOTEL_REVENUE))
    if ar_id is None or rev_id is None:
        return {"entries": 0, "updated_lines": 0}

    entries = list(
        db.scalars(
            select(GlJournalEntry)
            .where(
                GlJournalEntry.source_type.in_(
                    ("hotel_booking_payment", "hotel_booking_payment_refund")
                )
            )
            .options(selectinload(GlJournalEntry.lines))
        ).all()
    )
    updated = 0
    for entry in entries:
        for line in entry.lines:
            if int(line.account_id) == int(ar_id):
                line.account_id = int(rev_id)
                updated += 1
    if updated:
        db.flush()
    return {"entries": len(entries), "updated_lines": updated}
