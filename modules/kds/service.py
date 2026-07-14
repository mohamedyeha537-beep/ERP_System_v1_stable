"""خدمة شاشة المطبخ + إنشاء التذاكر عند إرسال الطلب للمطبخ."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

import json
import logging
import urllib.request

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from modules.catalog.models import CategoryRouting, Product, ProductCategory
from modules.kds.models import KitchenDepartment
from modules.printing.models import KitchenSection
from modules.printing.service import (
    enqueue_department_supplement_print,
    enqueue_department_ticket_print,
    enqueue_kitchen_ticket_print,
    enqueue_supplement_kitchen_ticket_print,
    group_lines_by_kitchen_section,
    lines_for_section_ticket,
    resolve_kitchen_section_id,
)
from modules.sales.models import KitchenTicket, Sale, SaleLine, TicketStatus
from modules.sales.receipt_layout import resolve_root_category
from app.number_format import format_qty_plain

log = logging.getLogger("kds")


def _coerce_routing_mode(mode: CategoryRouting | str) -> CategoryRouting:
    if isinstance(mode, CategoryRouting):
        return mode
    try:
        return CategoryRouting(str(mode))
    except ValueError:
        return CategoryRouting.NONE


def _ticket_row_from_line(ln: SaleLine) -> TicketLineRow:
    note = (getattr(ln, "line_note", None) or "").strip() or None
    if ln.product is None:
        name_ar = f"صنف #{ln.product_id}"
    else:
        name_ar = (ln.product.name_ar or ln.product.name_en or f"#{ln.product_id}").strip()
    return TicketLineRow(
        name_ar=name_ar or "—",
        qty=ln.quantity,
        notes=note,
    )


@dataclass
class TicketLineRow:
    name_ar: str
    qty: Decimal
    notes: str | None = None


@dataclass
class TicketView:
    ticket: KitchenTicket
    sale: Sale
    lines: list[TicketLineRow]
    table_label: str | None
    age_minutes: int
    section_label: str


@dataclass
class MasterSectionRow:
    name_ar: str
    lines: list[TicketLineRow]


def complimentary_line_for_sale(db: Session, sale: Sale) -> TicketLineRow | None:
    from modules.sales.order_policy import (
        load_order_policy,
        table_complimentary_product_id_for_sale,
    )

    product_id = table_complimentary_product_id_for_sale(sale, load_order_policy(db))
    if product_id is None:
        return None
    product = db.get(Product, product_id)
    if product is None or not product.is_active:
        return None
    label = (product.name_ar or product.name_en or f"#{product.id}").strip()
    return TicketLineRow(
        name_ar=label,
        qty=Decimal("1"),
        notes="ضيافة للطاولة فقط — تخصم من المخزون ولا تظهر في فاتورة الزبون",
    )


def complimentary_line_for_ticket(
    db: Session, ticket: KitchenTicket, sale: Sale
) -> TicketLineRow | None:
    if getattr(ticket, "is_supplement", False):
        return None
    first_ticket_id = db.scalar(
        select(func.min(KitchenTicket.id)).where(
            KitchenTicket.sale_id == sale.id,
            KitchenTicket.is_supplement.is_(False),
        )
    )
    if first_ticket_id is not None and int(first_ticket_id) != int(ticket.id):
        return None
    return complimentary_line_for_sale(db, sale)


def _group_lines_by_department(db: Session, sale: Sale) -> dict[int, list[SaleLine]]:
    out: dict[int, list[SaleLine]] = defaultdict(list)
    for line in sale.lines:
        if line.product is None:
            continue
        dept_id = line.product.kitchen_department_id
        if dept_id is None:
            continue
        out[dept_id].append(line)
    return out


def _group_lines_by_root(
    db: Session, sale: Sale
) -> dict[int, list[SaleLine]]:
    """يقسّم بنود الفاتورة حسب الفئة الجذر (للتوافق مع الإعداد القديم)."""
    out: dict[int, list[SaleLine]] = defaultdict(list)
    for line in sale.lines:
        if line.product is None:
            continue
        if line.product.kitchen_department_id is not None:
            continue
        if resolve_kitchen_section_id(db, line.product) is not None:
            continue
        root = resolve_root_category(db, line.product.category)
        if root is None:
            continue
        out[root.id].append(line)
    return out


def _ticket_exists(
    db: Session,
    sale_id: int,
    *,
    department_id: int | None = None,
    root_category_id: int | None = None,
    kitchen_section_id: int | None = None,
) -> KitchenTicket | None:
    stmt = select(KitchenTicket).where(KitchenTicket.sale_id == sale_id)
    if department_id is not None:
        stmt = stmt.where(KitchenTicket.kitchen_department_id == department_id)
    if root_category_id is not None:
        stmt = stmt.where(KitchenTicket.root_category_id == root_category_id)
    if kitchen_section_id is not None:
        stmt = stmt.where(KitchenTicket.kitchen_section_id == kitchen_section_id)
    return db.execute(stmt).scalar_one_or_none()


def _make_ticket(
    sale: Sale,
    *,
    department: KitchenDepartment | None = None,
    root: ProductCategory | None = None,
) -> KitchenTicket:
    if department is not None:
        mode = _coerce_routing_mode(department.routing_mode)
        target_info = department.routing_target
    elif root is not None:
        mode = _coerce_routing_mode(root.routing_mode)
        target_info = root.routing_target
    else:
        raise ValueError("department or root required")

    root_id = root.id if root is not None else None
    dept_id = department.id if department is not None else None

    ticket = KitchenTicket(
        sale_id=sale.id,
        root_category_id=root_id,
        kitchen_department_id=dept_id,
        status=TicketStatus.PENDING,
        routing_mode=mode.value,
        delivery_status="PENDING",
    )
    if mode == CategoryRouting.WHATSAPP:
        ticket.delivery_status = "PENDING_SEND"
        ticket.delivery_info = "في انتظار الإرسال"
    elif mode == CategoryRouting.PRINT:
        ticket.delivery_status = "PENDING"
        ticket.delivery_info = "تذكرة جاهزة للطباعة"
    else:
        ticket.delivery_status = "ON_SCREEN"
        ticket.delivery_info = None
    if target_info and mode == CategoryRouting.PRINT:
        ticket.delivery_info = (ticket.delivery_info or "") + f" ({target_info})"
    return ticket


def _format_ticket_message(
    sale: Sale, section_name: str, lines: Iterable[SaleLine]
) -> str:
    head = f"طلب جديد — {section_name}\nفاتورة #{sale.id}"
    if sale.table is not None:
        head += f"\nالطاولة: {sale.table.name_ar}"
    body_lines = []
    for ln in lines:
        if ln.product is None:
            continue
        body_lines.append(f"• {ln.product.name_ar} × {format_qty_plain(ln.quantity)}")
    body = "\n".join(body_lines) if body_lines else "(لا بنود)"
    return f"{head}\n{body}"


def _send_whatsapp(target: str, message: str) -> tuple[bool, str]:
    url = (target or "").strip()
    if not url.startswith(("http://", "https://")):
        return False, f"غير مُعدّ: {url or '—'}"
    try:
        payload = json.dumps({"text": message}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            return True, f"OK ({resp.status})"
    except Exception as exc:
        log.warning("WhatsApp webhook failed: %s", exc)
        return False, f"FAIL: {exc}"


def _make_section_ticket(sale: Sale, section: KitchenSection) -> KitchenTicket:
    ticket = KitchenTicket(
        sale_id=sale.id,
        kitchen_section_id=section.id,
        kitchen_department_id=None,
        status=TicketStatus.PENDING,
        routing_mode=CategoryRouting.PRINT.value
        if section.printer_id
        else CategoryRouting.SCREEN.value,
        delivery_status="PENDING_PRINT" if section.printer_id else "ON_SCREEN",
        delivery_info="في انتظار الوكيل المحلي" if section.printer_id else None,
    )
    return ticket


def _enqueue_department_kitchen_print(
    db: Session,
    ticket: KitchenTicket,
    sale: Sale,
    dept: KitchenDepartment,
    lines: list[SaleLine],
) -> None:
    """طباعة تذكرة قسم — عبر طابعة الفئة أو طابعة الكاشير."""
    mode = _coerce_routing_mode(dept.routing_mode)
    if mode in (CategoryRouting.NONE, CategoryRouting.WHATSAPP):
        return
    root = None
    if ticket.root_category_id:
        root = db.get(ProductCategory, ticket.root_category_id)
    if root is None:
        for ln in lines:
            if ln.product and ln.product.category:
                root = resolve_root_category(db, ln.product.category)
                if root:
                    break
    if root is not None and root.kitchen_section_id:
        section = db.get(KitchenSection, root.kitchen_section_id)
        if section is not None and section.is_active:
            ticket.kitchen_section_id = section.id
            job = enqueue_kitchen_ticket_print(db, ticket, sale, section)
            if job is not None:
                ticket.delivery_status = "PENDING_PRINT"
                ticket.delivery_info = "مهمة طباعة في قائمة الانتظار"
                return
    job = enqueue_department_ticket_print(
        db, ticket, sale, dept_name=dept.name_ar, lines=lines
    )
    if job is not None:
        ticket.delivery_status = "PENDING_PRINT"
        ticket.delivery_info = "مهمة طباعة في قائمة الانتظار"


def _enqueue_root_category_print(
    db: Session,
    ticket: KitchenTicket,
    sale: Sale,
    root: ProductCategory,
) -> None:
    """طباعة تذكرة الفئة الجذر عبر قسم المطبخ المرتبط بالفئة."""
    if root.kitchen_section_id is None:
        return
    section = db.get(KitchenSection, root.kitchen_section_id)
    if section is None or not section.is_active:
        return
    ticket.kitchen_section_id = section.id
    job = enqueue_kitchen_ticket_print(db, ticket, sale, section)
    if job is not None:
        ticket.delivery_status = "PENDING_PRINT"
        ticket.delivery_info = "مهمة طباعة في قائمة الانتظار"


def create_tickets_for_sale(db: Session, sale_id: int) -> list[KitchenTicket]:
    """يُنشئ تذكرة لكل قسم مطبخ + مهمة طباعة للوكيل عند الحاجة."""
    from modules.sales.order_policy import load_order_policy
    from modules.sales.service import load_sale_with_lines

    if not load_order_policy(db).kitchen_workflow_enabled:
        return []

    sale = load_sale_with_lines(db, sale_id)
    if sale is None:
        return []
    created: list[KitchenTicket] = []

    section_groups = group_lines_by_kitchen_section(db, sale)
    for sec_id, lines in section_groups.items():
        section = db.get(KitchenSection, sec_id)
        if section is None or not section.is_active:
            continue
        existing = _ticket_exists(db, sale.id, kitchen_section_id=section.id)
        if existing is not None:
            created.append(existing)
            continue
        root = None
        for ln in lines:
            if ln.product and ln.product.category:
                root = resolve_root_category(db, ln.product.category)
                if root:
                    break
        ticket = _make_section_ticket(sale, section)
        ticket.root_category_id = root.id if root else None
        db.add(ticket)
        db.flush()
        job = enqueue_kitchen_ticket_print(db, ticket, sale, section)
        if job is not None:
            ticket.delivery_status = "PENDING_PRINT"
            ticket.delivery_info = "مهمة طباعة في قائمة الانتظار"
        created.append(ticket)
        try:
            from modules.notifications.marketing_hooks import emit_kitchen_ticket_created

            emit_kitchen_ticket_created(
                db,
                sale=sale,
                ticket_id=ticket.id,
                section_name=section.name_ar if section else "",
            )
        except Exception:  # noqa: BLE001
            pass

    for dept_id, lines in _group_lines_by_department(db, sale).items():
        if dept_id in section_groups or db.get(KitchenSection, dept_id) is not None:
            continue
        dept = db.get(KitchenDepartment, dept_id)
        if dept is None or not dept.is_active or _coerce_routing_mode(dept.routing_mode) == CategoryRouting.NONE:
            continue
        existing = _ticket_exists(db, sale.id, department_id=dept.id)
        if existing is not None:
            created.append(existing)
            continue
        root = None
        for ln in lines:
            if ln.product and ln.product.category:
                root = resolve_root_category(db, ln.product.category)
                if root:
                    break
        ticket = _make_ticket(sale, department=dept, root=root)
        db.add(ticket)
        db.flush()
        _enqueue_department_kitchen_print(db, ticket, sale, dept, lines)
        created.append(ticket)
        try:
            from modules.notifications.marketing_hooks import emit_kitchen_ticket_created

            emit_kitchen_ticket_created(
                db,
                sale=sale,
                ticket_id=ticket.id,
                section_name=dept.name_ar if dept else "",
            )
        except Exception:  # noqa: BLE001
            pass

    for root_id, lines in _group_lines_by_root(db, sale).items():
        root = db.get(ProductCategory, root_id)
        if root is None or _coerce_routing_mode(root.routing_mode) == CategoryRouting.NONE:
            continue
        existing = _ticket_exists(db, sale.id, root_category_id=root.id)
        if existing is not None:
            created.append(existing)
            continue
        ticket = _make_ticket(sale, root=root)
        db.add(ticket)
        db.flush()
        mode = _coerce_routing_mode(root.routing_mode)
        if mode in (CategoryRouting.PRINT, CategoryRouting.SCREEN):
            _enqueue_root_category_print(db, ticket, sale, root)
        created.append(ticket)
        try:
            from modules.notifications.marketing_hooks import emit_kitchen_ticket_created

            emit_kitchen_ticket_created(
                db,
                sale=sale,
                ticket_id=ticket.id,
                section_name=root.name_ar if root else "",
            )
        except Exception:  # noqa: BLE001
            pass

    if not created:
        existing = list(
            db.scalars(select(KitchenTicket).where(KitchenTicket.sale_id == sale.id)).all()
        )
        if existing:
            return existing
        from modules.kds.sections_seed import ensure_default_kitchen_sections

        sections = [s for s in ensure_default_kitchen_sections(db) if s.is_active]
        if not sections:
            sections = list_active_kitchen_sections(db)
        if sections:
            section = next((s for s in sections if (s.code or "").upper() != "DRINKS"), sections[0])
            ticket = _make_section_ticket(sale, section)
            ticket.delivery_info = (
                (ticket.delivery_info + " — " if ticket.delivery_info else "")
                + "تذكرة عامة: لم يتم العثور على ربط قسم للأصناف"
            )
            db.add(ticket)
            db.flush()
            created.append(ticket)
            log.warning(
                "created fallback KDS ticket for sale %s in section %s; no item routing matched",
                sale.id,
                section.id,
            )
            try:
                from modules.notifications.marketing_hooks import emit_kitchen_ticket_created

                emit_kitchen_ticket_created(
                    db,
                    sale=sale,
                    ticket_id=ticket.id,
                    section_name=section.name,
                )
            except Exception:  # noqa: BLE001
                pass

    db.flush()
    return created


def repair_missing_tickets_for_sent_sales(db: Session, *, limit: int = 50) -> int:
    """ينشئ تذاكر KDS للطلبات التي أُرسلت للمطبخ ولم تُنشأ لها تذاكر.

    يحدث هذا غالباً بعد نقل قاعدة بيانات لا تحتوي ربط أقسام كامل، أو عند وجود
    مسار قديم يعلّم الطلب كمُرسل دون إنشاء تذاكر. الإصلاح محدود لتفادي أي حمل.
    """
    sale_ids = list(
        db.scalars(
            select(Sale.id)
            .where(Sale.sent_to_kitchen_at.isnot(None))
            .where(
                Sale.id.not_in(
                    select(KitchenTicket.sale_id).where(KitchenTicket.sale_id.isnot(None))
                )
            )
            .order_by(Sale.sent_to_kitchen_at.desc())
            .limit(max(1, int(limit)))
        ).all()
    )
    repaired = 0
    for sale_id in sale_ids:
        if create_tickets_for_sale(db, int(sale_id)):
            repaired += 1
    return repaired


def _supplement_snapshot(items: list[tuple[SaleLine, Decimal]]) -> str:
    rows = []
    for ln, delta in items:
        if ln.product is None or delta <= 0:
            continue
        rows.append(
            {
                "name_ar": ln.product.name_ar,
                "qty": format_qty_plain(delta),
                "note": (getattr(ln, "line_note", None) or "").strip() or None,
            }
        )
    return json.dumps(rows, ensure_ascii=False)


def create_supplement_tickets_for_sale(db: Session, sale_id: int) -> list[KitchenTicket]:
    """تذاكر تكميل جديدة (لا تُدمَج مع التذكرة الأصلية) للبنود الزائدة فقط."""
    from modules.sales.service import line_kitchen_pending_qty, load_sale_with_lines

    sale = load_sale_with_lines(db, sale_id)
    if sale is None or sale.sent_to_kitchen_at is None:
        return []

    pending: list[tuple[SaleLine, Decimal]] = []
    for ln in sale.lines:
        delta = line_kitchen_pending_qty(ln)
        if delta > 0:
            pending.append((ln, delta))
    if not pending:
        return []

    created: list[KitchenTicket] = []
    section_groups: dict[int, list[tuple[SaleLine, Decimal]]] = defaultdict(list)
    for ln, delta in pending:
        if ln.product is None:
            continue
        sec_id = resolve_kitchen_section_id(db, ln.product)
        if sec_id is not None:
            section_groups[sec_id].append((ln, delta))

    for sec_id, items in section_groups.items():
        section = db.get(KitchenSection, sec_id)
        if section is None or not section.is_active:
            continue
        root = None
        for ln, _delta in items:
            if ln.product and ln.product.category:
                root = resolve_root_category(db, ln.product.category)
                if root:
                    break
        ticket = _make_section_ticket(sale, section)
        ticket.is_supplement = True
        ticket.root_category_id = root.id if root else None
        ticket.supplement_lines_json = _supplement_snapshot(items)
        db.add(ticket)
        db.flush()
        job = enqueue_supplement_kitchen_ticket_print(db, ticket, sale, section, items)
        if job is not None:
            ticket.delivery_status = "PENDING_PRINT"
            ticket.delivery_info = "تكميل — مهمة طباعة في قائمة الانتظار"
        created.append(ticket)

    dept_groups: dict[int, list[tuple[SaleLine, Decimal]]] = defaultdict(list)
    for ln, delta in pending:
        if ln.product is None or ln.product.kitchen_department_id is None:
            continue
        dept_id = ln.product.kitchen_department_id
        if dept_id in section_groups or db.get(KitchenSection, dept_id) is not None:
            continue
        dept_groups[dept_id].append((ln, delta))

    for dept_id, items in dept_groups.items():
        dept = db.get(KitchenDepartment, dept_id)
        if dept is None or not dept.is_active or _coerce_routing_mode(dept.routing_mode) == CategoryRouting.NONE:
            continue
        root = None
        for ln, _delta in items:
            if ln.product and ln.product.category:
                root = resolve_root_category(db, ln.product.category)
                if root:
                    break
        ticket = _make_ticket(sale, department=dept, root=root)
        ticket.is_supplement = True
        ticket.supplement_lines_json = _supplement_snapshot(items)
        db.add(ticket)
        db.flush()
        mode = _coerce_routing_mode(dept.routing_mode)
        if mode == CategoryRouting.WHATSAPP:
            target = dept.routing_target
            if target:
                msg_lines = [
                    f"• {ln.product.name_ar} × {delta}"
                    for ln, delta in items
                    if ln.product is not None
                ]
                msg = (
                    f"تكميل — {dept.name_ar}\nتابع للطلب #{sale.id}\n"
                    + ("\n".join(msg_lines) if msg_lines else "(لا بنود)")
                )
                ok, info = _send_whatsapp(target, msg)
                ticket.delivery_status = "SENT" if ok else "ERROR"
                ticket.delivery_info = info[:500]
        elif mode in (CategoryRouting.PRINT, CategoryRouting.SCREEN):
            if root is not None:
                _enqueue_root_category_supplement_print(db, ticket, sale, root, items)
            else:
                job = enqueue_department_supplement_print(
                    db,
                    ticket,
                    sale,
                    dept_name=dept.name_ar,
                    items=items,
                )
                if job is not None:
                    ticket.delivery_status = "PENDING_PRINT"
                    ticket.delivery_info = "تكميل — مهمة طباعة في قائمة الانتظار"
        created.append(ticket)

    root_groups: dict[int, list[tuple[SaleLine, Decimal]]] = defaultdict(list)
    for ln, delta in pending:
        if ln.product is None:
            continue
        if ln.product.kitchen_department_id is not None:
            continue
        if resolve_kitchen_section_id(db, ln.product) is not None:
            continue
        root = resolve_root_category(db, ln.product.category)
        if root is None:
            continue
        root_groups[root.id].append((ln, delta))

    for root_id, items in root_groups.items():
        root = db.get(ProductCategory, root_id)
        if root is None or _coerce_routing_mode(root.routing_mode) == CategoryRouting.NONE:
            continue
        ticket = _make_ticket(sale, root=root)
        ticket.is_supplement = True
        ticket.supplement_lines_json = _supplement_snapshot(items)
        db.add(ticket)
        db.flush()
        mode = _coerce_routing_mode(root.routing_mode)
        if mode in (CategoryRouting.PRINT, CategoryRouting.SCREEN):
            _enqueue_root_category_supplement_print(db, ticket, sale, root, items)
        created.append(ticket)

    db.flush()
    return created


def create_void_kitchen_tickets(
    db: Session,
    sale: Sale,
    void_items: list[tuple[SaleLine, Decimal]],
    reason: str,
) -> list[KitchenTicket]:
    """تذاكر إلغاء/مسح للمطبخ — تُطبَع مثل التكميل مع بادئة «إلغاء»."""
    if not void_items:
        return []
    reason = (reason or "").strip()
    created: list[KitchenTicket] = []
    section_groups: dict[int, list[tuple[SaleLine, Decimal]]] = defaultdict(list)
    for ln, delta in void_items:
        if ln.product is None or delta <= 0:
            continue
        sec_id = resolve_kitchen_section_id(db, ln.product)
        if sec_id is not None:
            section_groups[sec_id].append((ln, delta))

    for sec_id, items in section_groups.items():
        section = db.get(KitchenSection, sec_id)
        if section is None or not section.is_active:
            continue
        root = None
        for ln, _delta in items:
            if ln.product and ln.product.category:
                root = resolve_root_category(db, ln.product.category)
                if root:
                    break
        ticket = _make_section_ticket(sale, section)
        ticket.is_supplement = True
        ticket.root_category_id = root.id if root else None
        ticket.supplement_lines_json = _supplement_snapshot(items)
        ticket.delivery_info = f"إلغاء — {reason[:200]}"
        db.add(ticket)
        db.flush()
        job = enqueue_supplement_kitchen_ticket_print(db, ticket, sale, section, items)
        if job is not None:
            ticket.delivery_status = "PENDING_PRINT"
            ticket.delivery_info = f"إلغاء — مهمة طباعة ({reason[:120]})"
        created.append(ticket)

    dept_groups: dict[int, list[tuple[SaleLine, Decimal]]] = defaultdict(list)
    for ln, delta in void_items:
        if ln.product is None or ln.product.kitchen_department_id is None:
            continue
        dept_id = ln.product.kitchen_department_id
        if dept_id in section_groups or db.get(KitchenSection, dept_id) is not None:
            continue
        dept_groups[dept_id].append((ln, delta))

    for dept_id, items in dept_groups.items():
        dept = db.get(KitchenDepartment, dept_id)
        if dept is None or not dept.is_active or _coerce_routing_mode(dept.routing_mode) == CategoryRouting.NONE:
            continue
        root = None
        for ln, _delta in items:
            if ln.product and ln.product.category:
                root = resolve_root_category(db, ln.product.category)
                if root:
                    break
        ticket = _make_ticket(sale, department=dept, root=root)
        ticket.is_supplement = True
        ticket.supplement_lines_json = _supplement_snapshot(items)
        ticket.delivery_info = f"إلغاء — {reason[:200]}"
        db.add(ticket)
        db.flush()
        mode = _coerce_routing_mode(dept.routing_mode)
        if mode == CategoryRouting.WHATSAPP:
            target = dept.routing_target
            if target:
                msg_lines = [
                    f"• {ln.product.name_ar} × {delta} (إلغاء)"
                    for ln, delta in items
                    if ln.product is not None
                ]
                msg = (
                    f"⛔ إلغاء — {dept.name_ar}\nطلب #{sale.id}\nسبب: {reason}\n"
                    + ("\n".join(msg_lines) if msg_lines else "(لا بنود)")
                )
                ok, info = _send_whatsapp(target, msg)
                ticket.delivery_status = "SENT" if ok else "ERROR"
                ticket.delivery_info = info[:500]
        elif mode in (CategoryRouting.PRINT, CategoryRouting.SCREEN):
            if root is not None:
                _enqueue_root_category_supplement_print(db, ticket, sale, root, items)
            else:
                job = enqueue_department_supplement_print(
                    db,
                    ticket,
                    sale,
                    dept_name=dept.name_ar,
                    items=items,
                )
                if job is not None:
                    ticket.delivery_status = "PENDING_PRINT"
                    ticket.delivery_info = f"إلغاء — مهمة طباعة ({reason[:120]})"
        created.append(ticket)

    root_groups: dict[int, list[tuple[SaleLine, Decimal]]] = defaultdict(list)
    for ln, delta in void_items:
        if ln.product is None:
            continue
        if ln.product.kitchen_department_id is not None:
            continue
        if resolve_kitchen_section_id(db, ln.product) is not None:
            continue
        root = resolve_root_category(db, ln.product.category)
        if root is None:
            continue
        root_groups[root.id].append((ln, delta))

    for root_id, items in root_groups.items():
        root = db.get(ProductCategory, root_id)
        if root is None or _coerce_routing_mode(root.routing_mode) == CategoryRouting.NONE:
            continue
        ticket = _make_ticket(sale, root=root)
        ticket.is_supplement = True
        ticket.supplement_lines_json = _supplement_snapshot(items)
        ticket.delivery_info = f"إلغاء — {reason[:200]}"
        db.add(ticket)
        db.flush()
        mode = _coerce_routing_mode(root.routing_mode)
        if mode in (CategoryRouting.PRINT, CategoryRouting.SCREEN):
            _enqueue_root_category_supplement_print(db, ticket, sale, root, items)
        created.append(ticket)

    db.flush()
    if created:
        try:
            from modules.notifications.marketing_hooks import emit_kitchen_item_cancelled

            emit_kitchen_item_cancelled(
                db,
                sale=sale,
                reason=reason,
                item_count=len(void_items),
            )
        except Exception:  # noqa: BLE001
            pass
    return created


def cancel_sale_kitchen_tickets(db: Session, sale_id: int) -> None:
    """يُغلق التذاكر المفتوحة على KDS عند إلغاء الفاتورة."""
    for t in db.scalars(
        select(KitchenTicket).where(KitchenTicket.sale_id == sale_id)
    ).all():
        if t.status not in (TicketStatus.SERVED, TicketStatus.CANCELLED):
            t.status = TicketStatus.CANCELLED
    db.flush()


def _enqueue_root_category_supplement_print(
    db: Session,
    ticket: KitchenTicket,
    sale: Sale,
    root: ProductCategory,
    items: list[tuple[SaleLine, Decimal]],
) -> None:
    if root.kitchen_section_id is None:
        return
    section = db.get(KitchenSection, root.kitchen_section_id)
    if section is None or not section.is_active:
        return
    ticket.kitchen_section_id = section.id
    job = enqueue_supplement_kitchen_ticket_print(db, ticket, sale, section, items)
    if job is not None:
        ticket.delivery_status = "PENDING_PRINT"
        ticket.delivery_info = "تكميل — مهمة طباعة في قائمة الانتظار"


def dispatch_pending_whatsapp_tickets(sale_id: int) -> None:
    from infra.background import with_db

    with with_db() as db:
        tickets = list(
            db.scalars(
                select(KitchenTicket).where(
                    KitchenTicket.sale_id == sale_id,
                    KitchenTicket.delivery_status == "PENDING_SEND",
                )
            ).all()
        )
        if not tickets:
            return
        sale = db.get(Sale, sale_id)
        if sale is None:
            return
        for t in tickets:
            target = None
            section_name = "—"
            lines: list[SaleLine] = []
            if t.kitchen_department_id:
                dept = db.get(KitchenDepartment, t.kitchen_department_id)
                if dept:
                    target = dept.routing_target
                    section_name = dept.name_ar
                for ln in sale.lines:
                    if ln.product and ln.product.kitchen_department_id == t.kitchen_department_id:
                        lines.append(ln)
            elif t.root_category_id:
                root = db.get(ProductCategory, t.root_category_id)
                if root:
                    target = root.routing_target
                    section_name = root.name_ar
                for ln in sale.lines:
                    if ln.product is None:
                        continue
                    rt = resolve_root_category(db, ln.product.category)
                    if rt and rt.id == t.root_category_id:
                        lines.append(ln)
            if not target:
                t.delivery_status = "ERROR"
                t.delivery_info = "هدف الإرسال غير مُعدّ"
                continue
            msg = _format_ticket_message(sale, section_name, lines)
            ok, info = _send_whatsapp(target, msg)
            t.delivery_status = "SENT" if ok else "ERROR"
            t.delivery_info = info[:500]
        db.commit()


def _age_minutes(ticket: KitchenTicket) -> int:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    created = ticket.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0, int((now - created).total_seconds() // 60))


def _section_label(ticket: KitchenTicket) -> str:
    if ticket.kitchen_section:
        return ticket.kitchen_section.name
    if ticket.kitchen_department:
        return ticket.kitchen_department.name_ar
    if ticket.root_category:
        return ticket.root_category.name_ar
    return "—"


def _lines_for_ticket(db: Session, ticket: KitchenTicket, sale: Sale) -> list[TicketLineRow]:
    extra = complimentary_line_for_ticket(db, ticket, sale)
    if ticket.kitchen_section_id:
        rows = [
            _ticket_row_from_line(ln)
            for ln in lines_for_section_ticket(db, ticket, sale)
            if ln.product
        ]
        if rows:
            if extra is not None:
                rows.append(extra)
            return rows
        if ticket.root_category_id is None and ticket.kitchen_department_id is None:
            rows = [
                _ticket_row_from_line(ln)
                for ln in sale.lines
                if ln.product is not None
            ]
            if extra is not None:
                rows.append(extra)
            return rows
        return rows
    rows: list[TicketLineRow] = []
    for ln in sale.lines:
        if ln.product is None:
            continue
        if ticket.kitchen_department_id:
            if ln.product.kitchen_department_id != ticket.kitchen_department_id:
                continue
        elif ticket.root_category_id:
            root = resolve_root_category(db, ln.product.category)
            if root is None or root.id != ticket.root_category_id:
                continue
        else:
            continue
        rows.append(_ticket_row_from_line(ln))
    if rows and extra is not None:
        rows.append(extra)
    return rows


_OPEN_TICKET_STATUSES = (
    TicketStatus.PENDING,
    TicketStatus.IN_PROGRESS,
    TicketStatus.READY,
)


def list_active_kitchen_sections(db: Session) -> list[KitchenSection]:
    return list(
        db.scalars(
            select(KitchenSection)
            .where(KitchenSection.is_active.is_(True))
            .order_by(KitchenSection.name, KitchenSection.id)
        ).all()
    )


def kds_open_counts(db: Session) -> dict[str, int | dict[int, int]]:
    """إجمالي التذاكر المفتوحة + توزيعها حسب القسم (للشارات على التبويبات)."""
    base = KitchenTicket.status.in_(_OPEN_TICKET_STATUSES)
    total = int(db.scalar(select(func.count(KitchenTicket.id)).where(base)) or 0)
    by_section: dict[int, int] = {}
    for sid, n in db.execute(
        select(KitchenTicket.kitchen_section_id, func.count(KitchenTicket.id))
        .where(base, KitchenTicket.kitchen_section_id.isnot(None))
        .group_by(KitchenTicket.kitchen_section_id)
    ).all():
        by_section[int(sid)] = int(n or 0)
    by_dept: dict[int, int] = {}
    for did, n in db.execute(
        select(KitchenTicket.kitchen_department_id, func.count(KitchenTicket.id))
        .where(base, KitchenTicket.kitchen_department_id.isnot(None))
        .group_by(KitchenTicket.kitchen_department_id)
    ).all():
        by_dept[int(did)] = int(n or 0)
    by_cat: dict[int, int] = {}
    for cid, n in db.execute(
        select(KitchenTicket.root_category_id, func.count(KitchenTicket.id))
        .where(base, KitchenTicket.root_category_id.isnot(None))
        .group_by(KitchenTicket.root_category_id)
    ).all():
        by_cat[int(cid)] = int(n or 0)
    unassigned = int(
        db.scalar(
            select(func.count(KitchenTicket.id)).where(
                base,
                KitchenTicket.kitchen_section_id.is_(None),
                KitchenTicket.kitchen_department_id.is_(None),
                KitchenTicket.root_category_id.is_(None),
            )
        )
        or 0
    )
    return {
        "total": total,
        "by_section": by_section,
        "by_dept": by_dept,
        "by_cat": by_cat,
        "unassigned": unassigned,
    }


def kds_pick_default_section(
    db: Session,
    *,
    kitchen_sections: list[KitchenSection],
    departments: list[KitchenDepartment],
    legacy_roots: list[ProductCategory],
    open_counts: dict[str, int | dict[int, int]] | None = None,
) -> tuple[str | None, int | None]:
    """يفتح أول تبويب فيه تذاكر مفتوحة (حتى لا تظهر شاشة فارغة والعداد 58)."""
    counts = open_counts if open_counts is not None else kds_open_counts(db)
    best_kind: str | None = None
    best_id: int | None = None
    best_n = 0

    def _consider(kind: str, sid: int, n: int) -> None:
        nonlocal best_kind, best_id, best_n
        if n > best_n:
            best_n = n
            best_kind = kind
            best_id = sid

    for s in kitchen_sections:
        _consider("sec", s.id, counts["by_section"].get(s.id, 0))
    for d in departments:
        _consider("dept", d.id, counts["by_dept"].get(d.id, 0))
    for c in legacy_roots:
        _consider("cat", c.id, counts["by_cat"].get(c.id, 0))
    if best_n > 0 and best_kind and best_id is not None:
        return best_kind, best_id
    if kitchen_sections:
        return "sec", kitchen_sections[0].id
    if departments:
        return "dept", departments[0].id
    if legacy_roots:
        return "cat", legacy_roots[0].id
    if counts["unassigned"] > 0:
        return "all", 0
    return None, None


def list_active_departments(db: Session) -> list[KitchenDepartment]:
    return list(
        db.scalars(
            select(KitchenDepartment)
            .where(
                KitchenDepartment.is_active.is_(True),
                KitchenDepartment.routing_mode != CategoryRouting.NONE,
            )
            .order_by(KitchenDepartment.venue, KitchenDepartment.sort_order, KitchenDepartment.id)
        ).all()
    )


def list_legacy_routed_roots(db: Session) -> list[ProductCategory]:
    return list(
        db.scalars(
            select(ProductCategory)
            .where(
                ProductCategory.parent_id.is_(None),
                ProductCategory.routing_mode != CategoryRouting.NONE,
            )
            .order_by(ProductCategory.sort_order, ProductCategory.id)
        ).all()
    )


def list_tickets(
    db: Session,
    *,
    kitchen_section_id: int | None = None,
    kitchen_department_id: int | None = None,
    root_category_id: int | None = None,
    only_open: bool = True,
    archived: bool = False,
    rejected: bool = False,
    limit: int = 60,
) -> list[TicketView]:
    stmt = select(KitchenTicket).order_by(KitchenTicket.id.desc())
    if rejected:
        stmt = stmt.where(
            KitchenTicket.archived_at.is_(None),
            KitchenTicket.status == TicketStatus.CANCELLED,
            KitchenTicket.delivery_status == "REJECTED",
        )
        only_open = False
    elif archived:
        stmt = stmt.where(KitchenTicket.archived_at.isnot(None))
    else:
        stmt = stmt.where(KitchenTicket.archived_at.is_(None))
    if kitchen_section_id is not None:
        stmt = stmt.where(KitchenTicket.kitchen_section_id == kitchen_section_id)
    if kitchen_department_id is not None:
        stmt = stmt.where(KitchenTicket.kitchen_department_id == kitchen_department_id)
    if root_category_id is not None:
        stmt = stmt.where(KitchenTicket.root_category_id == root_category_id)
    if only_open:
        stmt = stmt.where(
            KitchenTicket.status.in_(
                [TicketStatus.PENDING, TicketStatus.IN_PROGRESS, TicketStatus.READY]
            )
        )
    stmt = stmt.limit(limit).options(
        selectinload(KitchenTicket.sale).selectinload(Sale.lines).selectinload(SaleLine.product),
        selectinload(KitchenTicket.sale).selectinload(Sale.table),
        selectinload(KitchenTicket.kitchen_section),
        selectinload(KitchenTicket.kitchen_department),
        selectinload(KitchenTicket.root_category),
    )
    out: list[TicketView] = []
    for t in db.scalars(stmt).all():
        sale = t.sale
        if sale is None:
            continue
        rows = _lines_for_ticket(db, t, sale)
        table_label = sale.table.name_ar if sale.table is not None else None
        out.append(
            TicketView(
                ticket=t,
                sale=sale,
                lines=rows,
                table_label=table_label,
                age_minutes=_age_minutes(t),
                section_label=_section_label(t),
            )
        )
    return out


def archive_served_tickets_before(db: Session, cutoff, *, user_id: int | None = None) -> int:
    """أرشفة تذاكر KDS المنفذة حتى تاريخ محدد دون حذفها."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    result = db.execute(
        update(KitchenTicket)
        .where(
            KitchenTicket.archived_at.is_(None),
            KitchenTicket.status == TicketStatus.SERVED,
            KitchenTicket.served_at.isnot(None),
            KitchenTicket.served_at < cutoff,
        )
        .values(archived_at=now)
    )
    return int(result.rowcount or 0)


def archived_ticket_count(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count(KitchenTicket.id)).where(KitchenTicket.archived_at.isnot(None))
        )
        or 0
    )


def rejected_ticket_count(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count(KitchenTicket.id)).where(
                KitchenTicket.archived_at.is_(None),
                KitchenTicket.status == TicketStatus.CANCELLED,
                KitchenTicket.delivery_status == "REJECTED",
            )
        )
        or 0
    )


def build_master_ticket_sections(db: Session, sale: Sale) -> list[MasterSectionRow]:
    """كل الأقسام والبنود في ورقة تجميع واحدة."""
    by_section: dict[int, list[SaleLine]] = group_lines_by_kitchen_section(db, sale)
    sections: list[MasterSectionRow] = []
    for sec_id in sorted(by_section.keys(), key=lambda i: i):
        sec = db.get(KitchenSection, sec_id)
        name = sec.name if sec else f"قسم #{sec_id}"
        sections.append(
            MasterSectionRow(
                name_ar=name,
                lines=[
                    _ticket_row_from_line(ln)
                    for ln in by_section[sec_id]
                    if ln.product
                ],
            )
        )
    by_dept: dict[int, list[SaleLine]] = _group_lines_by_department(db, sale)
    by_root: dict[int, list[SaleLine]] = _group_lines_by_root(db, sale)
    for dept_id in sorted(by_dept.keys(), key=lambda i: i):
        if dept_id in by_section:
            continue
        dept = db.get(KitchenDepartment, dept_id)
        name = dept.name_ar if dept else f"قسم #{dept_id}"
        sections.append(
            MasterSectionRow(
                name_ar=name,
                lines=[
                    _ticket_row_from_line(ln)
                    for ln in by_dept[dept_id]
                    if ln.product
                ],
            )
        )
    for root_id in sorted(by_root.keys(), key=lambda i: i):
        root = db.get(ProductCategory, root_id)
        name = root.name_ar if root else f"فئة #{root_id}"
        sections.append(
            MasterSectionRow(
                name_ar=name,
                lines=[
                    _ticket_row_from_line(ln)
                    for ln in by_root[root_id]
                    if ln.product
                ],
            )
        )
    unassigned: list[TicketLineRow] = []
    for ln in sale.lines:
        if ln.product is None:
            continue
        if ln.product.kitchen_department_id is not None:
            continue
        if resolve_kitchen_section_id(db, ln.product) is not None:
            continue
        root = resolve_root_category(db, ln.product.category)
        if root is not None and root.routing_mode != CategoryRouting.NONE:
            continue
        unassigned.append(_ticket_row_from_line(ln))
    if unassigned:
        sections.append(MasterSectionRow(name_ar="أخرى", lines=unassigned))
    extra = complimentary_line_for_sale(db, sale)
    if extra is not None:
        sections.append(MasterSectionRow(name_ar="ضيافة الطاولة", lines=[extra]))
    return sections


def advance_ticket(db: Session, ticket_id: int) -> KitchenTicket | None:
    from datetime import datetime, timezone

    t = db.get(KitchenTicket, ticket_id)
    if t is None:
        return None
    now = datetime.now(timezone.utc)
    if t.status == TicketStatus.PENDING:
        t.status = TicketStatus.IN_PROGRESS
    elif t.status == TicketStatus.IN_PROGRESS:
        t.status = TicketStatus.SERVED
        t.served_at = now
    elif t.status == TicketStatus.READY:
        # تذاكر قديمة توقّفت عند «جاهز» قبل إلغاء خطوة التسليم في KDS
        t.status = TicketStatus.SERVED
        t.served_at = now
    db.flush()
    if t.sale is not None:
        from modules.sales.order_pipeline import sync_served_from_kds

        sync_served_from_kds(db, t.sale)
    return t


def reject_ticket(
    db: Session,
    ticket_id: int,
    *,
    reason: str = "",
    user_name: str = "",
) -> KitchenTicket | None:
    from datetime import datetime, timezone

    t = db.get(KitchenTicket, ticket_id)
    if t is None:
        return None
    now = datetime.now(timezone.utc)
    t.status = TicketStatus.CANCELLED
    t.served_at = now
    t.delivery_status = "REJECTED"
    reason = (reason or "").strip()
    user_name = (user_name or "").strip()
    bits = ["مرفوض من شاشة التجهيز"]
    if user_name:
        bits.append(f"بواسطة {user_name}")
    if reason:
        bits.append(f"السبب: {reason}")
    t.delivery_info = " — ".join(bits)[:500]
    db.flush()
    if t.sale is not None:
        from modules.sales.order_pipeline import sync_served_from_kds

        sync_served_from_kds(db, t.sale)
    return t


def reset_ticket(db: Session, ticket_id: int) -> KitchenTicket | None:
    t = db.get(KitchenTicket, ticket_id)
    if t is None:
        return None
    t.status = TicketStatus.PENDING
    t.served_at = None
    db.flush()
    return t
