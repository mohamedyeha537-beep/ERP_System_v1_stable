"""وسائط عرض الشقق في المتجر الأونلاين."""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class RoomMediaKind(str, enum.Enum):
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"


class HotelRoomMedia(Base):
    __tablename__ = "hotel_room_media"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_rooms.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[RoomMediaKind] = mapped_column(
        Enum(RoomMediaKind), default=RoomMediaKind.IMAGE, index=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    caption: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )

    room: Mapped["HotelRoom"] = relationship(back_populates="media_items")
