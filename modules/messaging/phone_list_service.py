"""قوائم أرقام خارجية للحملات — بدون إنشاء عملاء."""
from __future__ import annotations

import csv
import io
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.messaging.models import MessageChannel, MessagePhoneList, MessagePhoneListEntry
from modules.messaging.outbox import enqueue_message
from modules.messaging.phone_utils import normalize_whatsapp_phone
from modules.settings.service import get_setting


class PhoneListError(Exception):
    pass


PHONE_LIST_CSV_TEMPLATE = """phone,name
0912345678,أحمد محمد
0923456789,فاطمة علي
218912345678,
# سطر يبدأ بـ # يُتجاهل
# العمود الأول: رقم الهاتف (محلي أو مع كود الدولة)
# العمود الثاني: الاسم اختياري — يُستخدم في التحية
"""

PHONE_LIST_TXT_TEMPLATE = """# رقم واحد في كل سطر، أو: رقم، اسم
0912345678, أحمد محمد
0923456789
218912345678; فاطمة علي
0911111111 أحمد
# الأسطر التي تبدأ بـ # تُتجاهل
"""


def phone_list_template_content(*, fmt: str = "csv", country_code: str = "218") -> tuple[str, str, str]:
    """محتوى قالب الاستيراد — (body, filename, media_type)."""
    cc = (country_code or "218").strip()
    if fmt == "txt":
        body = PHONE_LIST_TXT_TEMPLATE + f"\n# كود الدولة في الإعدادات: {cc}\n"
        return body, "phone_list_template.txt", "text/plain; charset=utf-8"
    body = PHONE_LIST_CSV_TEMPLATE + f"# country_code setting: {cc}\n"
    return body, "phone_list_template.csv", "text/csv; charset=utf-8"


def _parse_phone_rows(content: bytes, filename: str) -> list[tuple[str, str | None]]:
    text = content.decode("utf-8-sig", errors="replace")
    rows: list[tuple[str, str | None]] = []
    name = (filename or "").lower()
    if name.endswith(".csv"):
        reader = csv.reader(io.StringIO(text))
        for row in reader:
            if not row:
                continue
            phone_raw = (row[0] or "").strip()
            if not phone_raw or phone_raw.startswith("#"):
                continue
            if phone_raw.lower() in ("phone", "هاتف", "رقم", "mobile"):
                continue
            label = (row[1] or "").strip() if len(row) > 1 else None
            rows.append((phone_raw, label or None))
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "," in line:
                parts = [p.strip() for p in line.split(",", 1)]
                rows.append((parts[0], parts[1] if len(parts) > 1 and parts[1] else None))
            elif ";" in line:
                parts = [p.strip() for p in line.split(";", 1)]
                rows.append((parts[0], parts[1] if len(parts) > 1 and parts[1] else None))
            else:
                m = re.match(r"^(\S+)\s+(.+)$", line)
                if m:
                    rows.append((m.group(1), m.group(2).strip()))
                else:
                    rows.append((line, None))
    return rows


def import_phone_list(
    db: Session,
    *,
    name: str,
    content: bytes,
    filename: str,
    note: str | None = None,
    user_id: int | None = None,
) -> tuple[MessagePhoneList, dict[str, Any]]:
    title = (name or "").strip() or "قائمة أرقام"
    if not content:
        raise PhoneListError("الملف فارغ.")
    cc = (get_setting(db, "messaging_country_code", "218") or "218").strip()
    raw_rows = _parse_phone_rows(content, filename)
    if not raw_rows:
        raise PhoneListError("لم يُعثر على أرقام في الملف.")

    plist = MessagePhoneList(name=title[:160], note=(note or "").strip() or None, created_by_id=user_id)
    db.add(plist)
    db.flush()

    seen: set[str] = set()
    stats = {"total_lines": len(raw_rows), "imported": 0, "invalid": 0, "duplicates": 0}

    for phone_raw, label in raw_rows:
        try:
            phone = normalize_whatsapp_phone(phone_raw, country_code=cc)
        except Exception:
            phone = ""
        if not phone or len(phone) < 8:
            stats["invalid"] += 1
            db.add(
                MessagePhoneListEntry(
                    list_id=plist.id,
                    phone=(phone_raw or "")[:40],
                    display_name=(label or "")[:120] or None,
                    is_valid=False,
                    skip_reason="رقم غير صالح",
                )
            )
            continue
        if phone in seen:
            stats["duplicates"] += 1
            continue
        seen.add(phone)
        stats["imported"] += 1
        db.add(
            MessagePhoneListEntry(
                list_id=plist.id,
                phone=phone,
                display_name=(label or "")[:120] or None,
                is_valid=True,
            )
        )

    if stats["imported"] == 0:
        raise PhoneListError("لم تُستورد أي أرقام صالحة.")
    db.flush()
    return plist, stats


def list_phone_lists(db: Session, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(MessagePhoneList).order_by(MessagePhoneList.id.desc()).limit(limit)
        ).all()
    )
    out: list[dict[str, Any]] = []
    for plist in rows:
        valid = db.scalar(
            select(func.count())
            .select_from(MessagePhoneListEntry)
            .where(
                MessagePhoneListEntry.list_id == plist.id,
                MessagePhoneListEntry.is_valid.is_(True),
            )
        ) or 0
        out.append(
            {
                "id": plist.id,
                "name": plist.name,
                "note": plist.note,
                "valid_count": int(valid),
                "created_at": plist.created_at,
            }
        )
    return out


def enqueue_phone_list_messages(
    db: Session,
    *,
    list_id: int,
    message: str,
    image_url: str = "",
    event_type: str = "campaign.external",
    campaign_id: int | None = None,
) -> int:
    """جدولة رسائل لقائمة خارجية — customer_id=None دائماً."""
    from modules.messaging.categories import message_kind

    plist = db.get(MessagePhoneList, list_id)
    if plist is None:
        raise PhoneListError("قائمة الأرقام غير موجودة.")
    text = (message or "").strip()
    if not text and not (image_url or "").strip():
        raise PhoneListError("نص الرسالة مطلوب.")

    store = get_setting(db, "store_name", "نقطة البيع")
    meta_base: dict = {
        "kind": message_kind(event_type),
        "source": "phone_list",
        "phone_list_id": int(list_id),
        "external_recipient": True,
    }
    img = (image_url or "").strip()
    if img:
        meta_base["image_url"] = img

    entries = list(
        db.scalars(
            select(MessagePhoneListEntry).where(
                MessagePhoneListEntry.list_id == list_id,
                MessagePhoneListEntry.is_valid.is_(True),
            )
        ).all()
    )
    count = 0
    for entry in entries:
        who = (entry.display_name or "").strip() or "عميلنا"
        body = f"مرحباً {who}،\n{text}\n— {store}"
        meta = dict(meta_base)
        meta["phone_list_entry_id"] = int(entry.id)
        if entry.display_name:
            meta["display_name"] = entry.display_name
        enqueue_message(
            db,
            body=body,
            channel=MessageChannel.WHATSAPP.value,
            phone=entry.phone,
            customer_id=None,
            event_type=event_type,
            campaign_id=campaign_id,
            meta=meta,
        )
        count += 1
    return count
