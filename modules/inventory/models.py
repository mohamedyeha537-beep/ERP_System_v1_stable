from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class StockMovementType(str, enum.Enum):
    SALE = "SALE"
    SALE_RETURN = "SALE_RETURN"
    PURCHASE = "PURCHASE"
    ADJUSTMENT = "ADJUSTMENT"
    TRANSFER = "TRANSFER"
    ONLINE_SYNC = "ONLINE_SYNC"
    WASTE = "WASTE"
    SHORTAGE = "SHORTAGE"


class WarehouseTransferStatus(str, enum.Enum):
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class WarehouseTransferLineStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    MODIFY_REQUESTED = "MODIFY_REQUESTED"


class Warehouse(Base):
    """مخزن رئيسي أو فرعي — الرصيد يُحسب لكل (مخزن، صنف)."""

    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name_ar: Mapped[str] = mapped_column(String(120), unique=True)
    is_main: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    deduct_sales_enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class StockBalance(Base):
    __tablename__ = "stock_balances"

    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="CASCADE"), primary_key=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), primary_key=True
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))

    warehouse = relationship("Warehouse", foreign_keys=[warehouse_id])


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), index=True
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    movement_type: Mapped[StockMovementType] = mapped_column(Enum(StockMovementType))
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    sale_id: Mapped[int | None] = mapped_column(ForeignKey("sales.id", ondelete="SET NULL"), nullable=True)
    purchase_id: Mapped[int | None] = mapped_column(
        ForeignKey("purchases.id", ondelete="SET NULL"), nullable=True, index=True
    )
    counterparty_warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouses.id", ondelete="SET NULL"), nullable=True
    )
    transfer_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouse_transfers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reason_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    product = relationship("Product", foreign_keys=[product_id])
    warehouse = relationship("Warehouse", foreign_keys=[warehouse_id])
    lot_consumptions = relationship(
        "InventoryLotConsumption",
        back_populates="stock_movement",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class InventoryLotConsumption(Base):
    """استهلاك من دفعة — FIFO: يربط حركة المخزن بالدفعة وسعرها الفعلي."""

    __tablename__ = "inventory_lot_consumptions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    stock_movement_id: Mapped[int] = mapped_column(
        ForeignKey("stock_movements.id", ondelete="CASCADE"), index=True
    )
    inventory_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="CASCADE"), index=True
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    line_total: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    stock_movement = relationship("StockMovement", back_populates="lot_consumptions")
    lot = relationship("InventoryLot", back_populates="consumptions")


class WarehouseTransfer(Base):
    """مستند صرف من مخزن رئيسي إلى فرعي — يتطلب مصادقة المستلم."""

    __tablename__ = "warehouse_transfers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    from_warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), index=True
    )
    to_warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[WarehouseTransferStatus] = mapped_column(
        Enum(WarehouseTransferStatus, native_enum=False, length=24),
        default=WarehouseTransferStatus.PENDING,
        index=True,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    from_warehouse = relationship("Warehouse", foreign_keys=[from_warehouse_id])
    to_warehouse = relationship("Warehouse", foreign_keys=[to_warehouse_id])
    lines = relationship(
        "WarehouseTransferLine",
        back_populates="transfer",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class WarehouseTransferLine(Base):
    __tablename__ = "warehouse_transfer_lines"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    transfer_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_transfers.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    qty_sent: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    qty_received: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    line_status: Mapped[WarehouseTransferLineStatus] = mapped_column(
        Enum(WarehouseTransferLineStatus, native_enum=False, length=24),
        default=WarehouseTransferLineStatus.PENDING,
        index=True,
    )
    recipient_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    responded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    transfer = relationship("WarehouseTransfer", back_populates="lines")
    product = relationship("Product", foreign_keys=[product_id])


class InventoryLot(Base):
    """دفعة مخزون مرتبطة بفاتورة شراء — كل دفعة لها رقم وتاريخ صلاحية."""

    __tablename__ = "inventory_lots"
    __table_args__ = (UniqueConstraint("lot_code", name="uq_inventory_lots_lot_code"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    lot_code: Mapped[str] = mapped_column(String(32), index=True)
    purchase_id: Mapped[int] = mapped_column(
        ForeignKey("purchases.id", ondelete="CASCADE"), index=True
    )
    purchase_line_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_lines.id", ondelete="CASCADE"), unique=True, index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), index=True
    )
    production_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    qty_received: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    qty_remaining: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    product = relationship("Product", foreign_keys=[product_id])
    warehouse = relationship("Warehouse", foreign_keys=[warehouse_id])
    consumptions = relationship(
        "InventoryLotConsumption",
        back_populates="lot",
        cascade="all, delete-orphan",
        lazy="selectin",
    )