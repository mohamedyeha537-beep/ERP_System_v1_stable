"""صلاحية ظهور خزائن الكاش/المصرف لكل مستخدم (يضبطها الأدمن)."""
from __future__ import annotations

from typing import Any, Iterable, TypeVar

from modules.payments.models import PaymentMethod, PaymentMethodKind
from modules.payments.service import payment_method_kind_matches

T = TypeVar("T")


def user_may_use_pos_cash(user: Any | None) -> bool:
    if user is None:
        return True
    try:
        return bool(getattr(user, "pos_show_cash", True))
    except Exception:
        return True


def user_may_use_pos_bank(user: Any | None) -> bool:
    if user is None:
        return True
    try:
        return bool(getattr(user, "pos_show_bank", True))
    except Exception:
        return True


def user_may_use_payment_method_kind(user: Any | None, kind: PaymentMethodKind | str) -> bool:
    key = kind.value if isinstance(kind, PaymentMethodKind) else str(kind or "").strip().upper()
    if key == PaymentMethodKind.CASH.value or key == "CASH":
        return user_may_use_pos_cash(user)
    if key == PaymentMethodKind.BANK.value or key == "BANK":
        return user_may_use_pos_bank(user)
    return True


def user_may_use_payment_method(user: Any | None, pm: PaymentMethod | None) -> bool:
    if pm is None:
        return False
    if payment_method_kind_matches(pm, PaymentMethodKind.CASH):
        return user_may_use_pos_cash(user)
    if payment_method_kind_matches(pm, PaymentMethodKind.BANK):
        return user_may_use_pos_bank(user)
    return True


def assert_user_may_use_payment_method(user: Any | None, pm: PaymentMethod) -> None:
    from modules.payments.service import PaymentsError

    if user_may_use_payment_method(user, pm):
        return
    if payment_method_kind_matches(pm, PaymentMethodKind.CASH):
        raise PaymentsError("خزينة الكاش غير مفعّلة لحسابك — راجع الإدارة.")
    if payment_method_kind_matches(pm, PaymentMethodKind.BANK):
        raise PaymentsError("خزينة المصرف غير مفعّلة لحسابك — راجع الإدارة.")
    raise PaymentsError(f"الحساب «{pm.name_ar}» غير مسموح لحسابك.")


def filter_payment_methods_for_user(
    methods: Iterable[T], user: Any | None
) -> list[T]:
    if user is None:
        return list(methods)
    out: list[T] = []
    for m in methods:
        if user_may_use_payment_method(user, m):  # type: ignore[arg-type]
            out.append(m)
    return out


def load_user_wallet_access_map(db, user_id: int) -> dict[int, tuple[bool, bool]]:
    """payment_method_id → (can_send, can_receive)."""
    from sqlalchemy import select

    from modules.authz.models import UserWalletAccess

    rows = db.scalars(
        select(UserWalletAccess).where(UserWalletAccess.user_id == int(user_id))
    ).all()
    return {
        int(r.payment_method_id): (bool(r.can_send), bool(r.can_receive)) for r in rows
    }


def user_custom_transfer_ids(
    db, user: Any | None, *, direction: str
) -> set[int] | None:
    """None = لا تخصيص (السلوك الافتراضي). وإلا مجموعة الحسابات المسموحة."""
    if user is None or getattr(user, "id", None) is None:
        return None
    access = load_user_wallet_access_map(db, int(user.id))
    if not access:
        return None
    want_send = direction == "send"
    out = {
        pmid
        for pmid, (can_send, can_recv) in access.items()
        if (can_send if want_send else can_recv)
    }
    return out or None


def replace_user_wallet_access(
    db,
    user_id: int,
    *,
    send_ids: Iterable[int],
    recv_ids: Iterable[int],
) -> None:
    from sqlalchemy import delete

    from modules.authz.models import UserWalletAccess

    send_set = {int(x) for x in send_ids}
    recv_set = {int(x) for x in recv_ids}
    db.execute(delete(UserWalletAccess).where(UserWalletAccess.user_id == int(user_id)))
    for pmid in sorted(send_set | recv_set):
        db.add(
            UserWalletAccess(
                user_id=int(user_id),
                payment_method_id=pmid,
                can_send=pmid in send_set,
                can_receive=pmid in recv_set,
            )
        )
    db.flush()


def default_clerk_send_method_ids(db) -> set[int]:
    """حسابات التحويل الافتراضية لأمين الخزينة: خزينة المطعم + خزينة الفندق."""
    from modules.payments.service import ensure_hotel_treasury_payment_methods
    from modules.payments.shift_handoff_service import (
        ensure_main_treasury_payment_methods,
    )

    hotels = ensure_hotel_treasury_payment_methods(db)
    mains = ensure_main_treasury_payment_methods(db)
    return {
        int(hotels["CASH"].id),
        int(hotels["BANK"].id),
        int(mains["CASH"].id),
        int(mains["BANK"].id),
    }


def apply_custom_transfer_filter(
    methods: Iterable[T], allowed_ids: set[int] | None
) -> list[T]:
    if allowed_ids is None:
        return list(methods)
    return [m for m in methods if int(getattr(m, "id", 0) or 0) in allowed_ids]
