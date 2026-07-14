from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal

from datetime import datetime, timezone

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session, selectinload

from modules.catalog.bom_explosion import merge_line_requirements
from modules.catalog.models import Product, ProductCategory, ProductKind
from modules.catalog.service import assert_product_pos_category_visible, assert_product_sellable
from modules.inventory.models import StockMovementType
from modules.inventory.service import apply_movement, get_product_sales_warehouse_id
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
        context_type=SaleContext.TABLE,
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


def table_assignment_required(sale: Sale) -> bool:
    """طاولة مطلوبة قبل الإضافة/الدفع — ما عدا الطلب المُرسَل للمطبخ مسبقاً."""
    return (
        sale.context_type == SaleContext.TABLE
        and sale.table_id is None
        and sale.sent_to_kitchen_at is None
    )


def sale_line_item_count(sale: Sale | None) -> int:
    """مجموع كميات البنود في الفاتورة (لعرضها على مربع الطاولة)."""
    if sale is None or not sale.lines:
        return 0
    total = 0
    for ln in sale.lines:
        q = ln.quantity or Decimal("0")
        try:
            total += int(q) if q == int(q) else max(1, round(float(q)))
        except (TypeError, ValueError):
            total += 1
    return total


def table_short_label(name_ar: str, fallback_index: int) -> str:
    """رقم قصير للطاولة من الاسم أو ترتيب العرض."""
    raw = (name_ar or "").strip()
    m = re.search(r"(\d+)\s*$", raw)
    if m:
        return m.group(1)
    m = re.search(r"(\d+)", raw)
    if m:
        return m.group(1)
    return str(fallback_index)


def find_draft_for_table(
    db: Session,
    *,
    user_id: int,
    table_id: int,
    pos_shift_id: int | None = None,
) -> Sale | None:
    """أحدث مسودة مرتبطة بطاولة (حتى الفارغة) لنفس الكاشير."""
    stmt = (
        select(Sale)
        .where(
            Sale.status == SaleStatus.DRAFT,
            Sale.source == SaleSource.POS,
            Sale.created_by_id == user_id,
            Sale.context_type == SaleContext.TABLE,
            Sale.table_id == table_id,
        )
        .options(selectinload(Sale.lines))
        .order_by(Sale.id.desc())
        .limit(1)
    )
    if pos_shift_id is not None:
        stmt = stmt.where(
            or_(Sale.pos_shift_id == pos_shift_id, Sale.pos_shift_id.is_(None))
        )
    return db.scalars(stmt).first()


def _pick_table_draft(
    candidates: list[Sale],
    *,
    current_sale_id: int | None,
) -> Sale | None:
    """أي مسودة تُعرض على مربع الطاولة: الطلب المفتوح أولاً ثم الأكثر بنوداً."""
    if not candidates:
        return None
    if current_sale_id is not None:
        for s in candidates:
            if s.id == current_sale_id:
                return s
    return max(
        candidates,
        key=lambda s: (sale_line_item_count(s), int(s.id or 0)),
    )


def build_table_board(
    db: Session,
    *,
    user_id: int,
    pos_shift_id: int | None,
    tables: list,
    current_sale_id: int | None = None,
    current_table_id: int | None = None,
    current_sale: Sale | None = None,
) -> list[dict]:
    """بيانات شبكة الطاولات: رقم، عدد الأصناف، حالة الإرسال للمطبخ."""
    from modules.catalog.models import DiningTable

    open_orders = list_pos_open_drafts(
        db, user_id=user_id, pos_shift_id=pos_shift_id, limit=80
    )
    by_id: dict[int, Sale] = {s.id: s for s in open_orders}
    if (
        current_sale is not None
        and current_sale.status == SaleStatus.DRAFT
        and current_sale.context_type == SaleContext.TABLE
        and current_sale.table_id
    ):
        by_id[current_sale.id] = current_sale

    per_table: dict[int, list[Sale]] = defaultdict(list)
    for s in by_id.values():
        if s.context_type != SaleContext.TABLE or not s.table_id:
            continue
        per_table[int(s.table_id)].append(s)

    by_table: dict[int, Sale] = {}
    for tid, candidates in per_table.items():
        picked = _pick_table_draft(candidates, current_sale_id=current_sale_id)
        if picked is not None:
            by_table[tid] = picked

    board: list[dict] = []
    for idx, t in enumerate(tables, 1):
        assert isinstance(t, DiningTable)
        s = by_table.get(t.id)
        cnt = sale_line_item_count(s)
        sent = bool(s and s.sent_to_kitchen_at)
        board.append(
            {
                "id": t.id,
                "label": table_short_label(t.name_ar, idx),
                "name_ar": t.name_ar,
                "sale_id": s.id if s else None,
                "item_count": cnt,
                "sent_kitchen": sent,
                "show_badge": cnt > 0,
                "badge_tone": "sent" if sent else "pending",
                "is_selected": current_table_id == t.id
                or (current_sale_id is not None and s is not None and s.id == current_sale_id),
            }
        )
    return board


def list_kitchen_unsent_orders(
    db: Session,
    *,
    user_id: int,
    pos_shift_id: int | None = None,
    exclude_sale_id: int | None = None,
) -> list[dict]:
    """طلبات فيها بنود ولم تُرسل للمطبخ بعد (تحذير للكاشير)."""
    rows: list[dict] = []
    for s in list_pos_open_drafts(
        db,
        user_id=user_id,
        pos_shift_id=pos_shift_id,
        exclude_sale_id=exclude_sale_id,
        limit=40,
    ):
        if s.sent_to_kitchen_at or not s.lines:
            continue
        label = sale_pos_order_label(s)
        rows.append({"id": s.id, "label": label, "item_count": sale_line_item_count(s)})
    return rows


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


def _sale_context_type_value(s: Sale) -> str:
    return s.context_type.value if s.context_type else "TABLE"


def sale_context_label_ar(s: Sale, *, detailed: bool = False) -> str:
    """تسمية عربية لنوع الطلب — لا تُرجع قيم enum إنجليزية."""
    ctx = _sale_context_type_value(s)
    if ctx == "TABLE":
        if s.table:
            if detailed:
                return f"طاولة — {s.table.name_ar}"
            return f"طاولة {table_short_label(s.table.name_ar, s.table_id)}"
        if s.table_id:
            return f"طاولة #{s.table_id}"
        return "طاولة — غير محددة"
    if ctx == "ROOM":
        return "شقة"
    if ctx == "EXTERNAL":
        ext = s.external_order_type.value if s.external_order_type else "PICKUP"
        if ext == "DELIVERY":
            return "توصيل"
        if ext == "PICKUP":
            return "استلام من المطعم"
        return "خارجي"
    return "طلب"


def sale_pos_order_label(s: Sale) -> str:
    """عنوان مختصر للطلب في قوائم POS وإغلاق الجلسة."""
    return sale_context_label_ar(s, detailed=False)


def list_pending_orders_blocking_shift_close(
    db: Session,
    *,
    user_id: int,
    pos_shift_id: int,
    limit: int = 50,
) -> list[dict]:
    """مسودات عالقة تمنع إغلاق الجلسة حتى يُكمَل البيع أو يُلغى الطلب."""
    rows: list[dict] = []
    for s in list_pos_open_drafts(
        db, user_id=user_id, pos_shift_id=pos_shift_id, limit=limit
    ):
        sent = bool(s.sent_to_kitchen_at)
        if sent:
            status = "مُرسَل للمطبخ — لم يُدفع بعد"
            action_hint = (
                "أتمِم البيع والدفع إن تم التسليم، أو ألغِ الطلب إن لم يُنفَّذ"
            )
        else:
            status = "قيد الإدخال — لم يُرسل للمطبخ"
            action_hint = "أكمل الطلب (إرسال أو دفع) أو ألغِه إن لم يعد مطلوباً"
        rows.append(
            {
                "id": s.id,
                "label": sale_pos_order_label(s),
                "total": s.total,
                "status": status,
                "action_hint": action_hint,
                "sent_kitchen": sent,
                "item_count": sale_line_item_count(s),
            }
        )
    return rows


def cancel_unsent_drafts_for_table(
    db: Session,
    *,
    user_id: int,
    table_id: int,
    pos_shift_id: int | None = None,
    except_sale_id: int | None = None,
) -> int:
    """إلغاء كل المسودات غير المُرسَلة للمطبخ على نفس الطاولة (تنظيف التحذيرات القديمة)."""
    stmt = select(Sale).where(
        Sale.status == SaleStatus.DRAFT,
        Sale.source == SaleSource.POS,
        Sale.created_by_id == user_id,
        Sale.table_id == table_id,
        Sale.sent_to_kitchen_at.is_(None),
    )
    if pos_shift_id is not None:
        stmt = stmt.where(
            or_(Sale.pos_shift_id == pos_shift_id, Sale.pos_shift_id.is_(None))
        )
    n = 0
    for s in db.scalars(stmt).all():
        if except_sale_id is not None and s.id == except_sale_id:
            continue
        try:
            cancel_sale(db, s.id)
            n += 1
        except SalesError:
            continue
    if n:
        db.flush()
    return n


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
    """يتحقق أن الفاتورة مسودة — التعديل بعد الإرسال للمطبخ مسموح بقواعد خاصة."""
    if sale.status != SaleStatus.DRAFT:
        raise SalesError("الفاتورة غير موجودة أو ليست مسودة.")


def line_kitchen_sent_qty(line: SaleLine) -> Decimal:
    return (line.kitchen_sent_qty or Decimal("0")).quantize(Decimal("0.0001"))


def line_kitchen_pending_qty(line: SaleLine) -> Decimal:
    pending = (line.quantity - line_kitchen_sent_qty(line)).quantize(Decimal("0.0001"))
    return pending if pending > 0 else Decimal("0")


def sale_has_pending_kitchen(sale: Sale) -> bool:
    return any(line_kitchen_pending_qty(ln) > 0 for ln in sale.lines)


def _assert_line_qty_not_below_sent(line: SaleLine, new_qty: Decimal) -> None:
    sent = line_kitchen_sent_qty(line)
    if new_qty < sent:
        raise SalesError(
            f"لا يمكن تقليل الكمية أقل من {sent} (مُرسل للمطبخ). "
            "يمكنك إضافة كميات جديدة فقط."
        )


def _assert_line_removable(line: SaleLine) -> None:
    if line_kitchen_sent_qty(line) > 0:
        raise SalesError(
            "لا يمكن حذف صنف أُرسل للمطبخ. يمكنك إضافة كميات جديدة فقط."
        )


def _aggregate_component_needs_for_delta(
    db: Session,
    sale: Sale,
    items: list[tuple[SaleLine, Decimal]],
) -> dict[int, Decimal]:
    from modules.sales.packaging import sale_requires_packaging

    pairs: list[tuple[int | None, Decimal]] = []
    for line, delta_qty in items:
        if delta_qty <= 0 or line.product is None:
            continue
        pairs.append((line.product_id, delta_qty))
    return merge_line_requirements(
        db, pairs, include_packaging=sale_requires_packaging(sale)
    )


def mark_lines_kitchen_sent(db: Session, sale: Sale) -> None:
    db.refresh(sale, ["lines"])
    for line in sale.lines:
        line.kitchen_sent_qty = line.quantity
    db.flush()


def _apply_sale_inventory_needs(
    db: Session,
    *,
    needs: dict[int, Decimal],
    sale_id: int,
    user_id: int | None,
    pos_shift_id: int | None,
    note: str,
) -> None:
    """يخصم كل مكوّن من مخزنه المربوط، وإلا من مخزن الجلسة/الإعداد."""
    for pid, need in needs.items():
        sales_wh = get_product_sales_warehouse_id(db, pid, pos_shift_id=pos_shift_id)
        apply_movement(
            db,
            product_id=pid,
            quantity_delta=-need,
            movement_type=StockMovementType.SALE,
            user_id=user_id,
            sale_id=sale_id,
            warehouse_id=sales_wh,
            note=note,
        )


def send_kitchen_supplement(db: Session, sale_id: int, user_id: int | None) -> list:
    """يطبع تكميلاً للمطبخ للبنود الجديدة/الزائدة ويُحدّث الكميات المُرسَلة."""
    from modules.kds.service import create_supplement_tickets_for_sale

    sale = load_sale_with_lines(db, sale_id)
    if sale is None or sale.sent_to_kitchen_at is None:
        return []
    if not sale_has_pending_kitchen(sale):
        return []
    pending = [
        (ln, line_kitchen_pending_qty(ln))
        for ln in sale.lines
        if line_kitchen_pending_qty(ln) > 0
    ]
    needs = _aggregate_component_needs_for_delta(db, sale, pending)
    _apply_sale_inventory_needs(
        db,
        needs=needs,
        sale_id=sale.id,
        user_id=user_id,
        pos_shift_id=sale.pos_shift_id,
        note="خصم بيع (تكميل مطبخ)",
    )
    tickets = create_supplement_tickets_for_sale(db, sale_id)
    mark_lines_kitchen_sent(db, sale)
    return tickets


def load_sale_with_lines(db: Session, sale_id: int) -> Sale | None:
    stmt = (
        select(Sale)
        .where(Sale.id == sale_id)
        .options(
            selectinload(Sale.lines)
            .selectinload(SaleLine.product)
            .selectinload(Product.category)
            .selectinload(ProductCategory.parent),
            selectinload(Sale.lines).selectinload(SaleLine.product).selectinload(
                Product.bom_lines_as_parent
            ),
            selectinload(Sale.customer),
            selectinload(Sale.table),
        )
    )
    return db.execute(stmt).scalar_one_or_none()


def load_completed_sale_for_print(db: Session, sale_id: int) -> Sale | None:
    stmt = (
        select(Sale)
        .where(Sale.id == sale_id, Sale.status == SaleStatus.COMPLETED)
        .options(
            selectinload(Sale.lines)
            .selectinload(SaleLine.product)
            .selectinload(Product.category)
            .selectinload(ProductCategory.parent),
            selectinload(Sale.table),
            selectinload(Sale.customer),
        )
    )
    return db.execute(stmt).scalar_one_or_none()


def _line_note_match_clause(note_norm: str | None):
    if note_norm:
        return SaleLine.line_note == note_norm
    return or_(SaleLine.line_note.is_(None), SaleLine.line_note == "")


def add_line_to_sale(
    db: Session,
    sale_id: int,
    product_id: int,
    quantity: Decimal,
    user_id: int | None,
    *,
    line_note: str | None = None,
) -> Sale:
    sale = get_draft_sale(db, sale_id)
    if sale is None:
        raise SalesError("الفاتورة غير موجودة أو ليست مسودة.")
    _assert_draft_lines_mutable(sale)
    product = db.get(Product, product_id)
    if product is None or not product.is_active:
        raise SalesError("الصنف غير متاح.")
    assert_product_sellable(product)
    assert_product_pos_category_visible(db, product)
    if product.sell_price is None:
        raise SalesError("لا يوجد سعر بيع لهذا الصنف.")
    if quantity <= 0:
        raise SalesError("الكمية غير صالحة.")

    unit = product.sell_price
    line_total = (quantity * unit).quantize(Decimal("0.001"))

    note_norm = (line_note or "").strip() or None
    line = SaleLine(
        sale_id=sale.id,
        product_id=product.id,
        quantity=quantity,
        unit_price=unit,
        line_total=line_total,
        line_note=note_norm,
    )
    db.add(line)
    db.flush()
    _recalc_sale_total(db, sale)
    return sale


def update_line_note(
    db: Session, sale_id: int, line_id: int, line_note: str | None
) -> Sale:
    sale = get_draft_sale(db, sale_id)
    if sale is None:
        raise SalesError("الفاتورة غير موجودة أو ليست مسودة.")
    _assert_draft_lines_mutable(sale)
    line = db.get(SaleLine, line_id)
    if line is None or line.sale_id != sale_id:
        raise SalesError("البند غير موجود.")
    line.line_note = (line_note or "").strip() or None
    db.flush()
    return sale


def adjust_line_quantity(
    db: Session, sale_id: int, line_id: int, delta: Decimal
) -> Sale:
    """زيادة أو تقليل كمية بند في السلة؛ إن وصلت إلى صفر أو أقل يُحذف البند."""
    sale = get_draft_sale(db, sale_id)
    if sale is None:
        raise SalesError("الفاتورة غير موجودة أو ليست مسودة.")
    _assert_draft_lines_mutable(sale)
    line = db.get(SaleLine, line_id)
    if line is None or line.sale_id != sale_id:
        raise SalesError("البند غير موجود.")
    if delta == 0:
        raise SalesError("الكمية غير صالحة.")

    new_qty = (line.quantity + delta).quantize(Decimal("0.0001"))
    if new_qty <= 0:
        _assert_line_removable(line)
        db.delete(line)
    else:
        _assert_line_qty_not_below_sent(line, new_qty)
        line.quantity = new_qty
        line.line_total = (new_qty * line.unit_price).quantize(Decimal("0.001"))
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
    _assert_line_removable(line)
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
    db: Session,
    sale_id: int,
    product_id: int,
    quantity: Decimal,
    user_id: int | None,
    *,
    line_note: str | None = None,
) -> Sale:
    """إضافة بند أو زيادة الكمية إن وُجد نفس المنتج بنفس الملاحظة."""
    sale = get_draft_sale(db, sale_id)
    if sale is None:
        raise SalesError("الفاتورة غير موجودة أو ليست مسودة.")
    _assert_draft_lines_mutable(sale)
    product = db.get(Product, product_id)
    if product is None or not product.is_active:
        raise SalesError("الصنف غير متاح.")
    assert_product_sellable(product)
    assert_product_pos_category_visible(db, product)
    if product.sell_price is None:
        raise SalesError("لا يوجد سعر بيع لهذا الصنف.")
    if quantity <= 0:
        raise SalesError("الكمية غير صالحة.")

    unit = product.sell_price
    note_norm = (line_note or "").strip() or None
    existing = db.execute(
        select(SaleLine).where(
            SaleLine.sale_id == sale_id,
            SaleLine.product_id == product_id,
            _line_note_match_clause(note_norm),
        )
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
            line_note=note_norm,
        )
        db.add(line)
        db.flush()
    _recalc_sale_total(db, sale)
    return sale


def _aggregate_component_needs(db: Session, sale: Sale) -> dict[int, Decimal]:
    """الكميات المطلوبة من مخزون المكوّنات الخام (مع توسيع المنتجات الوسيطة)."""
    pairs = [(line.product_id, line.quantity) for line in sale.lines if line.product_id]
    from modules.sales.order_policy import (
        load_order_policy,
        table_complimentary_product_id_for_sale,
    )
    from modules.sales.packaging import sale_requires_packaging

    comp_product_id = table_complimentary_product_id_for_sale(
        sale, load_order_policy(db)
    )
    if comp_product_id is not None:
        pairs.append((comp_product_id, Decimal("1")))
    return merge_line_requirements(
        db, pairs, include_packaging=sale_requires_packaging(sale)
    )


def send_draft_to_kitchen(
    db: Session,
    sale_id: int,
    user_id: int | None,
    *,
    room_session_ok: bool = False,
    pos_shift_id: int | None = None,
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

    needs = _aggregate_component_needs(db, sale)
    _apply_sale_inventory_needs(
        db,
        needs=needs,
        sale_id=sale.id,
        user_id=user_id,
        pos_shift_id=pos_shift_id,
        note="خصم بيع (إرسال مطبخ)",
    )

    sale.sent_to_kitchen_at = datetime.now(timezone.utc)
    mark_lines_kitchen_sent(db, sale)
    db.flush()
    try:
        from modules.notifications.hooks import emit_order_sent_to_kitchen

        emit_order_sent_to_kitchen(db, sale)
    except Exception:  # noqa: BLE001
        pass
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
        from modules.messaging.chat_order_service import is_online_guest_sale

        if sale.source != SaleSource.POS and not is_online_guest_sale(sale):
            raise SalesError("جلسة الكاشير تُربط بفواتير نقطة البيع فقط.")
        sh = db.get(PosShift, int(pos_shift_id))
        if sh is None or sh.status != PosShiftStatus.OPEN:
            raise SalesError("جلسة الكاشير غير صالحة أو مغلقة. افتح جلسة من «جلسة البيع».")
        if sh.user_id != int(user_id):
            raise SalesError("لا يمكن إتمام البيع على جلسة مستخدم آخر.")
        sale.pos_shift_id = int(pos_shift_id)

    if sale.sent_to_kitchen_at is None:
        needs = _aggregate_component_needs(db, sale)
        _apply_sale_inventory_needs(
            db,
            needs=needs,
            sale_id=sale.id,
            user_id=user_id,
            pos_shift_id=pos_shift_id or sale.pos_shift_id,
            note="خصم بيع",
        )

    sale.status = SaleStatus.COMPLETED
    db.flush()
    from modules.gl.posting import post_sale_completed_shadow_safe

    post_sale_completed_shadow_safe(db, sale)
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
    try:
        from modules.notifications.marketing_hooks import emit_order_cancelled

        emit_order_cancelled(db, sale, reason="إلغاء مسودة")
    except Exception:  # noqa: BLE001
        pass
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
