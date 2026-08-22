"""وجهة إقفال الجلسة: خزينة أم تسليم للوردية التالية."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

CLOSE_DEST_TREASURY = "TREASURY"
CLOSE_DEST_NEXT_SHIFT = "NEXT_SHIFT"

SETTING_ALLOW_CARRY = "hotel_shift_allow_next_shift_carry"
SETTING_ALLOW_TREASURY = "hotel_shift_allow_treasury_close"
SETTING_POS_ALLOW_CARRY = "pos_shift_allow_next_shift_carry"
SETTING_POS_ALLOW_TREASURY = "pos_shift_allow_treasury_close"


class ShiftCarryError(ValueError):
    pass


@dataclass(frozen=True)
class ShiftClosePolicy:
    allow_carry: bool
    allow_treasury: bool

    @property
    def any_allowed(self) -> bool:
        return self.allow_carry or self.allow_treasury

    @property
    def choice_required(self) -> bool:
        return self.allow_carry and self.allow_treasury

    @property
    def forced_destination(self) -> str | None:
        if self.allow_carry and not self.allow_treasury:
            return CLOSE_DEST_NEXT_SHIFT
        if self.allow_treasury and not self.allow_carry:
            return CLOSE_DEST_TREASURY
        return None


def load_hotel_shift_close_policy(db: Session) -> ShiftClosePolicy:
    from modules.settings.service import get_bool

    return ShiftClosePolicy(
        allow_carry=get_bool(db, SETTING_ALLOW_CARRY, True),
        allow_treasury=get_bool(db, SETTING_ALLOW_TREASURY, True),
    )


def save_hotel_shift_close_policy(
    db: Session,
    *,
    allow_carry: bool,
    allow_treasury: bool,
) -> None:
    from modules.settings.service import set_setting

    set_setting(db, SETTING_ALLOW_CARRY, "1" if allow_carry else "0")
    set_setting(db, SETTING_ALLOW_TREASURY, "1" if allow_treasury else "0")


def parse_close_destination(
    raw: str | None,
    *,
    default: str | None = None,
) -> str:
    v = (raw or "").strip().upper()
    if not v:
        if default in (CLOSE_DEST_TREASURY, CLOSE_DEST_NEXT_SHIFT):
            return default
        raise ShiftCarryError("اختر: ترحيل للجلسة التالية أو إقفال عادي للخزينة.")
    if v not in (CLOSE_DEST_TREASURY, CLOSE_DEST_NEXT_SHIFT):
        raise ShiftCarryError("وجهة التسليم غير صالحة.")
    return v


def load_pos_shift_close_policy(db: Session) -> ShiftClosePolicy:
    from modules.settings.service import get_bool

    return ShiftClosePolicy(
        allow_carry=get_bool(db, SETTING_POS_ALLOW_CARRY, True),
        allow_treasury=get_bool(db, SETTING_POS_ALLOW_TREASURY, True),
    )


def save_pos_shift_close_policy(
    db: Session,
    *,
    allow_carry: bool,
    allow_treasury: bool,
) -> None:
    from modules.settings.service import set_setting

    set_setting(db, SETTING_POS_ALLOW_CARRY, "1" if allow_carry else "0")
    set_setting(db, SETTING_POS_ALLOW_TREASURY, "1" if allow_treasury else "0")


def resolve_close_destination(
    db: Session,
    raw: str | None,
    *,
    enforce_policy: bool = True,
    policy: ShiftClosePolicy | None = None,
) -> str:
    """يحترم إعدادات الأدمن: إتاحة الترحيل و/أو الإقفال العادي."""
    if not enforce_policy:
        return parse_close_destination(raw, default=CLOSE_DEST_TREASURY)
    policy = policy or load_hotel_shift_close_policy(db)
    if not policy.any_allowed:
        raise ShiftCarryError(
            "الإقفال غير متاح حالياً — فعّل الترحيل أو الإقفال العادي من إعدادات الأدمن."
        )
    forced = policy.forced_destination
    v = (raw or "").strip().upper()
    if forced:
        if v and v != forced:
            if v == CLOSE_DEST_NEXT_SHIFT and not policy.allow_carry:
                raise ShiftCarryError(
                    "ترحيل الرصيد للجلسة التالية غير متاح. استخدم الإقفال العادي."
                )
            if v == CLOSE_DEST_TREASURY and not policy.allow_treasury:
                raise ShiftCarryError(
                    "الإقفال العادي غير متاح. استخدم ترحيل الرصيد للجلسة التالية."
                )
        return forced
    return parse_close_destination(raw)


def is_treasury_destination(dest: str | None) -> bool:
    return (dest or CLOSE_DEST_TREASURY) == CLOSE_DEST_TREASURY


def is_next_shift_destination(dest: str | None) -> bool:
    return (dest or "") == CLOSE_DEST_NEXT_SHIFT


@dataclass(frozen=True)
class ShiftCarryOffer:
    shift_id: int
    cash: Decimal
    bank: Decimal
    cashier_label: str
    closed_at: datetime | None
    recipient_label: str = ""
    already_confirmed_cash: Decimal = Decimal("0")
    already_confirmed_bank: Decimal = Decimal("0")
    remainder_needs_count: bool = True


def money3(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.001"))
