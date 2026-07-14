"""صرف مخزني مع مصادقة المستلم — إنشاء، موافقة، رفض، طلب تعديل."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from modules.inventory.models import (
    StockMovementType,
    Warehouse,
    WarehouseTransfer,
    WarehouseTransferLine,
    WarehouseTransferLineStatus,
    WarehouseTransferStatus,
)
from modules.inventory.service import (
    InsufficientStock,
    WarehouseError,
    apply_movement,
    get_main_warehouse,
)


class TransferError(Exception):
    pass


def _refresh_transfer_status(db: Session, transfer: WarehouseTransfer) -> None:
    lines = list(transfer.lines)
    if not lines:
        transfer.status = WarehouseTransferStatus.CANCELLED
        transfer.closed_at = datetime.now(timezone.utc)
        return
    pending = {WarehouseTransferLineStatus.PENDING, WarehouseTransferLineStatus.MODIFY_REQUESTED}
    if all(ln.line_status == WarehouseTransferLineStatus.APPROVED for ln in lines):
        transfer.status = WarehouseTransferStatus.COMPLETED
        transfer.closed_at = datetime.now(timezone.utc)
    elif all(
        ln.line_status
        in (
            WarehouseTransferLineStatus.APPROVED,
            WarehouseTransferLineStatus.REJECTED,
        )
        for ln in lines
    ):
        transfer.status = WarehouseTransferStatus.COMPLETED
        transfer.closed_at = datetime.now(timezone.utc)
    elif any(ln.line_status in pending for ln in lines):
        transfer.status = (
            WarehouseTransferStatus.PARTIAL
            if any(ln.line_status == WarehouseTransferLineStatus.APPROVED for ln in lines)
            else WarehouseTransferStatus.PENDING
        )
        transfer.closed_at = None
    db.flush()


def create_transfer_request(
    db: Session,
    *,
    to_warehouse_id: int,
    lines: list[tuple[int, Decimal]],
    user_id: int | None,
    note: str | None = None,
) -> WarehouseTransfer:
    """صرف من الرئيسي — يُخصم فوراً وينتظر مصادقة المستلم لإضافته للفرعي."""
    main = get_main_warehouse(db)
    return create_transfer_request_between(
        db,
        from_warehouse_id=main.id,
        to_warehouse_id=to_warehouse_id,
        lines=lines,
        user_id=user_id,
        note=note,
    )


def create_transfer_request_between(
    db: Session,
    *,
    from_warehouse_id: int,
    to_warehouse_id: int,
    lines: list[tuple[int, Decimal]],
    user_id: int | None,
    note: str | None = None,
) -> WarehouseTransfer:
    """صرف من أي مخزن إلى أي مخزن آخر — الداخل ينتظر مصادقة المستلم."""
    source = db.get(Warehouse, from_warehouse_id)
    dest = db.get(Warehouse, to_warehouse_id)
    if source is None or not source.is_active:
        raise WarehouseError("مخزن المصدر غير صالح.")
    if dest is None or not dest.is_active:
        raise WarehouseError("مخزن الوجهة غير صالح.")
    if source.id == dest.id:
        raise WarehouseError("اختر مخزناً مختلفاً كوجهة للنقل.")
    if not lines:
        raise WarehouseError("أضف صنفاً واحداً على الأقل.")

    base_note = (note or "").strip()
    tr = WarehouseTransfer(
        from_warehouse_id=source.id,
        to_warehouse_id=dest.id,
        status=WarehouseTransferStatus.PENDING,
        note=base_note or None,
        created_by_id=user_id,
    )
    db.add(tr)
    db.flush()

    for pid, qty in lines:
        if qty <= 0:
            raise WarehouseError("الكمية يجب أن تكون أكبر من صفر.")
        line_note = f"صرف معلق إلى {dest.name_ar}"
        if base_note:
            line_note = f"{line_note} — {base_note}"
        apply_movement(
            db,
            product_id=pid,
            quantity_delta=-qty,
            movement_type=StockMovementType.TRANSFER,
            user_id=user_id,
            warehouse_id=source.id,
            note=line_note,
            counterparty_warehouse_id=dest.id,
            transfer_id=tr.id,
        )
        db.add(
            WarehouseTransferLine(
                transfer_id=tr.id,
                product_id=pid,
                qty_sent=qty,
                line_status=WarehouseTransferLineStatus.PENDING,
            )
        )
    db.flush()
    return tr


def approve_transfer_line(
    db: Session,
    line_id: int,
    user_id: int | None,
    *,
    qty_received: Decimal | None = None,
) -> WarehouseTransferLine:
    line = db.get(WarehouseTransferLine, line_id)
    if line is None:
        raise TransferError("البند غير موجود.")
    if line.line_status not in (
        WarehouseTransferLineStatus.PENDING,
        WarehouseTransferLineStatus.MODIFY_REQUESTED,
    ):
        raise TransferError("تمت معالجة هذا البند مسبقاً.")

    tr = db.get(WarehouseTransfer, line.transfer_id)
    if tr is None:
        raise TransferError("مستند الصرف غير موجود.")

    qty = qty_received if qty_received is not None else line.qty_sent
    qty = Decimal(str(qty))
    if qty <= 0:
        raise TransferError("كمية الاستلام يجب أن تكون أكبر من صفر.")
    if qty > line.qty_sent:
        raise TransferError("لا يمكن استلام كمية أكبر من المصروف.")

    main = db.get(Warehouse, tr.from_warehouse_id)
    dest = db.get(Warehouse, tr.to_warehouse_id)
    if main is None or dest is None:
        raise TransferError("المخزن غير صالح.")

    # فرق عن الكمية المصروفة — يُعاد للرئيسي
    if qty < line.qty_sent:
        diff = line.qty_sent - qty
        apply_movement(
            db,
            product_id=line.product_id,
            quantity_delta=diff,
            movement_type=StockMovementType.TRANSFER,
            user_id=user_id,
            warehouse_id=main.id,
            note=f"تعديل استلام — إرجاع فائض إلى {main.name_ar}",
            counterparty_warehouse_id=dest.id,
            transfer_id=tr.id,
        )

    recv_note = f"استلام مصادَق من {main.name_ar}"
    apply_movement(
        db,
        product_id=line.product_id,
        quantity_delta=qty,
        movement_type=StockMovementType.TRANSFER,
        user_id=user_id,
        warehouse_id=dest.id,
        note=recv_note,
        counterparty_warehouse_id=main.id,
        transfer_id=tr.id,
    )

    line.qty_received = qty
    line.line_status = WarehouseTransferLineStatus.APPROVED
    line.responded_by_id = user_id
    line.responded_at = datetime.now(timezone.utc)
    db.flush()
    _refresh_transfer_status(db, tr)
    return line


def reject_transfer_line(
    db: Session,
    line_id: int,
    user_id: int | None,
    *,
    note: str | None = None,
) -> WarehouseTransferLine:
    line = db.get(WarehouseTransferLine, line_id)
    if line is None:
        raise TransferError("البند غير موجود.")
    if line.line_status not in (
        WarehouseTransferLineStatus.PENDING,
        WarehouseTransferLineStatus.MODIFY_REQUESTED,
    ):
        raise TransferError("تمت معالجة هذا البند مسبقاً.")

    tr = db.get(WarehouseTransfer, line.transfer_id)
    if tr is None:
        raise TransferError("مستند الصرف غير موجود.")

    main = db.get(Warehouse, tr.from_warehouse_id)
    dest = db.get(Warehouse, tr.to_warehouse_id)
    if main is None or dest is None:
        raise TransferError("المخزن غير صالح.")

    apply_movement(
        db,
        product_id=line.product_id,
        quantity_delta=line.qty_sent,
        movement_type=StockMovementType.TRANSFER,
        user_id=user_id,
        warehouse_id=main.id,
        note=f"رفض استلام — إرجاع من {dest.name_ar}",
        counterparty_warehouse_id=dest.id,
        transfer_id=tr.id,
    )

    line.line_status = WarehouseTransferLineStatus.REJECTED
    line.qty_received = Decimal("0")
    line.recipient_note = (note or "").strip() or None
    line.responded_by_id = user_id
    line.responded_at = datetime.now(timezone.utc)
    db.flush()
    _refresh_transfer_status(db, tr)
    return line


def request_transfer_line_modify(
    db: Session,
    line_id: int,
    user_id: int | None,
    *,
    note: str,
    suggested_qty: Decimal | None = None,
) -> WarehouseTransferLine:
    line = db.get(WarehouseTransferLine, line_id)
    if line is None:
        raise TransferError("البند غير موجود.")
    if line.line_status != WarehouseTransferLineStatus.PENDING:
        raise TransferError("لا يمكن طلب تعديل على هذا البند.")

    msg = (note or "").strip()
    if not msg:
        raise TransferError("أدخل سبب طلب التعديل.")

    if suggested_qty is not None:
        sq = Decimal(str(suggested_qty))
        if sq <= 0 or sq > line.qty_sent:
            raise TransferError("الكمية المقترحة غير صالحة.")
        msg = f"{msg} (كمية مقترحة: {sq})"

    line.line_status = WarehouseTransferLineStatus.MODIFY_REQUESTED
    line.recipient_note = msg
    line.responded_by_id = user_id
    line.responded_at = datetime.now(timezone.utc)
    db.flush()

    tr = db.get(WarehouseTransfer, line.transfer_id)
    if tr:
        tr.status = WarehouseTransferStatus.PARTIAL
        db.flush()
    return line


def get_transfer(db: Session, transfer_id: int) -> WarehouseTransfer | None:
    return db.scalar(
        select(WarehouseTransfer)
        .where(WarehouseTransfer.id == transfer_id)
        .options(
            selectinload(WarehouseTransfer.lines).selectinload(WarehouseTransferLine.product),
            selectinload(WarehouseTransfer.from_warehouse),
            selectinload(WarehouseTransfer.to_warehouse),
        )
    )


def list_transfers_for_warehouse(
    db: Session,
    warehouse_id: int,
    *,
    pending_only: bool = False,
    limit: int = 100,
) -> list[WarehouseTransfer]:
    stmt = (
        select(WarehouseTransfer)
        .where(WarehouseTransfer.to_warehouse_id == warehouse_id)
        .options(
            selectinload(WarehouseTransfer.lines).selectinload(WarehouseTransferLine.product),
            selectinload(WarehouseTransfer.from_warehouse),
            selectinload(WarehouseTransfer.to_warehouse),
        )
        .order_by(WarehouseTransfer.id.desc())
        .limit(limit)
    )
    if pending_only:
        stmt = stmt.where(
            WarehouseTransfer.status.in_(
                (
                    WarehouseTransferStatus.PENDING,
                    WarehouseTransferStatus.PARTIAL,
                )
            )
        )
    return list(db.scalars(stmt).all())


def list_all_transfers(db: Session, *, limit: int = 100) -> list[WarehouseTransfer]:
    return list(
        db.scalars(
            select(WarehouseTransfer)
            .options(
                selectinload(WarehouseTransfer.lines).selectinload(WarehouseTransferLine.product),
                selectinload(WarehouseTransfer.from_warehouse),
                selectinload(WarehouseTransfer.to_warehouse),
            )
            .order_by(WarehouseTransfer.id.desc())
            .limit(limit)
        ).all()
    )


def line_status_label(status: WarehouseTransferLineStatus) -> str:
    return {
        WarehouseTransferLineStatus.PENDING: "بانتظار المصادقة",
        WarehouseTransferLineStatus.APPROVED: "تم المصادقة",
        WarehouseTransferLineStatus.REJECTED: "مرفوض",
        WarehouseTransferLineStatus.MODIFY_REQUESTED: "طلب تعديل",
    }.get(status, str(status.value))


def transfer_status_label(status: WarehouseTransferStatus) -> str:
    return {
        WarehouseTransferStatus.PENDING: "بانتظار الاستلام",
        WarehouseTransferStatus.PARTIAL: "استلام جزئي",
        WarehouseTransferStatus.COMPLETED: "مكتمل",
        WarehouseTransferStatus.CANCELLED: "ملغى",
    }.get(status, str(status.value))
