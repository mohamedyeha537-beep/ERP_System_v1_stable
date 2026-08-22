from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from modules.notifications.models import (
    NotificationAction,
    NotificationActionStatus,
)


_ACTION_RE = re.compile(
    r"^(order_received|order_confirm|pos_approve|pos_reject|payroll_confirm|housekeeping_done|housekeeping_confirm|hotel_approve|hotel_reject):(\d+)$",
    re.I,
)


def create_pending_action(
    db: Session,
    *,
    action_key: str,
    payload: dict[str, Any],
) -> NotificationAction:
    row = NotificationAction(
        notification_log_id=None,
        action_key=action_key,
        action_payload_json=json.dumps(payload or {}, ensure_ascii=False),
        handled_status=NotificationActionStatus.PENDING.value,
    )
    db.add(row)
    db.flush()
    return row


def handle_incoming_action(
    db: Session,
    *,
    phone: str,
    text: str,
) -> dict[str, Any] | None:
    """يُستدعى بعد تسجيل رسالة واردة — يعالج أزرار TextMeBot."""
    raw = (text or "").strip()
    if not raw:
        return None
    m = _ACTION_RE.match(raw)
    if not m:
        return _match_by_button_label(db, phone=phone, label=raw)
    action_key, ref_id = m.group(1).lower(), int(m.group(2))
    return _dispatch_action(db, phone=phone, action_key=action_key, ref_id=ref_id, raw=raw)


def _match_by_button_label(db: Session, *, phone: str, label: str) -> dict[str, Any] | None:
    low = (label or "").strip()
    confirm_labels = (
        "نعم — انتهى التنظيف",
        "نعم، انتهى التنظيف",
        "متأكد — انتهى التنظيف",
        "تأكيد انتهاء التنظيف",
    )
    if low in confirm_labels or (
        "متأكد" in low and "تنظيف" in low
    ) or (
        low.startswith("نعم") and "تنظيف" in low
    ):
        from modules.hotel.booking_models import RoomPhysicalStatus
        from modules.hotel.models import HotelRoom
        from sqlalchemy import select

        room = db.scalar(
            select(HotelRoom)
            .where(
                HotelRoom.is_active.is_(True),
                HotelRoom.physical_status == RoomPhysicalStatus.CLEANING,
            )
            .order_by(HotelRoom.id.desc())
            .limit(1)
        )
        if room is None:
            return {
                "ok": False,
                "action_key": "housekeeping_confirm",
                "error": "لا توجد شقة قيد التنظيف حالياً.",
            }
        return _dispatch_action(
            db,
            phone=phone,
            action_key="housekeeping_confirm",
            ref_id=int(room.id),
            raw=label,
        )
    if "تم الانتهاء من التنظيف" in low or low in ("تم التنظيف", "انتهى التنظيف", "✓ تم الانتهاء من التنظيف"):
        # بدون رقم شقة في النص — نأخذ أحدث شقة قيد التنظيف
        from modules.hotel.booking_models import RoomPhysicalStatus
        from modules.hotel.models import HotelRoom
        from sqlalchemy import select

        room = db.scalar(
            select(HotelRoom)
            .where(
                HotelRoom.is_active.is_(True),
                HotelRoom.physical_status == RoomPhysicalStatus.CLEANING,
            )
            .order_by(HotelRoom.id.desc())
            .limit(1)
        )
        if room is None:
            return {
                "ok": False,
                "action_key": "housekeeping_done",
                "error": "لا توجد شقة قيد التنظيف حالياً.",
            }
        return _dispatch_action(
            db,
            phone=phone,
            action_key="housekeeping_done",
            ref_id=int(room.id),
            raw=label,
        )
    if "استلمت" in label or "تأكيد الاستلام" in label:
        return None
    hotel_approve = "موافقة" in low or low.startswith("✓")
    hotel_reject = "رفض" in low or low.startswith("✗")
    if hotel_approve or hotel_reject:
        from modules.hotel.cancel_approval import cancel_admin_phone, phones_match
        from modules.notifications.models import NotificationAction, NotificationActionStatus
        from sqlalchemy import select

        admin = cancel_admin_phone(db)
        if admin and phones_match(admin, phone):
            row = db.scalar(
                select(NotificationAction)
                .where(
                    NotificationAction.handled_status
                    == NotificationActionStatus.PENDING.value,
                    NotificationAction.action_key.in_(
                        (
                            "cancel",
                            "late_cancel",
                            "no_show",
                            "waive_auto_night",
                            "waive_previous_night",
                        )
                    ),
                )
                .order_by(NotificationAction.id.desc())
            )
            if row is not None:
                return _dispatch_action(
                    db,
                    phone=phone,
                    action_key="hotel_approve" if hotel_approve else "hotel_reject",
                    ref_id=int(row.id),
                    raw=label,
                )
    return None


def _dispatch_action(
    db: Session,
    *,
    phone: str,
    action_key: str,
    ref_id: int,
    raw: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {"action_key": action_key, "ref_id": ref_id}
    act = db.get(NotificationAction, ref_id)
    if act is None and action_key == "payroll_confirm":
        result["ok"] = False
        result["error"] = "طلب تأكيد غير موجود أو منتهي."
        return result
    if act is None and action_key in (
        "pos_approve",
        "pos_reject",
        "hotel_approve",
        "hotel_reject",
    ):
        act = NotificationAction(
            notification_log_id=None,
            action_key=f"{action_key}:{ref_id}",
            action_payload_json=json.dumps({"raw": raw}, ensure_ascii=False),
            received_from_phone=phone,
            handled_status=NotificationActionStatus.FAILED.value,
            result_message="طلب غير موجود أو منتهي.",
        )
        db.add(act)
        db.flush()
        result["ok"] = False
        return result

    if act is None:
        act = NotificationAction(
            notification_log_id=None,
            action_key=f"{action_key}:{ref_id}",
            action_payload_json=json.dumps({"sale_id": ref_id, "raw": raw}, ensure_ascii=False),
            received_from_phone=phone,
            handled_status=NotificationActionStatus.PENDING.value,
        )
        db.add(act)
        db.flush()

    act.received_from_phone = phone
    try:
        if action_key == "order_received":
            from modules.sales.models import Sale

            sale = db.get(Sale, ref_id)
            if sale is None:
                raise ValueError("الطلب غير موجود.")
            if sale.served_to_customer_at is None:
                sale.served_to_customer_at = datetime.now(timezone.utc)
            act.handled_status = NotificationActionStatus.HANDLED.value
            act.handled_at = datetime.now(timezone.utc)
            act.result_message = f"تم تأكيد استلام الطلب #{ref_id}"
            result["ok"] = True
        elif action_key == "pos_approve":
            result = _handle_pos_approve(db, act, ref_id=ref_id)
        elif action_key == "hotel_approve":
            result = _handle_hotel_cancel_approve(db, act, phone=phone)
        elif action_key in ("pos_reject", "hotel_reject"):
            act.handled_status = NotificationActionStatus.HANDLED.value
            act.handled_at = datetime.now(timezone.utc)
            act.result_message = "تم رفض الطلب."
            result["ok"] = True
            if action_key == "hotel_reject":
                _ack_hotel_cancel(db, act, phone=phone, approved=False)
        elif action_key == "payroll_confirm":
            result = _handle_payroll_confirm(db, act, ref_id=ref_id, phone=phone)
        elif action_key == "housekeeping_done":
            # الخطوة الأولى فقط: طلب تأكيد — لا تُحدَّث الشقة بعد
            result = _handle_housekeeping_done_ask(db, act, ref_id=ref_id, phone=phone)
        elif action_key == "housekeeping_confirm":
            result = _handle_housekeeping_done(db, act, ref_id=ref_id, phone=phone)
        else:
            act.handled_status = NotificationActionStatus.FAILED.value
            act.result_message = "إجراء غير مدعوم."
            result["ok"] = False
    except Exception as exc:  # noqa: BLE001
        act.handled_status = NotificationActionStatus.FAILED.value
        act.result_message = str(exc)[:500]
        result["ok"] = False
        result["error"] = str(exc)
    db.flush()
    result["action_id"] = act.id
    return result


def _handle_housekeeping_done_ask(
    db: Session, act: NotificationAction, *, ref_id: int, phone: str
) -> dict[str, Any]:
    """الزر الأول: اسأل للتأكيد — لا تُحدَّث حالة الشقة بعد."""
    from modules.hotel.booking_models import RoomPhysicalStatus
    from modules.hotel.dashboard import room_display_name
    from modules.hotel.housekeeping_links import housekeeping_done_url
    from modules.hotel.models import HotelRoom
    from modules.messaging.models import MessageChannel
    from modules.messaging.outbox import enqueue_message, send_outbox_item_now
    from modules.settings.service import get_setting

    room = db.get(HotelRoom, ref_id)
    if room is None:
        raise ValueError("الشقة غير موجودة.")
    name = room_display_name(room)
    if room.physical_status == RoomPhysicalStatus.AVAILABLE:
        act.handled_status = NotificationActionStatus.HANDLED.value
        act.handled_at = datetime.now(timezone.utc)
        act.result_message = f"الشقة {name} جاهزة مسبقاً."
        return {"ok": True, "action_id": act.id, "note": "already_clean", "room_id": room.id}

    base_url = (get_setting(db, "public_base_url", "") or "").strip()
    done_url = housekeeping_done_url(db, room.id, base_url=base_url)
    # زر التأكيد الثاني: رابط صفحة السؤال إن وُجد، وإلا رد سريع housekeeping_confirm
    confirm_id = done_url if done_url else f"housekeeping_confirm:{int(room.id)}"

    act.handled_status = NotificationActionStatus.HANDLED.value
    act.handled_at = datetime.now(timezone.utc)
    act.received_from_phone = phone
    act.result_message = f"طُلب تأكيد انتهاء تنظيف {name} (بانتظار موافقة الموظف)"
    try:
        db.commit()
        db.refresh(act)
    except Exception:  # noqa: BLE001
        db.rollback()
        raise

    body = (
        f"🧹 *هل انتهى التنظيف؟*\n"
        f"الشقة: *{name}* (#{room.number})\n\n"
        f"هل أنت متأكد من تأكيد انتهاء عملية التنظيف؟\n"
        f"إذا ضغطت الزر السابق بالخطأ، تجاهل هذه الرسالة."
    )
    buttons = [{"text": "نعم — انتهى التنظيف", "id": confirm_id}]
    try:
        row = enqueue_message(
            db,
            body=body,
            channel=MessageChannel.WHATSAPP.value,
            phone=phone,
            event_type="hotel.room_cleaning_confirm_ask",
            meta={
                "kind": "housekeeping_confirm_ask",
                "room_id": room.id,
                "buttons": buttons,
            },
        )
        db.flush()
        send_outbox_item_now(db, row)
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    return {
        "ok": True,
        "action_id": act.id,
        "room_id": room.id,
        "note": "awaiting_confirm",
        "confirm_url": done_url or "",
    }


def _handle_housekeeping_done(
    db: Session, act: NotificationAction, *, ref_id: int, phone: str
) -> dict[str, Any]:
    """الزر الثاني / التأكيد الصريح: تم الانتهاء → الشقة متاحة."""
    from modules.hotel.booking_models import RoomPhysicalStatus
    from modules.hotel.booking_service import BookingError, mark_room_clean
    from modules.hotel.dashboard import room_display_name
    from modules.hotel.models import HotelRoom
    from modules.messaging.models import MessageChannel
    from modules.messaging.outbox import enqueue_message, send_outbox_item_now

    room = db.get(HotelRoom, ref_id)
    if room is None:
        raise ValueError("الشقة غير موجودة.")
    if room.physical_status == RoomPhysicalStatus.AVAILABLE:
        act.handled_status = NotificationActionStatus.HANDLED.value
        act.handled_at = datetime.now(timezone.utc)
        act.result_message = f"الشقة {room_display_name(room)} جاهزة مسبقاً."
        return {"ok": True, "action_id": act.id, "note": "already_clean", "room_id": room.id}
    try:
        mark_room_clean(db, ref_id, user_id=None)
    except BookingError as exc:
        raise ValueError(str(exc)) from exc

    act.handled_status = NotificationActionStatus.HANDLED.value
    act.handled_at = datetime.now(timezone.utc)
    act.received_from_phone = phone
    name = room_display_name(room)
    act.result_message = f"تم تأكيد انتهاء تنظيف {name}"
    # احفظ حالة الشقة قبل إرسال الرد — send_outbox_item_now يعمل commit/rollback
    # وقد كان rollback عند فشل الإرسال يعيد الشقة إلى «قيد التنظيف».
    try:
        db.commit()
        db.refresh(room)
        db.refresh(act)
    except Exception:  # noqa: BLE001
        db.rollback()
        raise
    try:
        row = enqueue_message(
            db,
            body=(
                f"✅ تم تسجيل انتهاء التنظيف\n"
                f"الشقة: *{name}* (#{room.number})\n"
                f"الحالة الآن: متاحة للحجز."
            ),
            channel=MessageChannel.WHATSAPP.value,
            phone=phone,
            event_type="hotel.room_cleaning_done_ack",
            meta={"kind": "housekeeping_ack", "room_id": room.id},
        )
        db.flush()
        send_outbox_item_now(db, row)
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "action_id": act.id, "room_id": room.id}


def _handle_payroll_confirm(
    db: Session, act: NotificationAction, *, ref_id: int, phone: str
) -> dict[str, Any]:
    if act is None or act.id != ref_id:
        act = db.get(NotificationAction, ref_id)
    if act is None:
        raise ValueError("طلب تأكيد غير موجود.")
    if act.handled_status == NotificationActionStatus.HANDLED.value:
        return {"ok": True, "action_id": act.id, "note": "already_handled"}
    try:
        payload = json.loads(act.action_payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    entry_id = int(payload.get("payroll_entry_id") or 0)
    if entry_id <= 0:
        raise ValueError("بند الراتب غير محدد.")
    from modules.hr.models import Employee, PayrollEntry
    from modules.messaging.providers.textmebot import normalize_whatsapp_phone
    from modules.settings.service import get_setting

    entry = db.get(PayrollEntry, entry_id)
    if entry is None:
        raise ValueError("بند الراتب غير موجود.")
    emp = db.get(Employee, entry.employee_id)
    if emp is None or not emp.phone:
        raise ValueError("لا يوجد رقم واتساب للموظف.")
    cc = (get_setting(db, "messaging_country_code") or "218").strip()
    expected = normalize_whatsapp_phone(emp.phone, country_code=cc)
    got = normalize_whatsapp_phone(phone, country_code=cc)
    if expected != got:
        raise ValueError("التأكيد مسموح من رقم الموظف المسجّل فقط.")
    if entry.salary_receipt_confirmed_at is not None:
        act.handled_status = NotificationActionStatus.HANDLED.value
        act.handled_at = datetime.now(timezone.utc)
        act.result_message = "تم تأكيد الاستلام مسبقاً."
        return {"ok": True, "action_id": act.id, "note": "already_confirmed"}
    entry.salary_receipt_confirmed_at = datetime.now(timezone.utc)
    entry.salary_receipt_confirmed_via = "whatsapp"
    act.received_from_phone = phone
    act.handled_status = NotificationActionStatus.HANDLED.value
    act.handled_at = datetime.now(timezone.utc)
    period = entry.run.label if entry.run else ""
    act.result_message = f"أكّد {emp.full_name_ar} استلام راتب {period}"
    return {"ok": True, "action_id": act.id}


def _handle_pos_approve(db: Session, act: NotificationAction, *, ref_id: int) -> dict[str, Any]:
    try:
        payload = json.loads(act.action_payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    kind = payload.get("kind") or act.action_key
    if act.handled_status == NotificationActionStatus.HANDLED.value:
        return {"ok": True, "action_id": act.id, "note": "already_handled"}

    if kind == "void_line":
        from modules.sales.void_service import void_sent_line, find_void_supervisor_for_phone
        from modules.hr.models import Employee

        sale_id = int(payload.get("sale_id") or 0)
        line_id = int(payload.get("line_id") or 0)
        reason = (payload.get("reason") or "موافقة مشرف عبر واتساب").strip()
        supervisor_id = payload.get("supervisor_employee_id")
        user_id = payload.get("cashier_user_id")
        supervisor = db.get(Employee, int(supervisor_id)) if supervisor_id else None
        if supervisor is None and act.received_from_phone:
            supervisor = find_void_supervisor_for_phone(db, act.received_from_phone)
        if supervisor is None:
            raise ValueError("المشرف غير محدد في الطلب.")
        void_sent_line(
            db,
            sale_id=sale_id,
            line_id=line_id,
            reason=reason,
            supervisor=supervisor,
            user_id=int(user_id) if user_id else None,
        )
    elif kind == "void_sale":
        from modules.sales.void_service import void_sent_sale, find_void_supervisor_for_phone
        from modules.hr.models import Employee

        sale_id = int(payload.get("sale_id") or 0)
        reason = (payload.get("reason") or "موافقة مشرف عبر واتساب").strip()
        supervisor_id = payload.get("supervisor_employee_id")
        user_id = payload.get("cashier_user_id")
        supervisor = db.get(Employee, int(supervisor_id)) if supervisor_id else None
        if supervisor is None and act.received_from_phone:
            supervisor = find_void_supervisor_for_phone(db, act.received_from_phone)
        if supervisor is None:
            raise ValueError("المشرف غير محدد في الطلب.")
        void_sent_sale(
            db,
            sale_id=sale_id,
            reason=reason,
            supervisor=supervisor,
            user_id=int(user_id) if user_id else None,
        )
    else:
        raise ValueError("نوع طلب غير معروف.")

    act.handled_status = NotificationActionStatus.HANDLED.value
    act.handled_at = datetime.now(timezone.utc)
    act.result_message = "تمت الموافقة وتنفيذ الإجراء."
    return {"ok": True, "action_id": act.id}


def _handle_hotel_cancel_approve(
    db: Session, act: NotificationAction, *, phone: str
) -> dict[str, Any]:
    from modules.hotel.cancel_approval import (
        cancel_admin_phone,
        execute_approved_cancel,
        phones_match,
    )

    if act.handled_status == NotificationActionStatus.HANDLED.value:
        return {"ok": True, "action_id": act.id, "note": "already_handled"}
    try:
        payload = json.loads(act.action_payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    admin = cancel_admin_phone(db)
    if admin and not phones_match(admin, phone):
        raise ValueError("هذا الرقم غير مخوّل لاعتماد إلغاء الحجوزات.")
    note = execute_approved_cancel(db, payload)
    act.handled_status = NotificationActionStatus.HANDLED.value
    act.handled_at = datetime.now(timezone.utc)
    act.received_from_phone = phone
    act.result_message = note
    try:
        db.commit()
        db.refresh(act)
    except Exception:
        db.rollback()
        raise
    _ack_hotel_cancel(db, act, phone=phone, approved=True, note=note)
    return {"ok": True, "action_id": act.id}


def _ack_hotel_cancel(
    db: Session,
    act: NotificationAction,
    *,
    phone: str,
    approved: bool,
    note: str = "",
) -> None:
    from modules.messaging.models import MessageChannel
    from modules.messaging.outbox import enqueue_message, send_outbox_item_now

    body = (
        f"✅ {note or 'تمت الموافقة وتنفيذ الإلغاء.'}"
        if approved
        else "✗ تم رفض طلب الإلغاء — لم يُغيَّر الحجز."
    )
    try:
        row = enqueue_message(
            db,
            body=body,
            channel=MessageChannel.WHATSAPP.value,
            phone=phone,
            event_type="hotel.cancel_approval_ack",
            meta={"action_id": act.id, "approved": approved},
        )
        db.flush()
        send_outbox_item_now(db, row)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass

