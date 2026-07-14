"""كود تفويض الاسترداد من نقطة البيع (منفصل عن صلاحية المستخدم)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from modules.authz.service import hash_password, verify_password
from modules.settings.service import get_setting, set_setting

REFUND_AUTH_HASH_KEY = "refund_authorization_code_hash"


class RefundAuthError(ValueError):
    pass


def validate_refund_code(plain: str) -> str:
    code = (plain or "").strip()
    if len(code) < 4 or len(code) > 12:
        raise RefundAuthError("كود الاسترداد يجب أن يكون بين 4 و12 حرفاً.")
    return code


def hash_refund_code(plain: str) -> str:
    return hash_password(validate_refund_code(plain))


def verify_refund_code(plain: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        validate_refund_code(plain)
    except RefundAuthError:
        return False
    return verify_password(plain, hashed)


def refund_auth_configured(db: Session) -> bool:
    return bool((get_setting(db, REFUND_AUTH_HASH_KEY, "") or "").strip())


def set_refund_authorization_code(db: Session, *, plain: str | None, clear: bool = False) -> None:
    if clear or not (plain or "").strip():
        set_setting(db, REFUND_AUTH_HASH_KEY, "")
        return
    set_setting(db, REFUND_AUTH_HASH_KEY, hash_refund_code(plain or ""))


def check_refund_authorization(db: Session, plain: str) -> None:
    hashed = (get_setting(db, REFUND_AUTH_HASH_KEY, "") or "").strip()
    if not hashed:
        raise RefundAuthError(
            "لم يُضبط كود الاسترداد بعد. من إعدادات النظام → كود تفويض الاسترداد."
        )
    if not verify_refund_code(plain, hashed):
        raise RefundAuthError("كود الاسترداد غير صحيح.")
