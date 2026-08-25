from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from modules.messaging.consent import customer_can_receive
from modules.messaging.models import MessageChannel
from modules.messaging.outbox import enqueue_message, send_outbox_ids_now
from modules.messaging.service import messaging_enabled
from modules.notifications.events import PHASE1_EVENT_KEYS, REFERRAL_LINK_CREATED
from modules.notifications.idempotency import (
    build_idempotency_key,
    default_throttle_minutes,
    is_throttled,
    log_exists,
)
from modules.notifications.models import (
    NotificationEvent,
    NotificationEventStatus,
    NotificationLog,
    NotificationLogStatus,
    NotificationMessageType,
    NotificationRule,
    NotificationTemplate,
)
from modules.notifications.providers.whatsapp import get_provider
from modules.notifications.providers.base import OutboundMessage
from modules.notifications.rules import evaluate_conditions
from modules.notifications.recipients import resolve_recipient, resolve_recipients
from modules.notifications.templates import parse_buttons_json, render_template
from modules.settings.service import get_bool, get_setting

LOG = logging.getLogger("notifications")


class NotificationService:
    @staticmethod
    def enabled(db: Session) -> bool:
        return get_bool(db, "notifications_enabled", True) and messaging_enabled(db)

    @staticmethod
    def _hub_forward_title_detail(
        event_key: str,
        label: str,
        payload: dict[str, Any],
        *,
        source_id: int | None = None,
    ) -> tuple[str, str]:
        """عنوان وتفاصيل عربية واضحة لإعادة توجيه مركز الإشعارات إلى واتساب."""
        pl = payload if isinstance(payload, dict) else {}
        pre = str(pl.get("hub_detail") or "").strip()
        if pre:
            kind_ar = str(pl.get("shift_kind_ar") or "").strip()
            if event_key == "treasury.handoff_pending" and kind_ar:
                return f"جلسة {kind_ar} بانتظار اعتماد الخزينة", pre
            return str(label), pre

        if event_key in (
            "treasury.handoff_pending",
            "treasury.shift_closed",
            "pos.shift_closed",
        ):
            kind_ar = str(pl.get("shift_kind_ar") or "").strip() or "مطعم"
            sid = pl.get("shift_id") or source_id or ""
            emp = (
                str(pl.get("employee_name") or pl.get("cashier_name") or "").strip()
                or "—"
            )
            closed = str(pl.get("closed_at") or "").strip()
            parts = [f"جلسة {kind_ar} #{sid}", f"الموظف: {emp}"]
            if closed and closed != "—":
                parts.append(f"أُغلقت: {closed}")
            title = (
                f"جلسة {kind_ar} بانتظار اعتماد الخزينة"
                if event_key == "treasury.handoff_pending"
                else f"إغلاق جلسة {kind_ar}"
            )
            return title, " · ".join(parts)

        hint_parts: list[str] = []
        labels_ar = {
            "room_number": "غرفة",
            "guest_name": "ضيف",
            "customer_name": "عميل",
            "operator_name": "موظف",
            "cashier_name": "موظف",
            "employee_name": "موظف",
            "shift_id": "جلسة",
            "reference": "مرجع",
            "booking_id": "حجز",
            "sale_id": "فاتورة",
            "order_type": "نوع",
        }
        for k in (
            "hub_detail",
            "shift_kind_ar",
            "shift_id",
            "cashier_name",
            "employee_name",
            "operator_name",
            "room_number",
            "guest_name",
            "customer_name",
            "reference",
            "booking_id",
            "sale_id",
            "total",
            "amount",
            "items_summary",
            "order_type",
            "closed_at",
        ):
            v = pl.get(k)
            if v is None or not str(v).strip():
                continue
            if k in ("hub_detail", "shift_kind_ar"):
                continue
            if k == "items_summary":
                hint_parts.append(str(v).strip())
            elif k == "total":
                hint_parts.append(f"الإجمالي={v}")
            elif k in labels_ar:
                hint_parts.append(f"{labels_ar[k]}: {v}")
            else:
                hint_parts.append(f"{k}={v}")
            if len(hint_parts) >= 6:
                break
        return str(label), " · ".join(hint_parts)

    @staticmethod
    def emit_event(
        db: Session,
        *,
        event_key: str,
        source_type: str,
        source_id: int | None,
        payload: dict[str, Any],
        process_now: bool = True,
    ) -> int | None:
        """Record event ledger row; optionally process immediately."""
        if not NotificationService.enabled(db):
            return None
        from modules.notifications.event_settings import is_notification_event_enabled
        from modules.notifications.module_settings import is_event_module_enabled

        if not is_notification_event_enabled(db, event_key):
            return None
        if not is_event_module_enabled(db, event_key):
            return None
        try:
            # لا نستخدم begin_nested هنا: إرسال outbox يعمل db.commit()
            # ووجود commit داخل savepoint يكسر الجلسة (closed transaction).
            row = NotificationEvent(
                event_key=event_key.strip()[:64],
                source_type=source_type.strip()[:32],
                source_id=source_id,
                payload_json=json.dumps(payload or {}, ensure_ascii=False),
                status=NotificationEventStatus.PENDING.value,
            )
            db.add(row)
            db.flush()
            try:
                from modules.dashboard_notify.whatsapp_forward import (
                    forward_hub_item_to_whatsapp,
                )
                from modules.notifications.events import ALL_EVENT_KEYS

                label = next(
                    (lab for k, lab in ALL_EVENT_KEYS if k == event_key),
                    event_key,
                )
                pl = payload or {}
                title_s, detail_s = NotificationService._hub_forward_title_detail(
                    event_key, label, pl, source_id=source_id
                )
                forward_hub_item_to_whatsapp(
                    db,
                    title=title_s,
                    detail=detail_s,
                    event_type=f"hub.event.{event_key}"[:64],
                    meta={
                        "event_key": event_key,
                        "source_type": source_type,
                        "source_id": source_id,
                        "notification_event_id": row.id,
                    },
                )
            except Exception:  # noqa: BLE001
                LOG.debug("activity hub WA forward skipped for %s", event_key, exc_info=True)
            if process_now:
                NotificationService.process_event(db, row.id)
            return row.id
        except SQLAlchemyError as exc:
            LOG.warning("emit_event database failure %s: %s", event_key, exc)
            return None
        except Exception as exc:  # noqa: BLE001
            LOG.warning("emit_event failed %s: %s", event_key, exc)
            return None

    @staticmethod
    def emit_event_background(
        event_key: str,
        source_type: str,
        source_id: int | None,
        payload: dict[str, Any],
    ) -> None:
        from infra.background import run_in_background, with_db

        def _run() -> None:
            with with_db() as db:
                NotificationService.emit_event(
                    db,
                    event_key=event_key,
                    source_type=source_type,
                    source_id=source_id,
                    payload=payload,
                    process_now=True,
                )
                db.commit()

        run_in_background(_run, name=f"notify-{event_key}")

    @staticmethod
    def process_event(db: Session, event_id: int) -> int:
        """Process rules for one ledger event. Returns count of logs created."""
        event = db.get(NotificationEvent, event_id)
        if event is None:
            return 0
        if event.status == NotificationEventStatus.PROCESSED.value:
            return 0
        try:
            payload = json.loads(event.payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        count = 0
        rules = list(
            db.scalars(
                select(NotificationRule)
                .options(joinedload(NotificationRule.template))
                .where(
                    NotificationRule.event_key == event.event_key,
                    NotificationRule.is_active.is_(True),
                )
                .order_by(NotificationRule.id.asc())
            )
            .unique()
            .all()
        )
        for rule in rules:
            if rule.template is None or not rule.template.is_active:
                continue
            if not evaluate_conditions(rule.condition_json, payload):
                continue
            try:
                if NotificationService._dispatch_rule(db, event, rule, payload):
                    count += 1
            except SQLAlchemyError:
                raise
            except Exception as exc:  # noqa: BLE001
                LOG.warning("rule %s dispatch failed: %s", rule.id, exc)
        event.status = NotificationEventStatus.PROCESSED.value
        event.processed_at = datetime.now(timezone.utc)
        db.flush()
        return count

    @staticmethod
    def _dispatch_rule(
        db: Session,
        event: NotificationEvent,
        rule: NotificationRule,
        payload: dict[str, Any],
    ) -> bool:
        tpl = rule.template
        if tpl is None:
            return False
        require_consent = (
            rule.recipient_type == "customer"
            and event.event_key
            not in (REFERRAL_LINK_CREATED, "hotel.balance_claim")
        )
        recipients = resolve_recipients(
            db,
            rule.recipient_type,
            payload,
            event_key=event.event_key,
            require_consent=require_consent,
        )
        if not recipients:
            NotificationService._log_skipped(
                db,
                event=event,
                rule=rule,
                tpl=tpl,
                reason="no_recipient",
                payload=payload,
            )
            return False
        sent_any = False
        for recipient in recipients:
            if not recipient.phone:
                continue
            idem = build_idempotency_key(
                event.event_key,
                event.source_type,
                event.source_id,
                rule.recipient_type,
                payload,
                extra=recipient.phone,
            )
            throttle = default_throttle_minutes(event.event_key, rule.throttle_minutes)
            if log_exists(db, idem) or is_throttled(
                db, idempotency_key=idem, throttle_minutes=throttle
            ):
                NotificationService._log_skipped(
                    db,
                    event=event,
                    rule=rule,
                    tpl=tpl,
                    reason="throttled",
                    payload=payload,
                    idempotency_key=idem,
                    recipient=recipient,
                )
                continue
            vars_out = NotificationService._template_vars(
                db, payload, event_key=event.event_key
            )
            body = render_template(tpl.body_template, vars_out)
            buttons = parse_buttons_json(tpl.buttons_json, vars_out)
            msg_type = tpl.message_type or NotificationMessageType.TEXT.value
            log = NotificationLog(
                event_key=event.event_key,
                notification_event_id=event.id,
                source_type=event.source_type,
                source_id=event.source_id,
                recipient_type=rule.recipient_type,
                recipient_name=recipient.name,
                recipient_phone=recipient.phone,
                channel=rule.channel,
                message_type=msg_type,
                template_id=tpl.id,
                body_rendered=body,
                provider="textmebot",
                status=NotificationLogStatus.QUEUED.value,
                idempotency_key=idem,
                attempts=0,
            )
            try:
                with db.begin_nested():
                    db.add(log)
                    db.flush()
            except IntegrityError:
                # مفتاح تكرار — تجاهل دون إفساد جلسة قاعدة البيانات
                continue
            if NotificationService._send_log(db, log, tpl, body, buttons, recipient, payload):
                sent_any = True
        return sent_any

    @staticmethod
    def _log_skipped(
        db: Session,
        *,
        event: NotificationEvent,
        rule: NotificationRule,
        tpl: NotificationTemplate,
        reason: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
        recipient: Any = None,
    ) -> None:
        idem = idempotency_key or build_idempotency_key(
            event.event_key,
            event.source_type,
            event.source_id,
            rule.recipient_type,
            payload,
            extra=reason,
        )
        if log_exists(db, idem, include_skipped=True):
            return
        try:
            with db.begin_nested():
                db.add(
                    NotificationLog(
                        event_key=event.event_key,
                        notification_event_id=event.id,
                        source_type=event.source_type,
                        source_id=event.source_id,
                        recipient_type=rule.recipient_type,
                        recipient_name=getattr(recipient, "name", None),
                        recipient_phone=getattr(recipient, "phone", None),
                        channel=rule.channel,
                        message_type=tpl.message_type,
                        template_id=tpl.id,
                        body_rendered=reason,
                        status=NotificationLogStatus.SKIPPED.value,
                        idempotency_key=idem[:200],
                    )
                )
                db.flush()
        except IntegrityError:
            return

    @staticmethod
    def _template_vars(
        db: Session,
        payload: dict[str, Any],
        *,
        event_key: str | None = None,
    ) -> dict[str, Any]:
        from modules.branding.service import hotel_display_name

        payload_store = (payload.get("store_name") or "").strip()
        if payload_store:
            store = payload_store
        elif event_key and str(event_key).startswith("hotel."):
            store = hotel_display_name(db)
        else:
            store = get_setting(db, "store_name", "نقطة البيع") or "نقطة البيع"
        vars_out = {str(k): ("" if v is None else v) for k, v in (payload or {}).items()}
        vars_out.update({
            "store_name": store,
            "customer_name": payload.get("customer_name") or payload.get("name") or "عميلنا",
            "phone": payload.get("phone") or "",
            "order_id": payload.get("order_id") or payload.get("sale_id") or "",
            "sale_id": payload.get("sale_id") or payload.get("order_id") or "",
            "total": payload.get("total") or "",
            "points": payload.get("points") or "0",
            "points_value": payload.get("points_value") or "0",
            "earned_points": payload.get("earned_points") or "0",
            "earned_points_value": payload.get("earned_points_value") or "0",
            "earn_line": payload.get("earn_line") or "",
            "balance": payload.get("balance") or "0",
            "balance_value": payload.get("balance_value") or "0",
            "product_name": payload.get("product_name") or "",
            "unit": payload.get("unit") or "",
            "balance_qty": payload.get("balance") or payload.get("balance_qty") or "",
            "reorder_level": payload.get("reorder_level") or "",
            "driver_name": payload.get("driver_name") or "",
            "cashier_name": payload.get("cashier_name") or "",
            "employee_name": payload.get("employee_name")
            or payload.get("cashier_name")
            or "",
            "shift_id": payload.get("shift_id") or "",
            "shortage": payload.get("shortage") or "",
            "cash_shortage": payload.get("cash_shortage") or "",
            "bank_shortage": payload.get("bank_shortage") or "",
            "shortage_detail": payload.get("shortage_detail") or "",
            "reason": payload.get("reason") or "",
            "action_id": payload.get("action_id") or "",
            "line_id": payload.get("line_id") or "",
            "message": payload.get("message") or "",
            "purchase_id": payload.get("purchase_id") or "",
            "supplier": payload.get("supplier") or "",
            "amount": payload.get("amount") or "",
            "movement_qty": payload.get("movement_qty") or "",
            "note": payload.get("note") or "",
            "lot_code": payload.get("lot_code") or "",
            "days_left": payload.get("days_left") or "",
            "qty": payload.get("qty") or "",
            "expiry_date": payload.get("expiry_date") or "",
            "old_cost": payload.get("old_cost") or "",
            "new_cost": payload.get("new_cost") or "",
            "item_count": payload.get("item_count") or "",
            "referral_code": payload.get("referral_code") or "",
            "buyer_referral_code": payload.get("buyer_referral_code") or "",
            "referral_code_line": payload.get("referral_code_line") or "",
            "referral_share_block": payload.get("referral_share_block")
            or payload.get("referral_code_line")
            or "",
            "share_url": payload.get("share_url") or "",
            "referrer_points": payload.get("referrer_points") or "",
            "buyer_points": payload.get("buyer_points") or "",
            "buyer_points_value": payload.get("buyer_points_value") or "",
            "referrer_name": payload.get("referrer_name") or "",
            "referrer_phone": payload.get("referrer_phone") or "",
            "referrer_customer_id": payload.get("referrer_customer_id") or "",
            "buyer_name": payload.get("buyer_name") or "",
            "employee_count": payload.get("employee_count") or "",
            "total_net": payload.get("total_net") or "",
            "run_id": payload.get("run_id") or "",
            "digest_date": payload.get("digest_date") or "",
            "product_id": payload.get("product_id") or "",
            "ticket_id": payload.get("ticket_id") or "",
            "section_name": payload.get("section_name") or "",
            "employee_id": payload.get("employee_id") or "",
            "employee_phone": payload.get("employee_phone") or "",
            "check_in_time": payload.get("check_in_time") or "",
            "check_out_time": payload.get("check_out_time") or "",
            "hours_worked": payload.get("hours_worked") or "",
            "late_minutes": payload.get("late_minutes") or "",
            "overtime_hours": payload.get("overtime_hours") or "",
            "early_leave_minutes": payload.get("early_leave_minutes") or "",
            "attendance_status": payload.get("attendance_status") or "",
            "period_label": payload.get("period_label") or "",
            "net_pay": payload.get("net_pay") or "",
            "gross_pay": payload.get("gross_pay") or "",
            "deductions": payload.get("deductions") or "",
            "advances": payload.get("advances") or "",
            "bonuses": payload.get("bonuses") or "",
            "overtime_pay": payload.get("overtime_pay") or "",
            "advance_amount": payload.get("advance_amount") or "",
            "deduction_amount": payload.get("deduction_amount") or "",
            "deduction_note": payload.get("deduction_note") or "",
            "payroll_entry_id": payload.get("payroll_entry_id") or "",
            "booking_id": payload.get("booking_id") or "",
            "booking_reference": payload.get("booking_reference") or "",
            "guest_name": payload.get("guest_name") or "",
            "guest_phone": payload.get("guest_phone") or payload.get("phone") or "",
            "room_name": payload.get("room_name") or "",
            "room_type": payload.get("room_type") or "",
            "check_in_date": payload.get("check_in_date") or "",
            "check_out_date": payload.get("check_out_date") or "",
            "nights": payload.get("nights") or "",
            "nightly_rate": payload.get("nightly_rate") or "",
            "booking_total": payload.get("booking_total") or "",
            "paid_amount": payload.get("paid_amount") or "",
            "balance_due": payload.get("balance_due") or "",
            "folio_lines": payload.get("folio_lines") or "",
            "folio_summary": payload.get("folio_summary") or "",
            "service_name": payload.get("service_name") or "",
            "service_amount": payload.get("service_amount") or "",
            "payment_amount": payload.get("payment_amount") or payload.get("amount") or "",
            "payment_method": payload.get("payment_method") or "",
            "payment_id": payload.get("payment_id") or "",
            "payment_label": payload.get("payment_label") or "",
            "doc_title": payload.get("doc_title") or payload.get("payment_label") or "إيصال قبض",
            "booking_url": payload.get("booking_url") or "",
            "invoice_url": payload.get("invoice_url") or "",
            "reception_phone": payload.get("reception_phone") or "",
            "checkout_time_line": payload.get("checkout_time_line") or "",
            "grace_deadline": payload.get("grace_deadline") or "",
            "new_check_out": payload.get("new_check_out") or "",
            "claim_note_line": payload.get("claim_note_line") or "",
            "claim_note": payload.get("claim_note") or "",
        })
        return vars_out

    @staticmethod
    def _send_log(
        db: Session,
        log: NotificationLog,
        tpl: NotificationTemplate,
        body: str,
        buttons: list[dict[str, str]],
        recipient: Any,
        payload: dict[str, Any],
    ) -> bool:
        meta: dict[str, Any] = {"kind": "automatic", "notification_log_id": log.id}
        if buttons:
            meta["buttons"] = buttons
        img = (payload.get("image_url") or tpl.image_url or "").strip()
        if img:
            meta["image_url"] = render_template(
                img,
                NotificationService._template_vars(
                    db, payload, event_key=log.event_key
                ),
            )
        doc = (payload.get("document_url") or tpl.document_url or "").strip()
        if doc:
            meta["document_url"] = render_template(
                doc,
                NotificationService._template_vars(
                    db, payload, event_key=log.event_key
                ),
            )
        if recipient.telegram_chat_id:
            meta["telegram_chat_id"] = recipient.telegram_chat_id
        outbox = enqueue_message(
            db,
            body=body,
            channel=MessageChannel.WHATSAPP.value,
            phone=recipient.phone,
            customer_id=recipient.customer_id,
            event_type=log.event_key,
            meta=meta,
        )
        log.outbox_id = outbox.id
        log.attempts = 0
        # مطالبة الدين يدوياً: إرسال فوري (مثل فاتورة الحجز) حتى لا يظن الموظف أن الرسالة أُرسلت وهي في الطابور فقط
        immediate = str(payload.get("_immediate") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if immediate or log.event_key == "hotel.balance_claim":
            from modules.messaging.models import MessageOutboxStatus
            from modules.messaging.outbox import send_outbox_item_now

            row = send_outbox_item_now(db, outbox)
            log.attempts = 1
            log.last_attempt_at = datetime.now(timezone.utc)
            if row.status == MessageOutboxStatus.SENT.value:
                log.status = NotificationLogStatus.SENT.value
                log.sent_at = datetime.now(timezone.utc)
                log.error_message = None
                return True
            log.status = NotificationLogStatus.FAILED.value
            log.error_message = (row.error_message or "فشل إرسال واتساب").strip()[:500]
            return False
        log.status = NotificationLogStatus.QUEUED.value
        # باقي الأحداث: عامل الرسائل يلتقط الطابور حتى لا يتجمّد إتمام البيع
        return True

    @staticmethod
    def retry_failed(db: Session, *, limit: int = 50) -> int:
        rows = list(
            db.scalars(
                select(NotificationLog)
                .where(
                    NotificationLog.status == NotificationLogStatus.FAILED.value,
                    NotificationLog.attempts < 3,
                )
                .order_by(NotificationLog.created_at.asc())
                .limit(limit)
            ).all()
        )
        n = 0
        for log in rows:
            if not log.outbox_id or not log.recipient_phone:
                continue
            try:
                send_outbox_ids_now(db, [log.outbox_id])
                log.attempts += 1
                log.last_attempt_at = datetime.now(timezone.utc)
                from modules.messaging.models import MessageOutbox

                ob = db.get(MessageOutbox, log.outbox_id)
                if ob and ob.status == "sent":
                    log.status = NotificationLogStatus.SENT.value
                    log.sent_at = datetime.now(timezone.utc)
                    n += 1
                else:
                    log.error_message = (ob.error_message if ob else "retry failed")[:500]
            except Exception as exc:  # noqa: BLE001
                log.attempts += 1
                log.error_message = str(exc)[:500]
        return n

    @staticmethod
    def process_pending_events(db: Session, *, limit: int = 20) -> int:
        rows = list(
            db.scalars(
                select(NotificationEvent)
                .where(NotificationEvent.status == NotificationEventStatus.PENDING.value)
                .order_by(NotificationEvent.created_at.asc())
                .limit(limit)
            ).all()
        )
        total = 0
        for row in rows:
            total += NotificationService.process_event(db, row.id)
        return total


def emit_event_safe(
    db: Session,
    *,
    event_key: str,
    source_type: str,
    source_id: int | None,
    payload: dict[str, Any],
) -> None:
    try:
        NotificationService.emit_event(
            db,
            event_key=event_key,
            source_type=source_type,
            source_id=source_id,
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("emit_event_safe: %s", exc)


def emit_event_background_safe(
    event_key: str,
    source_type: str,
    source_id: int | None,
    payload: dict[str, Any],
) -> None:
    try:
        NotificationService.emit_event_background(
            event_key, source_type, source_id, payload
        )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("emit_event_background_safe: %s", exc)
