from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from modules.messaging.consent import get_profile
from modules.messaging.events import MESSAGING_TEST, MESSAGING_TEST_BUTTONS
from modules.messaging.models import (
    MessageChannel,
    MessageOutbox,
    MessageOutboxStatus,
)
from modules.messaging.phone_utils import normalize_whatsapp_recipient
from modules.messaging.providers.telegram import send_telegram_message
from modules.messaging.providers.textmebot import buttons_from_meta, send_textmebot_with_fallback
from modules.messaging.providers.webhook import send_webhook
from modules.messaging.send_gap import MIN_SEND_GAP_SEC, wait_send_gap
from modules.settings.service import get_bool, get_int, get_setting

LOG = logging.getLogger("messaging.outbox")

TEXTMEBOT_TIMEOUT = 20.0

# أولوية الطابور — الاختبار والدعائية قبل التلقائية
_OUTBOX_PRIORITY = case(
    (
        MessageOutbox.event_type.in_((MESSAGING_TEST, MESSAGING_TEST_BUTTONS)),
        0,
    ),
    (
        MessageOutbox.event_type.in_(
            ("campaign.manual", "campaign.broadcast", "campaign.external")
        ),
        1,
    ),
    else_=2,
)


@dataclass
class _DispatchJob:
    channel: str
    body: str
    phone: str | None
    event_type: str | None
    customer_id: int | None
    meta: dict
    provider: str
    webhook_url: str
    webhook_method: str
    webhook_param: str
    use_n8n: bool
    bot_token: str
    textmebot_base_url: str
    textmebot_apikey: str
    country_code: str
    admin_phone: str
    telegram_chat_id: str | None
    send_gap_seconds: int = MIN_SEND_GAP_SEC





def _parse_channels(raw: str) -> list[str]:

    if not raw:

        return [MessageChannel.WHATSAPP.value]

    if raw.strip().startswith("["):

        try:

            data = json.loads(raw)

            if isinstance(data, list):

                return [str(c).strip() for c in data if str(c).strip()]

        except json.JSONDecodeError:

            pass

    return [c.strip() for c in raw.split(",") if c.strip()]





def _match_conditions(conditions_json: str, payload: dict) -> bool:

    try:

        cond = json.loads(conditions_json or "{}")

    except json.JSONDecodeError:

        cond = {}

    if not cond:

        return True

    min_points = cond.get("min_points")

    if min_points is not None:

        pts = float(payload.get("points") or 0)

        if pts < float(min_points):

            return False

    return True





def whatsapp_provider(db: Session) -> str:

    return (get_setting(db, "messaging_whatsapp_provider", "webhook") or "webhook").strip().lower()





def send_delay_seconds(db: Session) -> int:
    """الحد الأدنى بين رسالتين متتاليتين — لا يقل عن 10 ثوانٍ."""
    return max(MIN_SEND_GAP_SEC, get_int(db, "messaging_send_delay_seconds", MIN_SEND_GAP_SEC))





def outbox_batch_size(db: Session, *, limit: int | None = None) -> int:

    if limit is not None:

        return max(1, limit)

    base = max(1, get_int(db, "messaging_outbox_batch_size", 1))

    pending = pending_outbox_count(db)

    if pending > 15:

        return max(base, 5)

    if pending > 5:

        return max(base, 3)

    return base





def enqueue_message(

    db: Session,

    *,

    body: str,

    channel: str,

    phone: str | None = None,

    customer_id: int | None = None,

    event_type: str | None = None,

    rule_id: int | None = None,

    campaign_id: int | None = None,

    meta: dict | None = None,

) -> MessageOutbox:

    row = MessageOutbox(

        customer_id=customer_id,

        phone=(phone or "")[:40] or None,

        channel=channel[:32],

        event_type=event_type,

        body=body,

        status=MessageOutboxStatus.PENDING.value,

        rule_id=rule_id,

        campaign_id=campaign_id,

        meta_json=json.dumps(meta or {}, ensure_ascii=False),

    )

    db.add(row)

    db.flush()

    return row





def resolve_channel_for_customer(

    db: Session, customer_id: int, rule_channels: list[str]

) -> str | None:
    prof = get_profile(db, customer_id)
    if prof is None or not prof.opt_in:

        return None

    pref = prof.preferred_channel or MessageChannel.WHATSAPP.value

    if pref in rule_channels:

        return pref

    return rule_channels[0] if rule_channels else None





def _parse_meta(row: MessageOutbox) -> dict:

    if not row.meta_json:

        return {}

    try:

        return json.loads(row.meta_json)

    except json.JSONDecodeError:

        return {}





def _country_code(db: Session) -> str:

    return (get_setting(db, "messaging_country_code", "218") or "218").strip()





def _build_dispatch_job(db: Session, row: MessageOutbox) -> _DispatchJob:
    meta = _parse_meta(row)
    telegram_chat_id = meta.get("telegram_chat_id")
    if row.channel == MessageChannel.TELEGRAM.value and not telegram_chat_id and row.customer_id:
        prof = get_profile(db, row.customer_id)
        if prof and prof.telegram_chat_id:
            telegram_chat_id = prof.telegram_chat_id
    webhook_url = (get_setting(db, "messaging_webhook_url") or "").strip()
    if not webhook_url:
        webhook_url = (get_setting(db, "whatsapp_webhook_url") or "").strip()
    return _DispatchJob(
        channel=row.channel,
        body=row.body,
        phone=row.phone,
        event_type=row.event_type,
        customer_id=row.customer_id,
        meta=meta,
        provider=whatsapp_provider(db),
        webhook_url=webhook_url,
        webhook_method=(
            get_setting(db, "messaging_webhook_method")
            or get_setting(db, "whatsapp_webhook_method", "POST")
            or "POST"
        ),
        webhook_param=(
            get_setting(db, "messaging_webhook_param")
            or get_setting(db, "whatsapp_webhook_param", "text")
            or "text"
        ),
        use_n8n=get_bool(db, "messaging_use_n8n_json", True),
        bot_token=(get_setting(db, "messaging_telegram_bot_token") or "").strip(),
        textmebot_base_url=get_setting(
            db, "messaging_textmebot_base_url", "http://api.textmebot.com/send.php"
        ),
        textmebot_apikey=(get_setting(db, "messaging_textmebot_apikey") or "").strip(),
        country_code=_country_code(db),
        admin_phone=(get_setting(db, "messaging_admin_phone") or "").strip(),
        telegram_chat_id=str(telegram_chat_id) if telegram_chat_id else None,
        send_gap_seconds=send_delay_seconds(db),
    )


def _execute_dispatch_job(job: _DispatchJob) -> None:
    """إرسال شبكي فقط — بدون قفل SQLite."""
    if job.channel == MessageChannel.WHATSAPP.value:
        # فاصل إلزامي ≥10ث بين أي رسائل واتساب (حتى من مسارات/أحداث مختلفة)
        wait_send_gap(float(job.send_gap_seconds or MIN_SEND_GAP_SEC))
    if job.channel == MessageChannel.TELEGRAM.value:
        send_telegram_message(job.bot_token, str(job.telegram_chat_id or ""), job.body)
    elif job.channel == MessageChannel.WHATSAPP.value and job.provider == "textmebot":
        phone_raw = job.phone or job.admin_phone
        recipient = normalize_whatsapp_recipient(phone_raw, country_code=job.country_code)
        send_textmebot_with_fallback(
            base_url=job.textmebot_base_url,
            apikey=job.textmebot_apikey,
            recipient=recipient,
            text=job.body,
            file_url=job.meta.get("image_url"),
            document_url=job.meta.get("document_url"),
            document_filename=job.meta.get("document_filename"),
            buttons=buttons_from_meta(job.meta),
            copycode=job.meta.get("copycode"),
            copytext=job.meta.get("copytext"),
            timeout=TEXTMEBOT_TIMEOUT,
        )
    else:
        send_webhook(
            job.webhook_url,
            method=job.webhook_method or "POST",
            param=job.webhook_param or "text",
            text=job.body,
            phone=job.phone,
            channel=job.channel,
            event=job.event_type,
            customer_id=job.customer_id,
            meta=job.meta,
            use_json_payload=job.use_n8n,
        )


def _dispatch_one(db: Session, row: MessageOutbox) -> None:
    row_id = row.id
    job = _build_dispatch_job(db, row)
    db.commit()
    _execute_dispatch_job(job)
    fresh = db.get(MessageOutbox, row_id)
    if fresh is None:
        return
    fresh.status = MessageOutboxStatus.SENT.value
    fresh.sent_at = datetime.now(timezone.utc)
    fresh.error_message = None





def send_outbox_item_now(db: Session, row: MessageOutbox) -> MessageOutbox:
    """إرسال رسالة واحدة فوراً (اختبار) — يُسجّل sent/failed في السجل."""
    row_id = row.id
    row.attempts = (row.attempts or 0) + 1
    try:
        _dispatch_one(db, row)
        db.commit()
    except Exception as exc:
        db.rollback()
        fresh = db.get(MessageOutbox, row_id)
        if fresh is not None:
            fresh.status = MessageOutboxStatus.FAILED.value
            fresh.error_message = str(exc)[:500]
            db.commit()
            return fresh
        raise
    return db.get(MessageOutbox, row_id) or row


def send_outbox_ids_now(db: Session, ids: list[int]) -> int:
    """إرسال فوري لرسائل محددة — لا تنتظر دورة العامل."""
    sent = 0
    delay: int | None = None
    for i, oid in enumerate(ids):
        if i > 0:
            if delay is None:
                delay = send_delay_seconds(db)
            db.commit()
            time.sleep(delay)
        row = db.get(MessageOutbox, oid)
        if row is None or row.status != MessageOutboxStatus.PENDING.value:
            continue
        row = send_outbox_item_now(db, row)
        if row.status == MessageOutboxStatus.SENT.value:
            sent += 1
    return sent


def process_outbox_batch(db: Session, *, limit: int | None = None) -> int:

    if not get_bool(db, "messaging_enabled", False):

        return 0

    batch = outbox_batch_size(db, limit=limit)

    rows = list(

        db.scalars(

            select(MessageOutbox)

            .where(MessageOutbox.status == MessageOutboxStatus.PENDING.value)

            .order_by(_OUTBOX_PRIORITY.asc(), MessageOutbox.created_at.asc())

            .limit(batch)

        ).all()

    )

    delay = send_delay_seconds(db)
    sent = 0
    for i, row in enumerate(rows):
        if i > 0:
            db.commit()
            time.sleep(delay)
        row.attempts = (row.attempts or 0) + 1
        try:
            _dispatch_one(db, row)
            sent += 1
            db.commit()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("outbox %s failed: %s", row.id, exc)
            db.rollback()
            fresh = db.get(MessageOutbox, row.id)
            if fresh is not None:
                fresh.error_message = str(exc)[:500]
                if fresh.attempts >= 3:
                    fresh.status = MessageOutboxStatus.FAILED.value
                db.commit()
    return sent





def retry_outbox_item(db: Session, outbox_id: int) -> MessageOutbox | None:

    row = db.get(MessageOutbox, outbox_id)

    if row is None:

        return None

    row.status = MessageOutboxStatus.PENDING.value

    row.error_message = None

    db.flush()

    return row





def cancel_outbox_item(db: Session, outbox_id: int) -> MessageOutbox | None:

    row = db.get(MessageOutbox, outbox_id)

    if row is None or row.status != MessageOutboxStatus.PENDING.value:

        return None

    row.status = MessageOutboxStatus.CANCELLED.value

    row.error_message = None

    db.flush()

    return row





def cancel_pending_outbox(

    db: Session,

    *,

    event_types: set[str] | None = None,

) -> int:

    rows = list(

        db.scalars(

            select(MessageOutbox).where(

                MessageOutbox.status == MessageOutboxStatus.PENDING.value

            )

        ).all()

    )

    n = 0

    for row in rows:

        if event_types is not None and (row.event_type or "") not in event_types:

            continue

        row.status = MessageOutboxStatus.CANCELLED.value

        row.error_message = None

        n += 1

    db.flush()

    return n





def pending_outbox_count(db: Session) -> int:

    return (

        db.scalar(

            select(func.count())

            .select_from(MessageOutbox)

            .where(MessageOutbox.status == MessageOutboxStatus.PENDING.value)

        )

        or 0

    )





def estimate_queue_minutes(db: Session, pending: int) -> int:

    if pending <= 0:

        return 0

    batch = outbox_batch_size(db)

    delay = send_delay_seconds(db)

    interval = max(10, get_int(db, "messaging_worker_interval_seconds", 30))

    cycles = (pending + batch - 1) // batch

    secs_per_cycle = delay * max(0, batch - 1) + interval

    return max(1, (cycles * secs_per_cycle + 59) // 60)


