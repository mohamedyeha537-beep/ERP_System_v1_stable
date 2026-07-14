from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class PrinterConnectionType(str, enum.Enum):
    LOCAL_AGENT = "local_agent"
    PRINTNODE = "printnode"
    DIRECT_TCP = "direct_tcp"
    BROWSER = "browser"


class PrintJobType(str, enum.Enum):
    KITCHEN_TICKET = "kitchen_ticket"
    RECEIPT = "receipt"
    TEST = "test"


class PrintJobStatus(str, enum.Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    PRINTED = "printed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PayloadFormat(str, enum.Enum):
    TEXT = "text"
    ESCPOS_TEXT = "escpos_text"
    JSON = "json"


class PrintAgent(Base):
    __tablename__ = "print_agents"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))
    branch_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    printers: Mapped[list["Printer"]] = relationship(back_populates="agent")


class Printer(Base):
    __tablename__ = "printers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    connection_type: Mapped[PrinterConnectionType] = mapped_column(
        String(32), default=PrinterConnectionType.LOCAL_AGENT
    )
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("print_agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    local_printer_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    port: Mapped[int] = mapped_column(Integer, default=9100)
    paper_width: Mapped[int] = mapped_column(Integer, default=80)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    agent: Mapped[PrintAgent | None] = relationship(back_populates="printers")
    kitchen_sections: Mapped[list["KitchenSection"]] = relationship(back_populates="printer")


class KitchenSection(Base):
    __tablename__ = "kitchen_sections"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    printer_id: Mapped[int | None] = mapped_column(
        ForeignKey("printers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    printer: Mapped[Printer | None] = relationship(back_populates="kitchen_sections")


class PrintJob(Base):
    __tablename__ = "print_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    printer_id: Mapped[int] = mapped_column(
        ForeignKey("printers.id", ondelete="CASCADE"), index=True
    )
    kitchen_ticket_id: Mapped[int | None] = mapped_column(
        ForeignKey("kitchen_tickets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    job_type: Mapped[str] = mapped_column(String(32), default=PrintJobType.KITCHEN_TICKET.value)
    status: Mapped[str] = mapped_column(
        String(20), default=PrintJobStatus.PENDING.value, index=True
    )
    payload_format: Mapped[str] = mapped_column(String(20), default=PayloadFormat.TEXT.value)
    payload: Mapped[str] = mapped_column(Text().with_variant(mysql.LONGTEXT, "mysql"))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claimed_by_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("print_agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    printed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    printer: Mapped[Printer] = relationship(foreign_keys=[printer_id])
    kitchen_ticket: Mapped["KitchenTicket | None"] = relationship(  # noqa: F821
        foreign_keys=[kitchen_ticket_id]
    )
    claimed_by_agent: Mapped[PrintAgent | None] = relationship(
        foreign_keys=[claimed_by_agent_id]
    )
