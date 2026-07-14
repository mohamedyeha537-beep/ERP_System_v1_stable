from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.messaging.models import CustomerMessagingProfile, MessageChannel


def get_profile(db: Session, customer_id: int) -> CustomerMessagingProfile | None:
    return db.scalar(
        select(CustomerMessagingProfile).where(
            CustomerMessagingProfile.customer_id == customer_id
        )
    )


def ensure_profile(db: Session, customer_id: int) -> CustomerMessagingProfile:
    prof = get_profile(db, customer_id)
    if prof is not None:
        return prof
    prof = CustomerMessagingProfile(customer_id=customer_id, opt_in=False)
    db.add(prof)
    db.flush()
    return prof


def update_consent(
    db: Session,
    *,
    customer_id: int,
    opt_in: bool,
    preferred_channel: str = MessageChannel.WHATSAPP.value,
    consent_source: str = "checkout",
    telegram_chat_id: str | None = None,
) -> CustomerMessagingProfile:
    prof = ensure_profile(db, customer_id)
    now = datetime.now(timezone.utc)
    prof.opt_in = opt_in
    prof.preferred_channel = (preferred_channel or MessageChannel.WHATSAPP.value)[
        :32
    ]
    prof.consent_source = consent_source[:32] if consent_source else None
    if telegram_chat_id:
        prof.telegram_chat_id = telegram_chat_id.strip()[:64]
    if opt_in:
        prof.opt_in_at = now
        prof.opt_out_at = None
    else:
        prof.opt_out_at = now
    prof.updated_at = now
    db.flush()
    return prof


def customer_can_receive(db: Session, customer_id: int) -> bool:
    prof = get_profile(db, customer_id)
    return prof is not None and prof.opt_in
