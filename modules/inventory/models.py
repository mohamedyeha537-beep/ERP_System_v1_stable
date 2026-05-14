from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class StockMovementType(str, enum.Enum):
    SALE = "SALE"
    SALE_RETURN = "SALE_RETURN"
    PURCHASE = "PURCHASE"
    ADJUSTMENT = "ADJUSTMENT"
    TRANSFER = "TRANSFER"
    ONLINE_SYNC = "ONLINE_SYNC"


class StockBalance(Base):
    __tablename__ = "stock_balances"

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), primary_key=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    movement_type: Mapped[StockMovementType] = mapped_column(Enum(StockMovementType))
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    sale_id: Mapped[int | None] = mapped_column(ForeignKey("sales.id", ondelete="SET NULL"), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    product = relationship("Product", foreign_keys=[product_id])
