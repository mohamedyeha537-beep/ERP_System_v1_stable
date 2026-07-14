from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from modules.notifications.events import (
    TREASURY_BALANCE_UPDATE,
    TREASURY_MOVEMENT,
    TREASURY_SHIFT_CLOSED,
)
from modules.notifications.service import emit_event_safe
from modules.payments.models import PaymentMethodDomain, PaymentMethodKind
from modules.settings.service import get_bool


def _fmt(value: Decimal | int | str | None) -> str:
    return str(Decimal(str(value or 0)).quantize(Decimal("0.001")))


def _enabled(db: Session, key: str) -> bool:
    return get_bool(db, "treasury_notifications_enabled", True) and get_bool(db, key, True)


def _global_enabled(db: Session) -> bool:
    return get_bool(db, "treasury_notifications_enabled", True)


def treasury_balances_payload(db: Session) -> dict[str, Any]:
    from modules.payments.service import wallet_breakdown

    totals: dict[str, Decimal] = {
        "cash_balance": Decimal("0"),
        "bank_balance": Decimal("0"),
        "restaurant_cash_balance": Decimal("0"),
        "restaurant_bank_balance": Decimal("0"),
        "hotel_cash_balance": Decimal("0"),
        "hotel_bank_balance": Decimal("0"),
        "shared_cash_balance": Decimal("0"),
        "shared_bank_balance": Decimal("0"),
    }
    for row in wallet_breakdown(db, None, None):
        kind = row.method.kind
        if kind not in (PaymentMethodKind.CASH, PaymentMethodKind.BANK):
            continue
        kind_key = "cash" if kind == PaymentMethodKind.CASH else "bank"
        bal = Decimal(str(row.net or 0)).quantize(Decimal("0.001"))
        totals[f"{kind_key}_balance"] += bal

        domain = getattr(row.method, "business_domain", None)
        if domain == PaymentMethodDomain.HOTEL or str(domain) == PaymentMethodDomain.HOTEL.value:
            totals[f"hotel_{kind_key}_balance"] += bal
        elif (
            domain == PaymentMethodDomain.RESTAURANT
            or str(domain) == PaymentMethodDomain.RESTAURANT.value
        ):
            totals[f"restaurant_{kind_key}_balance"] += bal
        else:
            totals[f"shared_{kind_key}_balance"] += bal

    return {k: _fmt(v) for k, v in totals.items()}


def emit_treasury_balance_update(
    db: Session,
    *,
    source_type: str = "treasury",
    source_id: int | None = None,
    reason: str = "",
) -> None:
    if not _enabled(db, "treasury_notify_balance_updates_enabled"):
        return
    emit_event_safe(
        db,
        event_key=TREASURY_BALANCE_UPDATE,
        source_type=source_type,
        source_id=source_id,
        payload={**treasury_balances_payload(db), "reason": reason},
    )


def emit_treasury_shift_closed(
    db: Session,
    *,
    shift_id: int,
    cashier_name: str,
    counted_cash: Decimal,
    expected_cash: Decimal,
    cash_difference: Decimal,
    counted_bank: Decimal,
    expected_bank: Decimal,
    bank_difference: Decimal,
) -> None:
    if not _global_enabled(db):
        return
    payload = {
        **treasury_balances_payload(db),
        "shift_id": shift_id,
        "cashier_name": cashier_name or "",
        "counted_cash": _fmt(counted_cash),
        "expected_cash": _fmt(expected_cash),
        "cash_difference": _fmt(cash_difference),
        "counted_bank": _fmt(counted_bank),
        "expected_bank": _fmt(expected_bank),
        "bank_difference": _fmt(bank_difference),
    }
    if get_bool(db, "treasury_notify_shift_close_enabled", True):
        emit_event_safe(
            db,
            event_key=TREASURY_SHIFT_CLOSED,
            source_type="pos_shift",
            source_id=shift_id,
            payload=payload,
        )
    emit_treasury_balance_update(
        db,
        source_type="pos_shift",
        source_id=shift_id,
        reason="shift_closed",
    )


def emit_treasury_movement(
    db: Session,
    *,
    source_type: str,
    source_id: int | None,
    movement_label: str,
    amount: Decimal,
    from_method: str = "",
    to_method: str = "",
    user_id: int | None = None,
) -> None:
    if not _global_enabled(db):
        return
    payload = {
        **treasury_balances_payload(db),
        "movement_label": movement_label,
        "amount": _fmt(amount),
        "from_method": from_method or "—",
        "to_method": to_method or "—",
        "user_id": user_id or "",
    }
    if get_bool(db, "treasury_notify_movements_enabled", True):
        emit_event_safe(
            db,
            event_key=TREASURY_MOVEMENT,
            source_type=source_type,
            source_id=source_id,
            payload=payload,
        )
    emit_treasury_balance_update(
        db,
        source_type=source_type,
        source_id=source_id,
        reason="treasury_movement",
    )
