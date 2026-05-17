from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from datetime import datetime, timezone

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session, selectinload

from modules.catalog.models import Product, ProductKind
from modules.catalog.service import assert_product_sellable
from modules.inventory.models import StockMovementType
from modules.inventory.service import (
    InsufficientStock,
    apply_movement,
    get_balance,
    get_sales_warehouse_id,
)
from modules.pos_shifts.models import PosShift, PosShiftStatus
from modules.sales.models import Sale, SaleContext, SaleLine, SaleSource, SaleStatus


class SalesError(Exception):
    pass


def create_draft_sale(
    db: Session,
    user_id: int | None,
    source: SaleSource = SaleSource.POS,
    *,
    pos_shift_id: int | None = None,
) -> Sale:
    s = Sale(
        status=SaleStatus.DRAFT,
        total=Decimal("0"),
        created_by_id=user_id,
        source=source,
        pos_shift_id=pos_shift_id,
    )
    db.add(s)
    db.flush()
    return s


def get_draft_sale(db: Session, sale_id: int) -> Sale | None:
    s = db.get(Sale, sale_id)
    if s is None or s.status != SaleStatus.DRAFT:
        return None
    return s


def is_meaningful_open_order(sale: Sale) -> bool:
    """طلب «مفتوح» فعلياً: فيه بنود أو أُرسل للمطبخ وينتظر التحصيل."""
    if sale.sent_to_kitchen_at is not None:
        return True
    if sale.lines:
        return True
    return False


def list_pos_open_drafts(
    db: Session,
    *,
    user_id: int,
    pos_shift_id: int | None = None,
    exclude_sale_id: int | None = None,
    limit: int = 30,
) -> list[Sale]:
    """مسودات POS ذات محتوى فقط (بنود أو مُرسَلة للمطبخ)."""
    has_lines = exists(select(SaleLine.id).where(SaleLine.sale_id == Sale.id))
    stmt = (
        select(Sale)
        .where(
            Sale.status == SaleStatus.DRAFT,
            Sale.source == SaleSource.POS,
            Sale.created_by_id == user_id,
            or_(Sale.sent_to_kitchen_at.isnot(None), has_lines),
        )
        .options(
            selectinload(Sale.table),
            selectinload(Sale.lines),
        )
        .order_by(Sale.id.desc())
        .limit(limit)
    )
    if pos_shift_id is not None:
        stmt = stmt.where(
            or_(Sale.pos_shift_id == pos_shift_id, Sale.pos_shift_id.is_(None))
        )
    rows = list(db.scalars(stmt).all())
    if exclude_sale_id is not None:
        rows = [r for r in rows if r.id != exclude_sale_id]
    return rows


def cancel_stale_empty_pos_drafts(
    db: Session,
    *,
    user_id: int,
    pos_shift_id: int | None = None,
    keep_sale_id: int | None = None,
) -> int:
    """إلغاء مسودات فارغة لم تُرسَل للمطبخ (تنظيف القائمة والقاعدة)."""
    stmt = select(Sale).where(
        Sale.status == SaleStatus.DRAFT,
        Sale.source == SaleSource.POS,
        Sale.created_by_id == user_id,
        Sale.sent_to_kitchen_at.is_(None),
    )
    if pos_shift_id is not None:
        stmt = stmt.where(
            or_(Sale.pos_shift_id == pos_shift_id, Sale.pos_shift_id.is_(None))
        )
    cancelled = 0
    for s in db.scalars(stmt).all():
        if keep_sale_id is not None and s.id == keep_sale_id:
            continue
        db.refresh(s, ["lines"])
        if s.lines:
            continue
        s.status = SaleStatus.CANCELLED
        cancelled += 1
    if cancelled:
        db.flush()
    return cancelled


def _assert_draft_lines_mutable(sale: Sale) -> None:
    if sale.sent_to_kitchen_at is not None:
        raise SalesError(
            "لا يمكن تعديل الأصناف بعد إرسال الطلب للمطبخ. "
            "أكمل الدفع لهذا الطلب، أو استخدم «طلب جديد» لطلب منفصل."
        )


def load_sale_with_lines(db: Session, sale_id: int) -> Sale | None:
    stmt = (
        select(Sale)
        .where(Sale.id == sale_id)
        .options(
            selectinload(Sale.lines).selectinload(SaleLine.product).selectinload(Product.category),
            selectinload(Sale.lines).selectinload(SaleLine.product).selectinload(
                Product.bom_lines_as_parent
            ),
        )
    )
    return db.execute(stmt).scalar_one_or_none()


def load_completed_sale_for_print(db: Session, sale_id: int) -> Sale | None:
    stmt = (
        select(Sale)
        .where(Sale.id == sale_id, Sale.status == SaleStatus.COMPLETED)
        .options(
            selectinload(Sale.lines).selectinload(SaleLine.product).selectinload(Product.category),
        )
    )
    return db.execute(stmt).scalar_one_or_none()


def add_line_to_sale(
    db: Session, sale_id: int, product_id: int, quantity: Decimal, user_id: int | None
) -> Sale:
    sale = get_draft_sale(db, sale_id)
    if sale is None:
        raise SalesError("الفاتورة غير موجودة أو ليست مسودة.")
    _assert_draft_lines_mutable(sale)
    product = db.get(Product, product_id)
    if product is None or not product.is_active:
        raise SalesError("الصنف غير متاح.")
    assert_product_sellable(product)
    if product.sell_price is None:
        raise SalesError("لا يوجد سعر بيع لهذا الصنف.")
    if quantity <= 0:
        raise SalesError("الكمية غير صالحة.")

    unit = product.sell_price
    line_total = (quantity * unit).quantize(Decimal("0.001"))

    line = SaleLine(
        sale_id=sale.id,
        product_id=product.id,
        quantity=quantity,
        unit_price=unit,
        line_total=line_total,
    )
    db.add(line)
    db.flush()
    _recalc_sale_total(db, sale)
    return sale


def remove_line(db: Session, sale_id: int, line_id: int) -> Sale:
    sale = get_draft_sale(db, sale_id)
    if sale is None:
        raise SalesError("الفاتورة غير موجودة أو ليست مسودة.")
    _assert_draft_lines_mutable(sale)
    line = db.get(SaleLine, line_id)
    if line is None or line.sale_id != sale_id:
        raise SalesError("البند غير موجود.")
    db.delete(line)
    db.flush()
    _recalc_sale_total(db, sale)
    return sale


def _recalc_sale_total(db: Session, sale: Sale) -> None:
    db.refresh(sale, ["lines"])
    t = sum((ln.line_total for ln in sale.lines), Decimal("0"))
    sale.total = t.quantize(Decimal("0.001"))
    db.flush()


def add_or_increment_line_to_sale(
    db: Session, sale_id: int, product_id: int, quantity: Decimal, user_id: int | None
) -> Sale:
    """إضافة بند جديد أو زيادة الكمية إن وُجد نفس المنتج في المسودة (مناسب للباركود)."""
    sale = get_draft_sale(db, sale_id)
    if sale is None:
        raise SalesError("الفاتورة غير موجودة أو ليست مسودة.")
    _assert_draft_lines_mutable(sale)
    product = db.get(Product, product_id)
    if product is None or not product.is_active:
        raise SalesError("الصنف غير متاح.")
    assert_product_sellable(product)
    if product.sell_price is None:
        raise SalesError("لا يوجد سعر بيع لهذا الصنف.")
    if quantity <= 0:
        raise SalesError("الكمية غير صالحة.")

    unit = product.sell_price
    existing = db.execute(
        select(SaleLine).where(SaleLine.sale_id == sale_id, SaleLine.product_id == product_id)
    ).scalar_one_or_none()
    if existing:
        existing.quantity = (existing.quantity + quantity).quantize(Decimal("0.0001"))
        existing.line_total = (existing.quantity * existing.unit_price).quantize(Decimal("0.001"))
        db.flush()
    else:
        line_total = (quantity * unit).quantize(Decimal("0.001"))
        line = SaleLine(
            sale_id=sale.id,
            product_id=product.id,
            quantity=quantity,
            unit_price=unit,
            line_total=line_total,
        )
        db.add(line)
        db.flush()
    _recalc_sale_total(db, sale)
    return sale


def _aggregate_component_needs(sale: Sale) -> dict[int, Decimal]:
    """الكميات المطلوبة من مخزون المكوّنات لإتمام بنود البيع.
    - منتج له وصفة تركيب: تُخصم مكوّناته بحسب الوصفة.
    - منتج بدون وصفة: لا يُخصم شيء من المخزون (لأن المخزون يضم المكوّنات فقط).
    """
    needs: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for line in sale.lines:
        product = line.product
        for bom in product.bom_lines_as_parent:
            needs[bom.component_product_id] += bom.qty_per_parent * line.quantity
    return dict(needs)


def send_draft_to_kitchen(
    db: Session,
    sale_id: int,
    user_id: int | None,
    *,
    room_session_ok: bool = False,
) -> Sale:
    """يخصم مخزون المكوّنات ويُعلّم المسودة كمُرسَلة للمطبخ دون إتمام الدفع."""
    sale = load_sale_with_lines(db, sale_id)
    if sale is None or sale.status != SaleStatus.DRAFT:
        raise SalesError("لا يمكن إرسال هذه الفاتورة للمطبخ.")
    if sale.sent_to_kitchen_at is not None:
        raise SalesError(
            "تم إرسال هذا الطلب للمطبخ مسبقاً. أكمل الدفع، أو افتح «طلب جديد» لطلب آخر."
        )
    if not sale.lines:
        raise SalesError("الفاتورة فارغة.")
    if user_id is None:
        raise SalesError("يجب تسجيل الدخول لإرسال الطلب للمطبخ.")

    if sale.context_type == SaleContext.TABLE:
        if not sale.table_id:
            raise SalesError("اختر الطاولة قبل الإرسال للمطبخ.")
    elif sale.context_type == SaleContext.ROOM:
        if not room_session_ok:
            raise SalesError("اختر الشقة قبل الإرسال للمطبخ.")
    elif sale.context_type == SaleContext.EXTERNAL:
        pass

    sales_wh = get_sales_warehouse_id(db)
    needs = _aggregate_component_needs(sale)
    for pid, need in needs.items():
        avail = get_balance(db, pid, sales_wh)
        if avail < need:
            p = db.get(Product, pid)
            name = p.name_ar if p else str(pid)
            raise InsufficientStock(name, need, avail)

    for pid, need in needs.items():
        apply_movement(
            db,
            product_id=pid,
            quantity_delta=-need,
            movement_type=StockMovementType.SALE,
            user_id=user_id,
            sale_id=sale.id,
            warehouse_id=sales_wh,
            note="خصم بيع (إرسال مطبخ)",
        )

    sale.sent_to_kitchen_at = datetime.now(timezone.utc)
    db.flush()
    return sale


def complete_sale(
    db: Session,
    sale_id: int,
    user_id: int | None,
    *,
    pos_shift_id: int | None = None,
) -> Sale:
    sale = load_sale_with_lines(db, sale_id)
    if sale is None or sale.status != SaleStatus.DRAFT:
        raise SalesError("لا يمكن إتمام هذه الفاتورة.")
    if not sale.lines:
        raise SalesError("الفاتورة فارغة.")

    if pos_shift_id is not None:
        if user_id is None:
            raise SalesError("لا يمكن ربط جلسة الكاشير بدون مستخدم مسجّل.")
        if sale.source != SaleSource.POS:
            raise SalesError("جلسة الكاشير تُربط بفواتير نقطة البيع فقط.")
        sh = db.get(PosShift, int(pos_shift_id))
        if sh is None or sh.status != PosShiftStatus.OPEN:
            raise SalesError("جلسة الكاشير غير صالحة أو مغلقة. افتح جلسة من «جلسة البيع».")
        if sh.user_id != int(user_id):
            raise SalesError("لا يمكن إتمام البيع على جلسة مستخدم آخر.")
        sale.pos_shift_id = int(pos_shift_id)

    if sale.sent_to_kitchen_at is None:
        sales_wh = get_sales_warehouse_id(db)
        needs = _aggregate_component_needs(sale)
        for pid, need in needs.items():
            avail = get_balance(db, pid, sales_wh)
            if avail < need:
                p = db.get(Product, pid)
                name = p.name_ar if p else str(pid)
                raise InsufficientStock(name, need, avail)

        for pid, need in needs.items():
            apply_movement(
                db,
                product_id=pid,
                quantity_delta=-need,
                movement_type=StockMovementType.SALE,
                user_id=user_id,
                sale_id=sale.id,
                warehouse_id=sales_wh,
                note="خصم بيع",
            )

    sale.status = SaleStatus.COMPLETED
    db.flush()
    return sale


def cancel_sale(db: Session, sale_id: int) -> Sale:
    sale = db.get(Sale, sale_id)
    if sale is None:
        raise SalesError("الفاتورة غير موجودة.")
    if sale.status != SaleStatus.DRAFT:
        raise SalesError("يمكن إلغاء المسودات فقط.")
    if sale.sent_to_kitchen_at is not None:
        raise SalesError(
            "لا يمكن مسح مسودة أُرسلت للمطبخ (تم خصم المخزون). أكمل الدفع أو راجع المشرف."
        )
    sale.status = SaleStatus.CANCELLED
    db.flush()
    return sale


def create_online_sale_completed(
    db: Session,
    *,
    external_order_id: str,
    lines: list[tuple[int, Decimal]],
    user_id: int | None,
) -> Sale:
    """lines: (product_id FINAL, qty). Caller ensures عدم وجود فاتورة مكتملة بنفس المرجع."""
    sale = Sale(
        status=SaleStatus.DRAFT,
        total=Decimal("0"),
        source=SaleSource.ONLINE,
        external_order_id=external_order_id,
        created_by_id=user_id,
    )
    db.add(sale)
    db.flush()
    for pid, qty in lines:
        add_line_to_sale(db, sale.id, pid, qty, user_id)
    sale = load_sale_with_lines(db, sale.id)
    assert sale is not None
    complete_sale(db, sale.id, user_id)
    return sale
