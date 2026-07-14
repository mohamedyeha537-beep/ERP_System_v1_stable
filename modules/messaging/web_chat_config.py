"""إعدادات محادثة الويب — اسم البوت، الترحيب، وقائمة الخيارات."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from modules.messaging.models import WebChatDepartment
from modules.settings.service import get_setting

_ARABIC_DIGITS = {"1": "١", "2": "٢", "3": "٣", "4": "٤", "5": "٥"}


@dataclass(frozen=True)
class WebChatMenuOption:
    num: int
    label: str
    dept: str


def web_chat_bot_name(db: Session) -> str:
    name = (get_setting(db, "web_chat_bot_name", "مساعد") or "مساعد").strip()
    return name or "مساعد"


def _valid_dept(code: str) -> str:
    c = (code or "").strip().lower()
    if c in ("sales", WebChatDepartment.SALES.value):
        return WebChatDepartment.SALES.value
    if c in ("support", WebChatDepartment.SUPPORT.value):
        return WebChatDepartment.SUPPORT.value
    if c in ("human", WebChatDepartment.HUMAN.value):
        return WebChatDepartment.HUMAN.value
    return WebChatDepartment.SALES.value


def _parse_menu_raw(raw: str) -> list[WebChatMenuOption]:
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        out: list[WebChatMenuOption] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            try:
                num = int(row.get("num") or row.get("number") or 0)
            except (TypeError, ValueError):
                continue
            label = (row.get("label") or row.get("label_ar") or "").strip()
            if num < 1 or not label:
                continue
            out.append(
                WebChatMenuOption(
                    num=num,
                    label=label,
                    dept=_valid_dept(str(row.get("dept") or row.get("department") or "sales")),
                )
            )
        return sorted(out, key=lambda x: x.num)

    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in re.split(r"[|=]", line, maxsplit=2)]
        if len(parts) < 2:
            continue
        try:
            num = int(parts[0])
        except ValueError:
            continue
        label = parts[1]
        dept = _valid_dept(parts[2] if len(parts) > 2 else "sales")
        if label:
            out.append(WebChatMenuOption(num=num, label=label, dept=dept))
    return sorted(out, key=lambda x: x.num)


def web_chat_menu_options(db: Session) -> list[WebChatMenuOption]:
    parsed = _parse_menu_raw(get_setting(db, "web_chat_menu_options", "") or "")
    if parsed:
        return parsed
    return [
        WebChatMenuOption(
            num=1,
            label="المبيعات والطلبات",
            dept=WebChatDepartment.SALES.value,
        )
    ]


def web_chat_menu_options_display(db: Session) -> str:
    """صيغة سهلة للتحرير في لوحة التحكم."""
    raw = (get_setting(db, "web_chat_menu_options", "") or "").strip()
    if raw and not raw.startswith("["):
        return raw
    lines = [
        f"{opt.num}|{opt.label}|{opt.dept}" for opt in web_chat_menu_options(db)
    ]
    return "\n".join(lines)


def normalize_menu_options_input(raw: str) -> str:
    """يحفظ القائمة بصيغة سطرية موحّدة."""
    parsed = _parse_menu_raw(raw)
    if not parsed:
        parsed = [
            WebChatMenuOption(
                num=1,
                label="المبيعات والطلبات",
                dept=WebChatDepartment.SALES.value,
            )
        ]
    return "\n".join(f"{opt.num}|{opt.label}|{opt.dept}" for opt in parsed)


def format_menu_block(db: Session) -> str:
    lines = []
    for opt in web_chat_menu_options(db):
        lines.append(f"{opt.num} — {opt.label}")
    return "\n".join(lines)


def department_for_choice(db: Session, text: str) -> str | None:
    t = (text or "").strip()
    if not t:
        return None
    for opt in web_chat_menu_options(db):
        nums = {str(opt.num), _ARABIC_DIGITS.get(str(opt.num), "")}
        if t in nums:
            return opt.dept
    return None


def build_web_chat_greeting(db: Session) -> str:
    store = (get_setting(db, "store_name", "مطعم ومقهى روف") or "مطعم ومقهى روف").strip()
    bot = web_chat_bot_name(db)
    menu = format_menu_block(db)
    template = (get_setting(db, "web_chat_greeting_text", "") or "").strip()
    if template:
        body = (
            template.replace("{bot_name}", bot)
            .replace("{store_name}", store)
            .replace("{menu}", menu)
        )
        return body.replace("**", "")

    return (
        f"👋 مرحباً، أنا {bot} من {store}!\n"
        "أنا جاهز لمساعدتك في إتمام طلبيتك.\n\n"
        "اختر من القائمة ما يناسبك:\n"
        f"{menu}"
    ).replace("**", "")


def web_chat_config_revision(db: Session) -> str:
    """بصمة إعدادات الترحيب — تتغيّر عند أي تعديل في لوحة التحكم."""
    payload = "|".join(
        [
            web_chat_bot_name(db),
            (get_setting(db, "web_chat_greeting_text", "") or "").strip(),
            normalize_menu_options_input(
                get_setting(db, "web_chat_menu_options", "") or ""
            ),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
