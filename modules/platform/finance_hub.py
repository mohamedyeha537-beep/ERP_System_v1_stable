"""مركز الربح — ملخص مالي لكل segment (مطعم / فندق)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from modules.authz.models import User
from modules.payments.daily_burden import compute_daily_burden
from modules.platform.business_domain import (
    BusinessDomain,
    ViewMode,
    domain_label,
    get_admin_view_mode,
    is_system_admin,
    view_mode_label,
)


@dataclass
class SegmentCard:
    domain: BusinessDomain
    label: str
    burden_daily: Decimal
    burden_monthly: Decimal
    break_even_daily: Decimal
    today_revenue: Decimal
    today_net: Decimal
    is_above_break_even: bool
    cm_ratio_pct: float
    coverage_pct: float
    revenue_source: str
    open_balance_count: int = 0
    open_balance_total: Decimal = Decimal("0")


def _segment_card(db: Session, domain: BusinessDomain) -> SegmentCard:
    b = compute_daily_burden(db, domain=domain)
    cm_pct = float(b.cm_ratio * 100) if b.cm_ratio else 0.0
    open_count = 0
    open_total = Decimal("0")
    if domain == BusinessDomain.HOTEL:
        from modules.hotel.bookings_report import hotel_open_balances

        _, ob = hotel_open_balances(db)
        open_count = ob.booking_count
        open_total = ob.balance_total
    return SegmentCard(
        domain=domain,
        label=domain_label(domain),
        burden_daily=b.grand_daily,
        burden_monthly=b.grand_monthly,
        break_even_daily=b.break_even_daily_revenue,
        today_revenue=b.today_revenue,
        today_net=b.today_net_position,
        is_above_break_even=b.is_above_break_even,
        cm_ratio_pct=round(cm_pct, 1),
        coverage_pct=round(b.coverage_pct, 1),
        revenue_source=getattr(b, "revenue_source", "pos"),
        open_balance_count=open_count,
        open_balance_total=open_total,
    )


def build_finance_hub_context(
    db: Session,
    user: User | None,
    session: dict | None,
) -> dict:
    view_mode = get_admin_view_mode(session) if is_system_admin(user) else ViewMode.GENERAL
    segments: list[SegmentCard] = []
    if is_system_admin(user):
        segments = [
            _segment_card(db, BusinessDomain.RESTAURANT),
            _segment_card(db, BusinessDomain.HOTEL),
        ]
    elif user is not None:
        from modules.platform.business_domain import (
            is_hotel_scope_user,
            is_restaurant_scope_user,
        )

        if is_hotel_scope_user(user):
            segments = [_segment_card(db, BusinessDomain.HOTEL)]
        elif is_restaurant_scope_user(user):
            segments = [_segment_card(db, BusinessDomain.RESTAURANT)]

    active = None
    if view_mode == ViewMode.RESTAURANT:
        active = BusinessDomain.RESTAURANT
    elif view_mode == ViewMode.HOTEL:
        active = BusinessDomain.HOTEL

    return {
        "view_mode": view_mode.value,
        "view_mode_label": view_mode_label(view_mode),
        "segments": segments,
        "active_domain": active,
        "can_switch_mode": is_system_admin(user),
    }
