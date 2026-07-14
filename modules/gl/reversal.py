"""عكس قيود GL — للتراجع التشغيلي مع حفظ الأثر المحاسبي."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from modules.gl.models import GlJournalEntry, GlJournalEntryStatus, GlJournalLine
from modules.gl.posting import _LineSpec, post_balanced_entry

logger = logging.getLogger(__name__)


def reverse_by_idempotency_key(
    db: Session,
    original_key: str,
    *,
    description_suffix: str = " — عكس",
    created_by_id: int | None = None,
    ignore_cutover: bool = False,
) -> GlJournalEntry | None:
    key = (original_key or "").strip()
    if not key:
        return None

    reverse_key = f"reverse:{key}"
    existing_reverse_id = db.scalar(
        select(GlJournalEntry.id).where(GlJournalEntry.idempotency_key == reverse_key)
    )
    if existing_reverse_id is not None:
        return db.get(GlJournalEntry, int(existing_reverse_id))

    orig = db.scalar(
        select(GlJournalEntry)
        .where(
            GlJournalEntry.idempotency_key == key,
            GlJournalEntry.status == GlJournalEntryStatus.POSTED,
        )
        .options(
            selectinload(GlJournalEntry.lines).selectinload(GlJournalLine.account)
        )
    )
    if orig is None:
        return None

    lines: list[_LineSpec] = []
    for ln in orig.lines:
        acc = ln.account
        if acc is None:
            continue
        memo = (ln.memo or "").strip()
        lines.append(
            _LineSpec(
                acc.code,
                ln.credit,
                ln.debit,
                f"عكس: {memo}" if memo else "عكس",
            )
        )
    if not lines:
        return None

    rev = post_balanced_entry(
        db,
        idempotency_key=reverse_key,
        source_type=f"{orig.source_type}_reverse" if orig.source_type else "reversal",
        source_id=orig.source_id or orig.id,
        description_ar=(orig.description_ar + description_suffix)[:255],
        entry_date=datetime.now(timezone.utc).date(),
        lines=lines,
        created_by_id=created_by_id,
        ignore_cutover=ignore_cutover,
    )
    if rev is None:
        return None

    orig.status = GlJournalEntryStatus.REVERSED
    orig.idempotency_key = f"{key}:superseded:{orig.id}"
    rev.reversed_entry_id = orig.id
    db.flush()
    return rev


def reverse_payroll_run_gl(
    db: Session,
    run_id: int,
    *,
    reverse_payment: bool = True,
    reverse_accrual: bool = True,
) -> None:
    if reverse_payment:
        reverse_by_idempotency_key(db, f"payroll:payment:{run_id}")
    if reverse_accrual:
        reverse_by_idempotency_key(db, f"payroll:accrual:{run_id}")


def reverse_payroll_run_gl_safe(
    db: Session,
    run_id: int,
    *,
    reverse_payment: bool = True,
    reverse_accrual: bool = True,
) -> None:
    try:
        reverse_payroll_run_gl(
            db,
            run_id,
            reverse_payment=reverse_payment,
            reverse_accrual=reverse_accrual,
        )
    except Exception as exc:
        logger.warning(
            "GL payroll reversal skipped (run %s): %s", run_id, exc, exc_info=True
        )
