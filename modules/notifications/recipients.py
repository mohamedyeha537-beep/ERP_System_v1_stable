from __future__ import annotations



from dataclasses import dataclass

from typing import Any



from sqlalchemy.orm import Session



from modules.customers.models import Customer

from modules.messaging.consent import customer_can_receive, get_profile

from modules.notifications.routing import resolve_system_phones

from modules.notifications.rules import evaluate_conditions

from modules.settings.service import get_setting



__all__ = ["ResolvedRecipient", "evaluate_conditions", "resolve_recipient", "resolve_recipients"]





@dataclass

class ResolvedRecipient:

    phone: str | None

    name: str | None

    customer_id: int | None = None

    telegram_chat_id: str | None = None

    employee_id: int | None = None





def resolve_recipients(

    db: Session,

    recipient_type: str,

    payload: dict[str, Any],

    *,

    event_key: str = "",

    require_consent: bool = True,

) -> list[ResolvedRecipient]:

    """قد يُعيد أكثر من مستلم (مسارات متعددة الأرقام)."""

    rt = (recipient_type or "").strip().lower()

    if rt in ("admin", "supervisor", "inventory_manager", "hr_manager", "treasury_clerk"):

        phones = resolve_system_phones(

            db, event_key=event_key or str(payload.get("event_key") or ""), recipient_type=rt

        )

        labels = {

            "admin": "الإدارة",

            "supervisor": "المشرف",

            "inventory_manager": "مخزون",

            "hr_manager": "موارد بشرية",

            "treasury_clerk": "أمين الخزينة",

        }

        return [

            ResolvedRecipient(phone=p, name=labels.get(rt, rt))

            for p in phones

            if p

        ]

    one = resolve_recipient(

        db, recipient_type, payload, require_consent=require_consent, event_key=event_key

    )

    return [one] if one and one.phone else []





def resolve_recipient(

    db: Session,

    recipient_type: str,

    payload: dict[str, Any],

    *,

    require_consent: bool = True,

    event_key: str = "",

) -> ResolvedRecipient | None:

    rt = (recipient_type or "").strip().lower()

    if rt == "customer":

        cid = payload.get("customer_id")

        phone = (payload.get("phone") or "").strip()

        name = (payload.get("customer_name") or payload.get("name") or "").strip() or None

        if cid:

            cust = db.get(Customer, int(cid))

            if cust is None:

                return None

            if require_consent and not customer_can_receive(db, cust.id):

                return None

            phone = cust.phone or phone

            name = cust.name or name

            prof = get_profile(db, cust.id)

            tg = prof.telegram_chat_id if prof else None

            return ResolvedRecipient(

                phone=phone, name=name, customer_id=cust.id, telegram_chat_id=tg

            )

        if phone:

            return ResolvedRecipient(phone=phone, name=name)

        return None

    if rt == "employee":

        eid = payload.get("employee_id")

        phone = (payload.get("phone") or payload.get("employee_phone") or "").strip()

        name = (payload.get("employee_name") or payload.get("name") or "").strip() or None

        if eid:

            from modules.hr.models import Employee



            emp = db.get(Employee, int(eid))

            if emp is None:

                return None

            phone = emp.phone or phone

            name = emp.full_name_ar or name

            if not phone:

                return None

            return ResolvedRecipient(

                phone=phone, name=name, employee_id=emp.id

            )

        if phone:

            return ResolvedRecipient(phone=phone, name=name)

        return None

    if rt == "admin":

        phones = resolve_system_phones(

            db, event_key=event_key or str(payload.get("event_key") or ""), recipient_type="admin"

        )

        phone = phones[0] if phones else (get_setting(db, "messaging_admin_phone") or "").strip()

        if not phone:

            return None

        return ResolvedRecipient(phone=phone, name="الإدارة")

    if rt == "inventory_manager":

        phones = resolve_system_phones(

            db,

            event_key=event_key or str(payload.get("event_key") or ""),

            recipient_type="inventory_manager",

        )

        if not phones:

            return None

        return ResolvedRecipient(phone=phones[0], name="مخزون")

    if rt == "hr_manager":

        phones = resolve_system_phones(

            db,

            event_key=event_key or str(payload.get("event_key") or ""),

            recipient_type="hr_manager",

        )

        if not phones:

            return None

        return ResolvedRecipient(phone=phones[0], name="موارد بشرية")

    if rt == "supervisor":

        phones = resolve_system_phones(

            db,

            event_key=event_key or str(payload.get("event_key") or ""),

            recipient_type="supervisor",

        )

        phone = phones[0] if phones else (get_setting(db, "messaging_admin_phone") or "").strip()

        if not phone:

            return None

        return ResolvedRecipient(phone=phone, name="المشرف")

    if rt == "treasury_clerk":

        phones = resolve_system_phones(

            db,

            event_key=event_key or str(payload.get("event_key") or ""),

            recipient_type="treasury_clerk",

        )

        if not phones:

            return None

        return ResolvedRecipient(phone=phones[0], name="أمين الخزينة")

    if rt == "driver":

        phone = (payload.get("driver_phone") or payload.get("phone") or "").strip()

        name = (payload.get("driver_name") or "السائق").strip()

        if not phone and payload.get("driver_id"):

            from modules.delivery.models import DeliveryDriver



            d = db.get(DeliveryDriver, int(payload["driver_id"]))

            if d:

                phone = d.phone or ""

                name = d.name or name

        if not phone:

            return None

        return ResolvedRecipient(phone=phone, name=name)

    if rt == "maintenance_staff":
        phone = (payload.get("maintenance_phone") or payload.get("phone") or "").strip()
        name = (payload.get("maintenance_staff_name") or payload.get("name") or "").strip() or "الصيانة"
        eid = payload.get("employee_id")
        if eid:
            from modules.hr.models import Employee

            emp = db.get(Employee, int(eid))
            if emp is not None:
                phone = (emp.phone or phone).strip()
                name = (emp.full_name_ar or name).strip() or name
        if not phone:
            phone = (get_setting(db, "hotel_maintenance_phone") or "").strip()
        if not phone:
            return None
        from modules.messaging.phone_utils import normalize_whatsapp_phone

        phone = normalize_whatsapp_phone(phone)
        saved_name = (get_setting(db, "hotel_maintenance_name") or "").strip()
        if saved_name and (not name or name == "الصيانة"):
            name = saved_name
        return ResolvedRecipient(phone=phone, name=name)

    if rt in ("housekeeping_staff", "cleaning_staff"):
        phone = (payload.get("cleaning_phone") or payload.get("phone") or "").strip()
        name = (
            payload.get("cleaning_staff_name") or payload.get("name") or ""
        ).strip() or "التنظيف"
        eid = payload.get("employee_id")
        if eid:
            from modules.hr.models import Employee

            emp = db.get(Employee, int(eid))
            if emp is not None:
                phone = (emp.phone or phone).strip()
                name = (emp.full_name_ar or name).strip() or name
        if not phone:
            phone = (get_setting(db, "hotel_cleaning_phone") or "").strip()
        if not phone:
            return None
        from modules.messaging.phone_utils import normalize_whatsapp_phone

        phone = normalize_whatsapp_phone(phone)
        saved_name = (get_setting(db, "hotel_cleaning_name") or "").strip()
        if saved_name and (not name or name == "التنظيف"):
            name = saved_name
        return ResolvedRecipient(phone=phone, name=name)

    if rt == "hotel_shift_supervisor":
        phone = (
            (payload.get("supervisor_phone") or "").strip()
            or (get_setting(db, "hotel_shift_supervisor_phone") or "").strip()
        )
        if not phone:
            phones = resolve_system_phones(db, event_key=event_key, recipient_type="supervisor")
            phone = phones[0] if phones else ""
        if not phone:
            return None
        from modules.messaging.phone_utils import normalize_whatsapp_phone

        phone = normalize_whatsapp_phone(phone)
        return ResolvedRecipient(phone=phone, name="مسؤول الورديات")

    if rt == "referrer":
        cid = payload.get("referrer_customer_id") or payload.get("customer_id")
        phone = (payload.get("referrer_phone") or payload.get("phone") or "").strip()
        name = (payload.get("referrer_name") or payload.get("customer_name") or "").strip() or None
        if cid:
            cust = db.get(Customer, int(cid))
            if cust is None:
                return None
            if require_consent and not customer_can_receive(db, cust.id):
                return None
            phone = cust.phone or phone
            name = cust.name or name
            prof = get_profile(db, cust.id)
            tg = prof.telegram_chat_id if prof else None
            return ResolvedRecipient(
                phone=phone, name=name, customer_id=cust.id, telegram_chat_id=tg
            )
        if phone:
            return ResolvedRecipient(phone=phone, name=name)
        return None

    return None

