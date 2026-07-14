"""تقارير صرف نقاط الولاء حسب جلسة الكاشier — تكلفة/خصم غير نقدي."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from modules.authz.models import User
from modules.customers.models import Customer, LoyaltyTransaction, LoyaltyTxnKind
from modules.customers.service import loyalty_settings, points_to_dinars
from modules.sales.models import Sale


@dataclass
class ShiftLoyaltyAgg:
    redeem_count: int = 0
    points_redeemed: Decimal = Decimal("0")
    dinar_cost: Decimal = Decimal("0")


@dataclass
class ShiftLoyaltyUserRow:
    user_id: int | None
    user_label: str
    redeem_count: int
    points_redeemed: Decimal
    dinar_cost: Decimal


@dataclass
class ShiftLoyaltyTxnRow:
    txn_id: int
    sale_id: int
    customer_id: int
    customer_name: str
    customer_phone: str
    points_redeemed: Decimal
    dinar_value: Decimal
    cashier_user_id: int | None
    cashier_label: str
    created_at: datetime | None
    note: str | None


@dataclass
class ShiftLoyaltyReport:
    shift_id: int
    redeem_count: int
    points_redeemed: Decimal
    dinar_cost: Decimal
    redeem_value_per_point: Decimal
    by_user: list[ShiftLoyaltyUserRow]
    rows: list[ShiftLoyaltyTxnRow]


@dataclass
class LoyaltyRedeemPeriodSummary:
    """إجمالي صرف نقاط الولاء في فترة — تكلفة تسويق تُخصم من صافي الربح."""

    redeem_count: int = 0
    points_redeemed: Decimal = Decimal("0")
    dinar_cost: Decimal = Decimal("0")


def loyalty_redeem_cost_in_period(
    db: Session, start: datetime, end: datetime
) -> LoyaltyRedeemPeriodSummary:
    from datetime import timezone

    s0 = start.astimezone(timezone.utc)
    s1 = end.astimezone(timezone.utc)
    cnt, pts_sum = db.execute(
        select(
            func.count(LoyaltyTransaction.id),
            func.coalesce(func.sum(LoyaltyTransaction.points), 0),
        ).where(
            LoyaltyTransaction.kind == LoyaltyTxnKind.REDEEM,
            LoyaltyTransaction.created_at >= s0,
            LoyaltyTransaction.created_at < s1,
        )
    ).one()
    pts = _abs_points(Decimal(str(pts_sum or 0)))
    return LoyaltyRedeemPeriodSummary(
        redeem_count=int(cnt or 0),
        points_redeemed=pts,
        dinar_cost=points_to_dinars(db, pts),
    )


def _abs_points(raw: Decimal) -> Decimal:
    return abs(Decimal(str(raw or 0))).quantize(Decimal("0.001"))


def loyalty_redeem_agg_for_shifts(
    db: Session, shift_ids: list[int]
) -> dict[int, ShiftLoyaltyAgg]:
    if not shift_ids:
        return {}
    rpp = loyalty_settings(db)["redeem_value_per_point"]
    rows = db.execute(
        select(
            Sale.pos_shift_id.label("shift_id"),
            func.count(LoyaltyTransaction.id).label("cnt"),
            func.coalesce(func.sum(LoyaltyTransaction.points), 0).label("pts_sum"),
        )
        .select_from(LoyaltyTransaction)
        .join(Sale, Sale.id == LoyaltyTransaction.sale_id)
        .where(
            Sale.pos_shift_id.in_(shift_ids),
            LoyaltyTransaction.kind == LoyaltyTxnKind.REDEEM,
        )
        .group_by(Sale.pos_shift_id)
    ).all()
    out: dict[int, ShiftLoyaltyAgg] = {}
    for r in rows:
        sid = int(r.shift_id)
        pts = _abs_points(Decimal(str(r.pts_sum or 0)))
        out[sid] = ShiftLoyaltyAgg(
            redeem_count=int(r.cnt or 0),
            points_redeemed=pts,
            dinar_cost=(pts * rpp).quantize(Decimal("0.001")),
        )
    return out


def loyalty_redeem_summary_for_shift(db: Session, shift_id: int) -> ShiftLoyaltyAgg:
    return loyalty_redeem_agg_for_shifts(db, [shift_id]).get(
        shift_id, ShiftLoyaltyAgg()
    )


def build_shift_loyalty_report(db: Session, shift_id: int) -> ShiftLoyaltyReport:
    rpp = loyalty_settings(db)["redeem_value_per_point"]
    txns = list(
        db.scalars(
            select(LoyaltyTransaction)
            .join(Sale, Sale.id == LoyaltyTransaction.sale_id)
            .where(
                Sale.pos_shift_id == shift_id,
                LoyaltyTransaction.kind == LoyaltyTxnKind.REDEEM,
            )
            .options(
                selectinload(LoyaltyTransaction.customer),
            )
            .order_by(LoyaltyTransaction.id.desc())
        ).all()
    )
    if not txns:
        return ShiftLoyaltyReport(
            shift_id=shift_id,
            redeem_count=0,
            points_redeemed=Decimal("0"),
            dinar_cost=Decimal("0"),
            redeem_value_per_point=rpp,
            by_user=[],
            rows=[],
        )

    sale_ids = {t.sale_id for t in txns if t.sale_id}
    sales_by_id: dict[int, Sale] = {}
    if sale_ids:
        sales_by_id = {
            s.id: s
            for s in db.scalars(
                select(Sale).where(Sale.id.in_(sale_ids))
            ).all()
        }

    user_ids = {
        s.created_by_id for s in sales_by_id.values() if s.created_by_id is not None
    }
    users_by_id: dict[int, User] = {}
    if user_ids:
        users_by_id = {
            u.id: u
            for u in db.scalars(select(User).where(User.id.in_(user_ids))).all()
        }

    detail_rows: list[ShiftLoyaltyTxnRow] = []
    by_user_map: dict[int | None, ShiftLoyaltyUserRow] = {}

    for t in txns:
        pts = _abs_points(t.points)
        dinar = points_to_dinars(db, pts)
        sale = sales_by_id.get(t.sale_id) if t.sale_id else None
        uid = sale.created_by_id if sale else None
        u = users_by_id.get(uid) if uid else None
        cashier_label = u.username if u else "—"
        cust = t.customer
        detail_rows.append(
            ShiftLoyaltyTxnRow(
                txn_id=t.id,
                sale_id=int(t.sale_id or 0),
                customer_id=int(t.customer_id),
                customer_name=(cust.name or cust.phone or f"#{cust.id}") if cust else "—",
                customer_phone=(cust.phone or "") if cust else "",
                points_redeemed=pts,
                dinar_value=dinar,
                cashier_user_id=uid,
                cashier_label=cashier_label,
                created_at=t.created_at,
                note=t.note,
            )
        )
        bucket = by_user_map.get(uid)
        if bucket is None:
            bucket = ShiftLoyaltyUserRow(
                user_id=uid,
                user_label=cashier_label,
                redeem_count=0,
                points_redeemed=Decimal("0"),
                dinar_cost=Decimal("0"),
            )
            by_user_map[uid] = bucket
        bucket.redeem_count += 1
        bucket.points_redeemed = (bucket.points_redeemed + pts).quantize(Decimal("0.001"))
        bucket.dinar_cost = (bucket.dinar_cost + dinar).quantize(Decimal("0.001"))

    total_pts = sum((r.points_redeemed for r in detail_rows), Decimal("0")).quantize(
        Decimal("0.001")
    )
    total_dinar = sum((r.dinar_value for r in detail_rows), Decimal("0")).quantize(
        Decimal("0.001")
    )
    by_user = sorted(
        by_user_map.values(),
        key=lambda x: (-x.dinar_cost, x.user_label),
    )
    return ShiftLoyaltyReport(
        shift_id=shift_id,
        redeem_count=len(detail_rows),
        points_redeemed=total_pts,
        dinar_cost=total_dinar,
        redeem_value_per_point=rpp,
        by_user=by_user,
        rows=detail_rows,
    )
