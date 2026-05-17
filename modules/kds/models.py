from __future__ import annotations

import enum

from sqlalchemy import Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base
from modules.catalog.models import CategoryRouting


class KitchenVenue(str, enum.Enum):
    """منطقة التشغيل — المطبخ يمكن تقسيمه لأقسام؛ المقهى يبقى كما هو عادة."""

    KITCHEN = "KITCHEN"
    CAFE = "CAFE"


class KitchenDepartment(Base):
    """قسم تجهيز في المطبخ/المقهى — لا يظهر في نقطة البيع.

    يُربط كل منتج بقسم واحد (اختياري). عند الإرسال للمطبخ تُنشأ تذكرة لكل قسم
    ببنوده فقط، بالإضافة لتذكرة مجمّعة للتجميع والتقديم.
    """

    __tablename__ = "kitchen_departments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name_ar: Mapped[str] = mapped_column(String(120))
    venue: Mapped[KitchenVenue] = mapped_column(
        Enum(KitchenVenue), default=KitchenVenue.KITCHEN, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(default=True)
    routing_mode: Mapped[CategoryRouting] = mapped_column(
        Enum(CategoryRouting), default=CategoryRouting.SCREEN
    )
    routing_target: Mapped[str | None] = mapped_column(String(255), nullable=True)

    products: Mapped[list["Product"]] = relationship(  # noqa: F821
        back_populates="kitchen_department",
        foreign_keys="Product.kitchen_department_id",
    )
