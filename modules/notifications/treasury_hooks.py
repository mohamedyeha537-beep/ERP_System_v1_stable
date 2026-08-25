from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from modules.notifications.events import (
    TREASURY_BALANCE_UPDATE,
    TREASURY_HANDOFF_APPROVED,
    TREASURY_HANDOFF_PENDING,
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


def _cashier_label_for_shift(sh: Any) -> str:
    emp = getattr(sh, "employee", None)
    if emp is not None and (getattr(emp, "full_name_ar", None) or "").strip():
        return str(emp.full_name_ar).strip()
    user = getattr(sh, "user", None)
    if user is not None and (getattr(user, "username", None) or "").strip():
        return str(user.username).strip()
    return f"جلسة #{sh.id}"


HANDOFF_PENDING_TICK_SETTING = "treasury_handoff_pending_last_tick_at"
HANDOFF_PENDING_TICK_SECONDS = 60 * 60  # تذكير مرة في الساعة كحد أقصى


def emit_treasury_handoff_pending(
    db: Session,
    *,
    shift_id: int,
    cashier_name: str = "",
    counted_cash: Decimal | str | None = None,
    counted_bank: Decimal | str | None = None,
    closed_at: str = "",
    pending_count: int | None = None,
    reminder_slot: str = "initial",
    shift_kind: str = "restaurant",
) -> None:
    """تنبيه أمين الخزينة والأدمن: جلسة مغلقة لم يُعتمد إيرادها بعد."""
    if not _global_enabled(db):
        return
    # معطّل افتراضياً — يُفعَّل يدوياً من الإعدادات عند الحاجة
    if not get_bool(db, "treasury_notify_handoff_pending_enabled", False):
        return
    from modules.payments.shift_handoff_service import count_shifts_pending_handoff

    count = pending_count if pending_count is not None else count_shifts_pending_handoff(db)
    kind = (shift_kind or "restaurant").strip().lower()
    if kind not in ("restaurant", "hotel"):
        kind = "restaurant"
    kind_ar = "فندق" if kind == "hotel" else "مطعم"
    employee = (cashier_name or "").strip() or "—"
    emit_event_safe(
        db,
        event_key=TREASURY_HANDOFF_PENDING,
        source_type="pos_shift" if kind == "restaurant" else "hotel_shift",
        source_id=shift_id,
        payload={
            "shift_id": shift_id,
            "cashier_name": employee,
            "employee_name": employee,
            "shift_kind": kind,
            "shift_kind_ar": kind_ar,
            "counted_cash": _fmt(counted_cash),
            "counted_bank": _fmt(counted_bank),
            "closed_at": closed_at or "—",
            "pending_count": str(count),
            "reminder_slot": (reminder_slot or "initial").strip() or "initial",
            "hub_detail": (
                f"جلسة {kind_ar} #{shift_id} · الموظف: {employee}"
                + (f" · أُغلقت: {closed_at}" if closed_at else "")
            ),
        },
    )


def emit_treasury_handoff_approved(
    db: Session,
    *,
    shift_id: int,
    cashier_name: str = "",
    claimed_cash: Decimal | str | None = None,
    claimed_bank: Decimal | str | None = None,
    shift_kind: str = "restaurant",
) -> None:
    if not _global_enabled(db):
        return
    kind = (shift_kind or "restaurant").strip().lower()
    if kind not in ("restaurant", "hotel"):
        kind = "restaurant"
    kind_ar = "فندق" if kind == "hotel" else "مطعم"
    employee = (cashier_name or "").strip() or "—"
    emit_event_safe(
        db,
        event_key=TREASURY_HANDOFF_APPROVED,
        source_type="pos_shift" if kind == "restaurant" else "hotel_shift",
        source_id=shift_id,
        payload={
            "shift_id": shift_id,
            "from_name": employee,
            "cashier_name": employee,
            "claimed_cash": _fmt(claimed_cash),
            "claimed_bank": _fmt(claimed_bank),
            "shift_kind": kind,
            "shift_kind_ar": kind_ar,
            "hub_detail": f"اعتماد خزينة جلسة {kind_ar} #{shift_id} · {employee}",
        },
    )


def scan_pending_treasury_handoffs(db: Session) -> int:
    """تذكير دوري (مرة/ساعة كحد أقصى) بالجلسات المغلقة دون اعتماد خزينة."""
    if not _global_enabled(db):
        return 0
    if not get_bool(db, "treasury_notify_handoff_pending_enabled", False):
        return 0
    from datetime import datetime, timezone

    from app.datetime_local import format_local_dt
    from modules.payments.shift_handoff_service import (
        count_shifts_pending_handoff,
        list_shifts_pending_handoff,
    )
    from modules.settings.service import get_setting, set_setting

    now = datetime.now(timezone.utc)
    raw = (get_setting(db, HANDOFF_PENDING_TICK_SETTING) or "").strip()
    if raw:
        try:
            last = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (now - last).total_seconds() < HANDOFF_PENDING_TICK_SECONDS:
                return 0
        except ValueError:
            pass

    pending = list_shifts_pending_handoff(db, limit=50)
    try:
        from modules.hotel.shift_handoff import (
            list_hotel_shifts_pending_handoff,
            count_hotel_shifts_pending_handoff,
        )

        hotel_pending = list_hotel_shifts_pending_handoff(db, limit=50)
        hotel_total = count_hotel_shifts_pending_handoff(db)
    except Exception:  # noqa: BLE001
        hotel_pending = []
        hotel_total = 0

    if not pending and not hotel_pending:
        set_setting(db, HANDOFF_PENDING_TICK_SETTING, now.isoformat())
        return 0
    total = count_shifts_pending_handoff(db) + hotel_total
    slot = now.strftime("%Y-%m-%dT%H")
    emitted = 0
    for sh in pending:
        closed = sh.closed_at
        if closed is not None:
            if closed.tzinfo is None:
                closed = closed.replace(tzinfo=timezone.utc)
            if (now - closed).total_seconds() < 55 * 60:
                continue
        emit_treasury_handoff_pending(
            db,
            shift_id=int(sh.id),
            cashier_name=_cashier_label_for_shift(sh),
            counted_cash=sh.counted_cash,
            counted_bank=sh.counted_bank,
            closed_at=format_local_dt(sh.closed_at, "%Y-%m-%d %H:%M") if sh.closed_at else "—",
            pending_count=total,
            reminder_slot=slot,
            shift_kind="restaurant",
        )
        emitted += 1
    for sh in hotel_pending:
        closed = sh.closed_at
        if closed is not None:
            if closed.tzinfo is None:
                closed = closed.replace(tzinfo=timezone.utc)
            if (now - closed).total_seconds() < 55 * 60:
                continue
        emit_treasury_handoff_pending(
            db,
            shift_id=int(sh.id),
            cashier_name=_cashier_label_for_shift(sh),
            counted_cash=sh.counted_cash,
            counted_bank=sh.counted_bank,
            closed_at=format_local_dt(sh.closed_at, "%Y-%m-%d %H:%M") if sh.closed_at else "—",
            pending_count=total,
            reminder_slot=slot,
            shift_kind="hotel",
        )
        emitted += 1
    set_setting(db, HANDOFF_PENDING_TICK_SETTING, now.isoformat())
    return emitted


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
