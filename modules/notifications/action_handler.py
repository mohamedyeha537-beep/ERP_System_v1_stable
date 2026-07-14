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
    r"^(order_received|order_confirm|pos_approve|pos_reject|payroll_confirm):(\d+)$", re.I
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
    if "استلمت" in label or "تأكيد الاستلام" in label:
        return None
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
    if act is None and action_key in ("pos_approve", "pos_reject"):
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
        elif action_key == "pos_reject":
            act.handled_status = NotificationActionStatus.HANDLED.value
            act.handled_at = datetime.now(timezone.utc)
            act.result_message = "تم رفض الطلب."
            result["ok"] = True
        elif action_key == "payroll_confirm":
            result = _handle_payroll_confirm(db, act, ref_id=ref_id, phone=phone)
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
