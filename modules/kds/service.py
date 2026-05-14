"""خدمة شاشة المطبخ + إنشاء التذاكر عند إتمام البيع وإرسال الطلب."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

import json
import logging
import urllib.parse
import urllib.request

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.catalog.models import CategoryRouting, ProductCategory
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


def _group_lines_by_root(
    db: Session, sale: Sale
) -> dict[int, list[SaleLine]]:
    """يقسّم بنود الفاتورة حسب الفئة الجذر للمنتج."""
    out: dict[int, list[SaleLine]] = defaultdict(list)
    for line in sale.lines:
        if line.product is None:
            continue
        root = resolve_root_category(db, line.product.category)
        if root is None:
            continue
        out[root.id].append(line)
    return out


def _format_ticket_message(
    sale: Sale, root: ProductCategory, lines: Iterable[SaleLine]
) -> str:
    head = f"طلب جديد — {root.name_ar}\nفاتورة #{sale.id}"
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
    """يحاول إرسال رسالة عبر webhook بسيط (POST JSON).
    يدعم نمطين:
      1) رابط كامل http(s) — يرسل JSON {"text": "..."}.
      2) أي صيغة أخرى تُحفظ كملاحظة دون إرسال فعلي.
    """
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
    except Exception as exc:  # نتعمّد التساهل حتى لا نُفشل عملية البيع
        log.warning("WhatsApp webhook failed: %s", exc)
        return False, f"FAIL: {exc}"


def create_tickets_for_sale(db: Session, sale_id: int) -> list[KitchenTicket]:
    """يُنشئ تذاكر لكل قسم له توجيه نشط، ويرسل عبر القناة المحدّدة (إن أمكن)."""
    sale = db.get(Sale, sale_id)
    if sale is None:
        return []
    grouped = _group_lines_by_root(db, sale)
    created: list[KitchenTicket] = []
    for root_id, lines in grouped.items():
        root = db.get(ProductCategory, root_id)
        if root is None:
            continue
        if root.routing_mode == CategoryRouting.NONE:
            continue
        # نمنع تكرار التذكرة لنفس (sale, root)
        existing = db.execute(
            select(KitchenTicket).where(
                KitchenTicket.sale_id == sale.id,
                KitchenTicket.root_category_id == root.id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            created.append(existing)
            continue
        ticket = KitchenTicket(
            sale_id=sale.id,
            root_category_id=root.id,
            status=TicketStatus.PENDING,
            routing_mode=root.routing_mode.value,
            delivery_status="PENDING",
        )
        if root.routing_mode == CategoryRouting.WHATSAPP:
            # نحفظ التذكرة بحالة PENDING_SEND ونرسل في الخلفية حتى لا يتأخر الكاشير
            ticket.delivery_status = "PENDING_SEND"
            ticket.delivery_info = "في انتظار الإرسال"
        elif root.routing_mode == CategoryRouting.PRINT:
            ticket.delivery_status = "PENDING"
            ticket.delivery_info = "تذكرة جاهزة للطباعة"
        else:  # SCREEN
            ticket.delivery_status = "ON_SCREEN"
            ticket.delivery_info = None
        db.add(ticket)
        created.append(ticket)
    db.flush()
    return created


def dispatch_pending_whatsapp_tickets(sale_id: int) -> None:
    """يُرسل تذاكر WhatsApp المعلّقة لبيع معيّن في الخلفية.

    تُستدعى من خيط منفصل بعد إنشاء التذاكر؛ تستخدم سيشن SQLAlchemy جديدة
    خاصة بها (لا يجوز مشاركة سيشن بين خيوط).
    """
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
        for t in tickets:
            sale = t.sale
            root = t.root_category if hasattr(t, "root_category") else db.get(
                ProductCategory, t.root_category_id
            )
            if sale is None or root is None or not root.routing_target:
                t.delivery_status = "ERROR"
                t.delivery_info = "تذكرة غير قابلة للإرسال (بيع/قسم/هدف ناقص)"
                continue
            # نجمع البنود الخاصة بهذا القسم
            lines = []
            for ln in sale.lines:
                if ln.product is None:
                    continue
                rt = resolve_root_category(db, ln.product.category)
                if rt is not None and rt.id == root.id:
                    lines.append(ln)
            msg = _format_ticket_message(sale, root, lines)
            ok, info = _send_whatsapp(root.routing_target, msg)
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


def list_tickets(
    db: Session,
    *,
    root_category_id: int | None = None,
    only_open: bool = True,
    limit: int = 60,
) -> list[TicketView]:
    """يجلب تذاكر للعرض على شاشة المطبخ."""
    stmt = select(KitchenTicket).order_by(KitchenTicket.created_at.desc())
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
        # بنود هذا القسم فقط
        rows: list[TicketLineRow] = []
        for ln in sale.lines:
            if ln.product is None:
                continue
            root = resolve_root_category(db, ln.product.category)
            if root is None or root.id != t.root_category_id:
                continue
            rows.append(
                TicketLineRow(name_ar=ln.product.name_ar, qty=ln.quantity)
            )
        table_label = sale.table.name_ar if sale.table is not None else None
        out.append(
            TicketView(
                ticket=t,
                sale=sale,
                lines=rows,
                table_label=table_label,
                age_minutes=_age_minutes(t),
            )
        )
    return out


def advance_ticket(db: Session, ticket_id: int) -> KitchenTicket | None:
    """يتقدّم بالتذكرة في دورة الحياة: PENDING → IN_PROGRESS → READY → SERVED."""
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
