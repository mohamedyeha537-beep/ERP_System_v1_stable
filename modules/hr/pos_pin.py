"""رقم سري نقطة البيع (4 أرقام) للموظفين."""
from __future__ import annotations

import re

from modules.authz.service import hash_password, verify_password

_PIN_RE = re.compile(r"^\d{4}$")


def validate_pos_pin(plain: str) -> str:
    pin = (plain or "").strip()
    if not _PIN_RE.match(pin):
        raise ValueError("الرقم السري يجب أن يكون 4 أرقام فقط.")
    return pin


def hash_pos_pin(plain: str) -> str:
    return hash_password(validate_pos_pin(plain))


def verify_pos_pin(plain: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        validate_pos_pin(plain)
    except ValueError:
        return False
    return verify_password(plain, hashed)
