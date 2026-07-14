"""تهيئة خطط الأسعار الافتراضية."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.hotel.booking_models import HotelPricingRule, HotelRatePlan, HotelRoomType


def ensure_default_rate_plans(db: Session) -> None:
    if db.scalar(select(func.count()).select_from(HotelRatePlan)):
        return
    for rt in db.scalars(select(HotelRoomType)).all():
        plan = HotelRatePlan(
            property_id=rt.property_id,
            room_type_id=rt.id,
            name_ar=f"أساسي — {rt.name_ar}",
            base_price=rt.base_price,
            is_active=True,
        )
        db.add(plan)
        db.flush()
        # Friday (4) and Saturday (5) weekend premium in Libya context
        for dow, mult in ((4, Decimal("1.15")), (5, Decimal("1.15"))):
            db.add(
                HotelPricingRule(
                    rate_plan_id=plan.id,
                    day_of_week=dow,
                    price=(Decimal(str(rt.base_price)) * mult).quantize(Decimal("0.001")),
                    is_active=True,
                )
            )
    db.flush()
