"""صلاحية ظهور خزائن الكاش/المصرف لكل مستخدم (يضبطها الأدمن)."""
from __future__ import annotations

from typing import Any, Iterable, TypeVar

from modules.payments.models import PaymentMethod, PaymentMethodKind
from modules.payments.service import payment_method_kind_matches

T = TypeVar("T")

# payment_method_id → (can_send, can_receive_hotel, can_receive_restaurant)
WalletAccessFlags = tuple[bool, bool, bool]


def user_may_use_pos_cash(user: Any | None) -> bool:
    if user is None:
        # سياقات نظام بلا مستخدم (طباعة/ترحيل) — لا تقييد
        return True
    try:
        return bool(getattr(user, "pos_show_cash", True))
    except Exception:
        return False


def user_may_use_pos_bank(user: Any | None) -> bool:
    if user is None:
        return True
    try:
        return bool(getattr(user, "pos_show_bank", True))
    except Exception:
        return False


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


def _row_recv_hotel(row) -> bool:
    if hasattr(row, "can_receive_hotel"):
        return bool(getattr(row, "can_receive_hotel", False))
    return bool(getattr(row, "can_receive", False))


def _row_recv_restaurant(row) -> bool:
    if hasattr(row, "can_receive_restaurant"):
        return bool(getattr(row, "can_receive_restaurant", False))
    return bool(getattr(row, "can_receive", False))


def load_user_wallet_access_map(db, user_id: int) -> dict[int, WalletAccessFlags]:
    """payment_method_id → (can_send, can_receive_hotel, can_receive_restaurant)."""
    from sqlalchemy import select

    from modules.authz.models import UserWalletAccess

    rows = db.scalars(
        select(UserWalletAccess).where(UserWalletAccess.user_id == int(user_id))
    ).all()
    return {
        int(r.payment_method_id): (
            bool(r.can_send),
            _row_recv_hotel(r),
            _row_recv_restaurant(r),
        )
        for r in rows
    }


def _normalize_mode_domain(domain: str | None) -> str | None:
    raw = (domain or "").strip().lower()
    if raw in ("hotel", "فندق"):
        return "hotel"
    if raw in ("restaurant", "مطعم", "resto", "pos"):
        return "restaurant"
    # BusinessDomain enum values
    if "hotel" in raw:
        return "hotel"
    if "restaurant" in raw or "resto" in raw:
        return "restaurant"
    return None


def user_custom_transfer_ids(
    db,
    user: Any | None,
    *,
    direction: str,
    domain: str | None = None,
) -> set[int] | None:
    """None = لا تخصيص (السلوك الافتراضي). وإلا مجموعة الحسابات المسموحة.

    direction:
      - send: خزائن الإرسال (من حساب)
      - receive: خزائن الاستقبال حسب وضع العمل (domain=hotel|restaurant)
    """
    if user is None or getattr(user, "id", None) is None:
        return None
    access = load_user_wallet_access_map(db, int(user.id))
    if not access:
        return None
    want_send = direction == "send"
    mode = _normalize_mode_domain(domain)
    out: set[int] = set()
    for pmid, (can_send, recv_hotel, recv_rest) in access.items():
        if want_send:
            if can_send:
                out.add(pmid)
            continue
        if mode == "hotel":
            if recv_hotel:
                out.add(pmid)
        elif mode == "restaurant":
            if recv_rest:
                out.add(pmid)
        else:
            if recv_hotel or recv_rest:
                out.add(pmid)
    return out


def filter_methods_by_user_transfer_access(
    db,
    user: Any | None,
    methods: Iterable[T],
    *,
    direction: str,
    domain: str | None = None,
) -> list[T]:
    """يضيّق قائمة الحسابات حسب تخصيص الإرسال/الاستقبال للمستخدم."""
    from modules.platform.business_domain import is_system_admin

    rows = list(methods)
    if user is None or is_system_admin(user):
        return rows
    allowed = user_custom_transfer_ids(
        db, user, direction=direction, domain=domain
    )
    return apply_custom_transfer_filter(rows, allowed)


def replace_user_wallet_access(
    db,
    user_id: int,
    *,
    send_ids: Iterable[int],
    recv_hotel_ids: Iterable[int] | None = None,
    recv_restaurant_ids: Iterable[int] | None = None,
    recv_ids: Iterable[int] | None = None,
) -> None:
    """يحفظ تخصيص التحويل.

    recv_hotel_ids = يظهر في «إلى» عند وضع الفندق
    recv_restaurant_ids = يظهر في «إلى» عند وضع المطعم
    recv_ids = توافق خلفي (يُطبَّق على الوضعين إن لم تُمرَّر القوائم الجديدة)
    """
    from sqlalchemy import delete

    from modules.authz.models import UserWalletAccess

    send_set = {int(x) for x in send_ids}
    if recv_hotel_ids is None and recv_restaurant_ids is None and recv_ids is not None:
        legacy = {int(x) for x in recv_ids}
        hotel_set = set(legacy)
        rest_set = set(legacy)
    else:
        hotel_set = {int(x) for x in (recv_hotel_ids or [])}
        rest_set = {int(x) for x in (recv_restaurant_ids or [])}

    db.execute(delete(UserWalletAccess).where(UserWalletAccess.user_id == int(user_id)))
    for pmid in sorted(send_set | hotel_set | rest_set):
        recv_h = pmid in hotel_set
        recv_r = pmid in rest_set
        db.add(
            UserWalletAccess(
                user_id=int(user_id),
                payment_method_id=pmid,
                can_send=pmid in send_set,
                can_receive=recv_h or recv_r,
                can_receive_hotel=recv_h,
                can_receive_restaurant=recv_r,
            )
        )
    db.flush()


def default_clerk_send_method_ids(db) -> set[int]:
    """حسابات الإرسال الافتراضية لأمين الخزينة: الخزينة الرئيسية كاش + مصرف فقط."""
    from modules.payments.shift_handoff_service import (
        ensure_main_treasury_payment_methods,
    )

    mains = ensure_main_treasury_payment_methods(db)
    return {
        int(mains["CASH"].id),
        int(mains["BANK"].id),
    }


def default_clerk_receive_method_ids(db, *, domain: str | None = None) -> set[int]:
    """وجهات الاستقبال الافتراضية = خزائن المجال المقابل (رئيسية كاش + مصرف).

    وضع فندق → خزينة المطعم الرئيسية.
    وضع مطعم → خزينة الفندق الرئيسية.
    """
    from modules.payments.service import ensure_hotel_treasury_payment_methods
    from modules.payments.shift_handoff_service import (
        ensure_main_treasury_payment_methods,
    )

    mode = _normalize_mode_domain(domain)
    if mode == "hotel":
        mains = ensure_main_treasury_payment_methods(db)
        return {int(mains["CASH"].id), int(mains["BANK"].id)}
    hotels = ensure_hotel_treasury_payment_methods(db)
    return {int(hotels["CASH"].id), int(hotels["BANK"].id)}


def apply_custom_transfer_filter(
    methods: Iterable[T], allowed_ids: set[int] | None
) -> list[T]:
    if allowed_ids is None:
        return list(methods)
    return [m for m in methods if int(getattr(m, "id", 0) or 0) in allowed_ids]
