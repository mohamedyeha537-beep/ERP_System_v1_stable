"""خدمة شاشة المطبخ + إنشاء التذاكر عند إرسال الطلب للمطبخ."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

import json
import logging
import urllib.request

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.catalog.models import CategoryRouting, ProductCategory
from modules.kds.models import KitchenDepartment
from modules.sales.models import KitchenTicket, Sale, SaleLine, TicketStatus
from modules.sales.receipt_layout import resolve_root_category

log = logging.getLogger("kds")


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
) -> KitchenTicket | None:
    stmt = select(KitchenTicket).where(KitchenTicket.sale_id == sale_id)
    if department_id is not None:
        stmt = stmt.where(KitchenTicket.kitchen_department_id == department_id)
    if root_category_id is not None:
        stmt = stmt.where(KitchenTicket.root_category_id == root_category_id)
    return db.execute(stmt).scalar_one_or_none()


def _make_ticket(
    sale: Sale,
    *,
    department: KitchenDepartment | None = None,
    root: ProductCategory | None = None,
) -> KitchenTicket:
    if department is not None:
        mode = department.routing_mode
        target_info = department.routing_target
    elif root is not None:
        mode = root.routing_mode
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
        body_lines.append(f"• {ln.product.name_ar} × {ln.quantity}")
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


def create_tickets_for_sale(db: Session, sale_id: int) -> list[KitchenTicket]:
    """يُنشئ تذكرة لكل قسم مطبخ + تذاكر الفئات القديمة (للمنتجات بلا قسم)."""
    sale = db.get(Sale, sale_id)
    if sale is None:
        return []
    created: list[KitchenTicket] = []

    for dept_id, lines in _group_lines_by_department(db, sale).items():
        dept = db.get(KitchenDepartment, dept_id)
        if dept is None or not dept.is_active or dept.routing_mode == CategoryRouting.NONE:
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
        created.append(ticket)

    for root_id, lines in _group_lines_by_root(db, sale).items():
        root = db.get(ProductCategory, root_id)
        if root is None or root.routing_mode == CategoryRouting.NONE:
            continue
        existing = _ticket_exists(db, sale.id, root_category_id=root.id)
        if existing is not None:
            created.append(existing)
            continue
        ticket = _make_ticket(sale, root=root)
        db.add(ticket)
        created.append(ticket)

    db.flush()
    return created


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
    if ticket.kitchen_department:
        return ticket.kitchen_department.name_ar
    if ticket.root_category:
        return ticket.root_category.name_ar
    return "—"


def _lines_for_ticket(db: Session, ticket: KitchenTicket, sale: Sale) -> list[TicketLineRow]:
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
        rows.append(TicketLineRow(name_ar=ln.product.name_ar, qty=ln.quantity))
    return rows


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
    kitchen_department_id: int | None = None,
    root_category_id: int | None = None,
    only_open: bool = True,
    limit: int = 60,
) -> list[TicketView]:
    stmt = select(KitchenTicket).order_by(KitchenTicket.created_at.desc())
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
    stmt = stmt.limit(limit)
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


def build_master_ticket_sections(db: Session, sale: Sale) -> list[MasterSectionRow]:
    """كل الأقسام والبنود في ورقة تجميع واحدة."""
    by_dept: dict[int, list[SaleLine]] = _group_lines_by_department(db, sale)
    by_root: dict[int, list[SaleLine]] = _group_lines_by_root(db, sale)
    sections: list[MasterSectionRow] = []
    for dept_id in sorted(by_dept.keys(), key=lambda i: i):
        dept = db.get(KitchenDepartment, dept_id)
        name = dept.name_ar if dept else f"قسم #{dept_id}"
        sections.append(
            MasterSectionRow(
                name_ar=name,
                lines=[
                    TicketLineRow(name_ar=ln.product.name_ar, qty=ln.quantity)
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
                    TicketLineRow(name_ar=ln.product.name_ar, qty=ln.quantity)
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
        root = resolve_root_category(db, ln.product.category)
        if root is not None and root.routing_mode != CategoryRouting.NONE:
            continue
        unassigned.append(TicketLineRow(name_ar=ln.product.name_ar, qty=ln.quantity))
    if unassigned:
        sections.append(MasterSectionRow(name_ar="أخرى", lines=unassigned))
    return sections


def advance_ticket(db: Session, ticket_id: int) -> KitchenTicket | None:
    from datetime import datetime, timezone

    t = db.get(KitchenTicket, ticket_id)
    if t is None:
        return None
    if t.status == TicketStatus.PENDING:
        t.status = TicketStatus.IN_PROGRESS
    elif t.status == TicketStatus.IN_PROGRESS:
        t.status = TicketStatus.READY
    elif t.status == TicketStatus.READY:
        t.status = TicketStatus.SERVED
        t.served_at = datetime.now(timezone.utc)
    db.flush()
    return t


def reset_ticket(db: Session, ticket_id: int) -> KitchenTicket | None:
    t = db.get(KitchenTicket, ticket_id)
    if t is None:
        return None
    t.status = TicketStatus.PENDING
    t.served_at = None
    db.flush()
    return t
