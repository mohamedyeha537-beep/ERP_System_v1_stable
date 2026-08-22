"""طباعة المطبخ عبر print_jobs ووكيل محلي."""
from __future__ import annotations

import hashlib
import json
import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from infra.config import get_settings
from app.datetime_local import format_local_dt, now_local
from modules.catalog.models import Product, ProductCategory
from modules.printing.models import (
    KitchenSection,
    PayloadFormat,
    PrintAgent,
    PrintJob,
    PrintJobStatus,
    PrintJobType,
    Printer,
    PrinterConnectionType,
)
from modules.printing.receipt_text import build_sale_receipt_text
from modules.sales.models import KitchenTicket, Sale, SaleLine
from modules.sales.receipt_layout import resolve_root_category
from modules.settings.service import get_int


def hash_agent_token(plain: str) -> str:
    pepper = get_settings().secret_key
    return hashlib.sha256(f"{plain}{pepper}".encode("utf-8")).hexdigest()


def generate_agent_token() -> tuple[str, str]:
    plain = secrets.token_urlsafe(32)
    return plain, hash_agent_token(plain)


def authenticate_agent(db: Session, token: str | None) -> PrintAgent | None:
    if not token or not token.strip():
        return None
    h = hash_agent_token(token.strip())
    agent = db.execute(
        select(PrintAgent).where(
            PrintAgent.token_hash == h,
            PrintAgent.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if agent is None:
        return None
    agent.last_seen_at = datetime.now(timezone.utc)
    db.flush()
    return agent


def resolve_kitchen_section_id(db: Session, product: Product) -> int | None:
    if product.kitchen_section_id is not None:
        return product.kitchen_section_id
    if product.kitchen_department_id is not None:
        sec = db.execute(
            select(KitchenSection.id).where(
                KitchenSection.id == product.kitchen_department_id
            )
        ).scalar_one_or_none()
        if sec is not None:
            return int(sec)
    cat = product.category
    while cat is not None:
        if cat.kitchen_section_id is not None:
            return cat.kitchen_section_id
        if cat.parent_id is not None and cat.parent is None:
            cat = db.get(ProductCategory, cat.parent_id)
        else:
            cat = cat.parent
    from modules.kds.section_rules import suggest_section_code, section_id_by_code

    code = suggest_section_code(product.name_ar or "")
    if code:
        sid = section_id_by_code(db, code)
        if sid is not None:
            return sid
    return None


def group_lines_by_kitchen_section(db: Session, sale: Sale) -> dict[int, list[SaleLine]]:
    out: dict[int, list[SaleLine]] = defaultdict(list)
    for line in sale.lines:
        if line.product is None:
            continue
        sec_id = resolve_kitchen_section_id(db, line.product)
        if sec_id is None:
            continue
        out[sec_id].append(line)
    return out


def lines_for_section_ticket(
    db: Session, ticket: KitchenTicket, sale: Sale
) -> list[SaleLine]:
    if ticket.kitchen_section_id is None:
        return []
    sec_id = ticket.kitchen_section_id
    rows: list[SaleLine] = []
    for ln in sale.lines:
        if ln.product is None:
            continue
        if resolve_kitchen_section_id(db, ln.product) == sec_id:
            rows.append(ln)
    return rows


def build_kitchen_ticket_text(
    sale: Sale,
    section_name: str,
    lines: list[SaleLine],
    *,
    is_supplement: bool = False,
    line_qty_overrides: dict[int, Decimal] | None = None,
    complimentary_label: str | None = None,
) -> str:
    width = 32
    sep = "=" * width
    head = f"تكميل — {section_name}" if is_supplement else f"طلب مطبخ — {section_name}"
    meta: list[str] = []
    if is_supplement:
        meta.append(f"*** تابع للطلب #{sale.id} ***")
    meta.append(f"فاتورة #{sale.id}")
    if sale.table is not None:
        meta.append(f"طاولة: {sale.table.name_ar}")
    meta.append(now_local().strftime("%Y-%m-%d %H:%M"))
    body: list[str] = []
    for ln in lines:
        if ln.product is None:
            continue
        qty = (
            line_qty_overrides.get(ln.id, ln.quantity)
            if line_qty_overrides
            else ln.quantity
        )
        row = f"{ln.product.name_ar}  x{qty}"
        note = (getattr(ln, "line_note", None) or "").strip()
        if note:
            row = f"{row}\n  ↳ {note}"
        body.append(row)
    if complimentary_label:
        body.append(
            f"{complimentary_label}  x1\n"
            "  ↳ ضيافة للطاولة فقط — تخصم من المخزون ولا تظهر في فاتورة الزبون"
        )
    if not body:
        body.append("(لا بنود)")
    return "\n".join([sep, head, sep, *meta, "-", *body, sep, ""])


def _table_complimentary_label(
    db: Session, sale: Sale, ticket: KitchenTicket
) -> str | None:
    from modules.sales.order_policy import (
        load_order_policy,
        table_complimentary_product_id_for_sale,
    )

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
    product_id = table_complimentary_product_id_for_sale(sale, load_order_policy(db))
    if product_id is None:
        return None
    product = db.get(Product, product_id)
    if product is None or not product.is_active:
        return None
    return (product.name_ar or product.name_en or f"#{product.id}").strip()


def build_supplement_kitchen_ticket_text(
    sale: Sale,
    section_name: str,
    items: list[tuple[SaleLine, Decimal]],
) -> str:
    lines = [ln for ln, delta in items if delta > 0]
    overrides = {ln.id: delta for ln, delta in items if delta > 0}
    return build_kitchen_ticket_text(
        sale,
        section_name,
        lines,
        is_supplement=True,
        line_qty_overrides=overrides,
    )


def build_job_payload_for_printer(
    db: Session,
    printer: Printer,
    *,
    text: str,
    logo_url: str | None = None,
    image_b64: str | None = None,
) -> tuple[str, str]:
    if printer.connection_type in (
        PrinterConnectionType.LOCAL_AGENT.value,
        PrinterConnectionType.DIRECT_TCP.value,
    ):
        meta = {
            "local_printer_key": printer.local_printer_key,
            "ip_address": printer.ip_address,
            "port": printer.port,
            "paper_width": printer.paper_width,
        }
        if image_b64:
            body: dict = {
                "print_mode": "raster",
                "printer": meta,
                "text": "",
                "image_b64": image_b64,
            }
        else:
            body = {"text": text, "printer": meta}
            if logo_url:
                body["logo_url"] = logo_url
        payload = json.dumps(body, ensure_ascii=False)
        return PayloadFormat.JSON.value, payload
    return PayloadFormat.TEXT.value, text


def get_receipt_printer(db: Session, domain=None) -> Printer | None:
    from modules.settings.service import get_receipt_printer_id

    pid = get_receipt_printer_id(db, domain)
    if pid <= 0 and domain is not None:
        pid = get_receipt_printer_id(db, None)
    if pid <= 0:
        return None
    printer = db.get(Printer, pid)
    if printer is None or not printer.is_active:
        return None
    if printer.connection_type not in (
        PrinterConnectionType.LOCAL_AGENT.value,
        PrinterConnectionType.DIRECT_TCP.value,
    ):
        return None
    return printer


def receipt_silent_print_available(db: Session) -> bool:
    p = get_receipt_printer(db)
    if p is None:
        return False
    if p.connection_type == PrinterConnectionType.LOCAL_AGENT.value:
        return p.agent_id is not None
    return True


def enqueue_receipt_print(
    db: Session,
    *,
    sale: Sale | None = None,
    text: str = "",
    logo_url: str | None = None,
    image_b64: str | None = None,
) -> PrintJob:
    printer = get_receipt_printer(db)
    if printer is None:
        raise ValueError(
            "لم تُعيَّن طابعة كاشير للطباعة المباشرة. "
            "من الإعدادات → طابعة الفاتورة، أو /admin/printing."
        )
    if not image_b64 and not (text or "").strip():
        raise ValueError("لا يوجد محتوى للطباعة.")
    job = create_print_job(
        db,
        printer=printer,
        job_type=PrintJobType.RECEIPT,
        text=text,
        logo_url=logo_url,
        image_b64=image_b64,
    )
    if job is None:
        raise ValueError("تعذّر إنشاء مهمة الطباعة — تحقق من نوع اتصال الطابعة والوكيل.")
    return job


def enqueue_completed_sale_receipt(
    db: Session,
    sale: Sale,
    *,
    room_hint: str | None = None,
    guest_name: str | None = None,
    guest_phone: str | None = None,
) -> PrintJob | None:
    """مهمة طباعة فاتورة مكتملة — نص ESC/POS عبر الوكيل. None إذا الطباعة غير مهيّأة."""
    if not receipt_silent_print_available(db):
        return None
    from modules.authz.models import User
    from modules.customers.service import build_receipt_loyalty_context
    from modules.payments.service import get_sale_payment, list_sale_payments
    from modules.settings.service import get_setting

    store_name = get_setting(db, "store_name", "نقطة البيع")
    sp = get_sale_payment(db, sale.id)
    sale_payments = list_sale_payments(db, sale.id)
    from modules.gl.wallet_labels import label_from_info_map, wallet_gl_info_map

    gl_info = wallet_gl_info_map(db)
    method_names = [
        label_from_info_map(gl_info, p.method)
        for p in sale_payments
        if p.method is not None and p.amount > 0
    ]
    payment_method_name = " + ".join(dict.fromkeys(method_names)) if method_names else None
    if payment_method_name is None and sp is not None and sp.method is not None:
        payment_method_name = label_from_info_map(gl_info, sp.method)
    loyalty_ctx = build_receipt_loyalty_context(db, sale, sp)
    printer = get_receipt_printer(db)
    paper_w = printer.paper_width if printer else 80
    cu = db.get(User, sale.created_by_id) if sale.created_by_id else None
    cashier_name = cu.username if cu else ""
    text = build_sale_receipt_text(
        db,
        sale,
        store_name=store_name,
        title="فاتورة",
        is_prebill=False,
        cashier_name=cashier_name,
        room_hint=room_hint,
        guest_name=guest_name,
        guest_phone=guest_phone,
        payment_method_name=payment_method_name,
        loyalty_points_display=loyalty_ctx.get("loyalty_points_earned_display", "0"),
        loyalty_points_redeemed_display=loyalty_ctx.get(
            "loyalty_points_redeemed_display", "0"
        ),
        loyalty_redeem_dinar_display=loyalty_ctx.get("loyalty_redeem_dinar_display", "0"),
        loyalty_balance_display=loyalty_ctx.get("loyalty_balance_display", "0"),
        loyalty_show_footer=loyalty_ctx.get("loyalty_show_footer", False),
        loyalty_show_earned=loyalty_ctx.get("loyalty_show_earned", False),
        loyalty_points_earned_dinar_display=loyalty_ctx.get(
            "loyalty_points_earned_dinar_display", "0"
        ),
        receipt_show_payment_breakdown=loyalty_ctx.get(
            "receipt_show_payment_breakdown", False
        ),
        receipt_invoice_total_display=loyalty_ctx.get(
            "receipt_invoice_total_display"
        ),
        receipt_loyalty_discount_display=loyalty_ctx.get(
            "receipt_loyalty_discount_display"
        ),
        receipt_amount_paid_display=loyalty_ctx.get("receipt_amount_paid_display"),
        receipt_amount_customer_total_display=loyalty_ctx.get(
            "receipt_amount_customer_total_display"
        ),
        paper_width=paper_w,
    )
    from modules.branding.service import get_branding

    brand = get_branding(db)
    logo_url = (brand.get("print_logo_url") or brand.get("logo_url") or "").strip()
    return enqueue_receipt_print(db, sale=sale, text=text, logo_url=logo_url or None)


def thermal_paper_code(db: Session) -> str:
    """مقاس HTML للفاتورة الحرارية — حسب طابعة الكاشير."""
    p = get_receipt_printer(db)
    pw = int(p.paper_width if p else 80)
    return "80mm" if pw >= 80 else "58mm"


def checkout_receipt_redirect(
    db: Session,
    sale_id: int,
    *,
    room_hint: str | None = None,
    guest_name: str | None = None,
    guest_phone: str | None = None,
) -> str:
    """بعد التحصيل — العودة للكاشير ثم طباعة الفاتورة عبر نفس مسار زر «طباعة» (iframe + html2canvas)."""
    _ = db, guest_name, guest_phone
    url = f"/pos?ok=1&receipt={sale_id}"
    if room_hint is not None:
        url += "&room=1"
    return url


def create_print_job(
    db: Session,
    *,
    printer: Printer,
    job_type: PrintJobType,
    text: str,
    kitchen_ticket_id: int | None = None,
    logo_url: str | None = None,
    image_b64: str | None = None,
) -> PrintJob | None:
    if not printer.is_active:
        return None
    if printer.connection_type not in (
        PrinterConnectionType.LOCAL_AGENT.value,
        PrinterConnectionType.DIRECT_TCP.value,
    ):
        return None
    fmt, payload = build_job_payload_for_printer(
        db,
        printer,
        text=text,
        logo_url=logo_url,
        image_b64=image_b64,
    )
    job = PrintJob(
        printer_id=printer.id,
        kitchen_ticket_id=kitchen_ticket_id,
        job_type=job_type.value,
        status=PrintJobStatus.PENDING.value,
        payload_format=fmt,
        payload=payload,
        attempts=0,
    )
    db.add(job)
    db.flush()
    from modules.dashboard_notify.constants import PRINTING
    from modules.dashboard_notify.service import record_activity

    record_activity(
        db,
        PRINTING,
        event_type="print_job",
        ref_id=job.id,
        note=f"مهمة طباعة #{job.id}",
    )
    return job


def resolve_kitchen_section_printer(
    db: Session, section: KitchenSection
) -> Printer | None:
    """طابعة القسم، أو طابعة الكاشier كاحتياط عند الإرسال للتجهيز."""
    if section.printer_id:
        printer = db.get(Printer, section.printer_id)
        if printer is not None and printer.is_active:
            return printer
    return get_receipt_printer(db)


def enqueue_kitchen_ticket_print(
    db: Session,
    ticket: KitchenTicket,
    sale: Sale,
    section: KitchenSection,
) -> PrintJob | None:
    printer = resolve_kitchen_section_printer(db, section)
    if printer is None:
        return None
    lines = lines_for_section_ticket(db, ticket, sale)
    if not lines:
        return None
    text = build_kitchen_ticket_text(
        sale,
        section.name,
        lines,
        complimentary_label=_table_complimentary_label(db, sale, ticket),
    )
    return create_print_job(
        db,
        printer=printer,
        job_type=PrintJobType.KITCHEN_TICKET,
        text=text,
        kitchen_ticket_id=ticket.id,
    )


def enqueue_department_ticket_print(
    db: Session,
    ticket: KitchenTicket,
    sale: Sale,
    *,
    dept_name: str,
    lines: list[SaleLine],
) -> PrintJob | None:
    """طباعة تذكرة قسم مطبخ (legacy) — طابعة الكاشير إن لم تُربَط طابعة قسم."""
    if not lines:
        return None
    printer = get_receipt_printer(db)
    if printer is None:
        return None
    text = build_kitchen_ticket_text(
        sale,
        dept_name,
        lines,
        complimentary_label=_table_complimentary_label(db, sale, ticket),
    )
    return create_print_job(
        db,
        printer=printer,
        job_type=PrintJobType.KITCHEN_TICKET,
        text=text,
        kitchen_ticket_id=ticket.id,
    )


def enqueue_supplement_kitchen_ticket_print(
    db: Session,
    ticket: KitchenTicket,
    sale: Sale,
    section: KitchenSection,
    items: list[tuple[SaleLine, Decimal]],
) -> PrintJob | None:
    printer = resolve_kitchen_section_printer(db, section)
    if printer is None:
        return None
    filtered = [(ln, delta) for ln, delta in items if delta > 0]
    if not filtered:
        return None
    text = build_supplement_kitchen_ticket_text(sale, section.name, filtered)
    return create_print_job(
        db,
        printer=printer,
        job_type=PrintJobType.KITCHEN_TICKET,
        text=text,
        kitchen_ticket_id=ticket.id,
    )


def enqueue_department_supplement_print(
    db: Session,
    ticket: KitchenTicket,
    sale: Sale,
    *,
    dept_name: str,
    items: list[tuple[SaleLine, Decimal]],
) -> PrintJob | None:
    filtered = [(ln, delta) for ln, delta in items if delta > 0]
    if not filtered:
        return None
    printer = get_receipt_printer(db)
    if printer is None:
        return None
    text = build_supplement_kitchen_ticket_text(sale, dept_name, filtered)
    return create_print_job(
        db,
        printer=printer,
        job_type=PrintJobType.KITCHEN_TICKET,
        text=text,
        kitchen_ticket_id=ticket.id,
    )


def create_test_print_job(db: Session, printer_id: int) -> PrintJob:
    printer = db.get(Printer, printer_id)
    if printer is None:
        raise ValueError("الطابعة غير موجودة.")
    text = build_kitchen_ticket_text(
        _FakeSale(),
        f"اختبار — {printer.name}",
        [],
    )
    job = create_print_job(
        db,
        printer=printer,
        job_type=PrintJobType.TEST,
        text=text,
    )
    if job is None:
        raise ValueError("نوع اتصال الطابعة لا يدعم مهام الوكيل.")
    return job


class _FakeSale:
    id = 0
    table = None


def retry_print_job(db: Session, job_id: int) -> PrintJob:
    job = db.get(PrintJob, job_id)
    if job is None:
        raise ValueError("المهمة غير موجودة.")
    if job.status not in (
        PrintJobStatus.FAILED.value,
        PrintJobStatus.CANCELLED.value,
        PrintJobStatus.CLAIMED.value,
    ):
        raise ValueError(
            "إعادة المحاولة متاحة للمهام الفاشلة أو الملغاة أو العالقة (claimed)."
        )
    _reset_job_for_retry(job)
    db.flush()
    return job


def retry_all_failed_print_jobs(
    db: Session, *, printer_id: int | None = None
) -> int:
    stmt = select(PrintJob).where(
        PrintJob.status.in_(
            (PrintJobStatus.FAILED.value, PrintJobStatus.CLAIMED.value)
        )
    )
    if printer_id is not None:
        stmt = stmt.where(PrintJob.printer_id == printer_id)
    jobs = list(db.scalars(stmt).all())
    for job in jobs:
        _reset_job_for_retry(job)
    db.flush()
    return len(jobs)


def reclaim_stale_claimed_jobs(
    db: Session,
    *,
    older_than_seconds: int = 120,
    max_attempts_before_fail: int = 5,
) -> int:
    """يعيد المهام العالقة في claimed إلى pending (أو failed بعد محاولات كثيرة).

    يحدث عندما يتعطل الوكيل أثناء الطباعة ولا يُبلّغ printed/failed.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(30, int(older_than_seconds)))
    rows = list(
        db.scalars(
            select(PrintJob).where(
                PrintJob.status == PrintJobStatus.CLAIMED.value,
                PrintJob.claimed_at.isnot(None),
                PrintJob.claimed_at < cutoff,
            )
        ).all()
    )
    now = datetime.now(timezone.utc)
    n = 0
    for job in rows:
        attempts = int(job.attempts or 0)
        if attempts >= max_attempts_before_fail:
            job.status = PrintJobStatus.FAILED.value
            job.error_message = (
                f"علقت المهمة claimed أكثر من {older_than_seconds}ث "
                f"بعد {attempts} محاولات — راجع الوكيل والطابعة."
            )[:500]
            job.updated_at = now
        else:
            job.status = PrintJobStatus.PENDING.value
            job.claimed_by_agent_id = None
            job.claimed_at = None
            job.error_message = (
                f"أُعيدت للطابور تلقائياً بعد تعليق claimed "
                f"(>{older_than_seconds}ث)."
            )[:500]
            job.updated_at = now
        n += 1
    if n:
        db.flush()
    return n


def count_offline_print_agents(db: Session, *, older_than_seconds: int = 90) -> int:
    """وكلاء مفعّلون لم يُرسلوا heartbeat منذ مدة."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(30, int(older_than_seconds)))
    n = db.scalar(
        select(func.count(PrintAgent.id)).where(
            PrintAgent.is_active.is_(True),
            or_(
                PrintAgent.last_seen_at.is_(None),
                PrintAgent.last_seen_at < cutoff,
            ),
        )
    )
    return int(n or 0)


def cancel_open_print_jobs(db: Session) -> int:
    """إلغاء كل المهام التي قد يسحبها الوكيل أو تعطل الاختبار الحالي."""
    rows = list(
        db.scalars(
            select(PrintJob).where(
                PrintJob.status.in_(
                    (
                        PrintJobStatus.PENDING.value,
                        PrintJobStatus.CLAIMED.value,
                        PrintJobStatus.FAILED.value,
                    )
                )
            )
        ).all()
    )
    now = datetime.now(timezone.utc)
    for job in rows:
        job.status = PrintJobStatus.CANCELLED.value
        job.error_message = "تم تنظيف المهمة قبل اختبار الطباعة."
        job.updated_at = now
    db.flush()
    return len(rows)


def _reset_job_for_retry(job: PrintJob) -> None:
    job.status = PrintJobStatus.PENDING.value
    job.claimed_by_agent_id = None
    job.claimed_at = None
    job.printed_at = None
    job.error_message = None
    job.updated_at = datetime.now(timezone.utc)


def list_pending_jobs_for_agent(db: Session, agent: PrintAgent, *, limit: int = 20) -> list[PrintJob]:
    # استعادة مهام claimed العالقة قبل سحب الجديد — يمنع توقف الطباعة لأيام
    try:
        reclaim_stale_claimed_jobs(db, older_than_seconds=120)
    except Exception:  # noqa: BLE001
        pass
    printer_ids = list(
        db.scalars(
            select(Printer.id).where(
                Printer.agent_id == agent.id,
                Printer.is_active.is_(True),
            )
        ).all()
    )
    if not printer_ids:
        return []
    return list(
        db.scalars(
            select(PrintJob)
            .where(
                PrintJob.printer_id.in_(printer_ids),
                PrintJob.status == PrintJobStatus.PENDING.value,
            )
            .order_by(PrintJob.created_at.asc())
            .limit(limit)
        ).all()
    )


def claim_job(db: Session, agent: PrintAgent, job_id: int) -> PrintJob | None:
    job = db.get(PrintJob, job_id)
    if job is None:
        return None
    printer = db.get(Printer, job.printer_id)
    if printer is None or printer.agent_id != agent.id:
        return None
    if job.status != PrintJobStatus.PENDING.value:
        return None
    job.status = PrintJobStatus.CLAIMED.value
    job.claimed_by_agent_id = agent.id
    job.claimed_at = datetime.now(timezone.utc)
    job.attempts = (job.attempts or 0) + 1
    job.updated_at = datetime.now(timezone.utc)
    db.flush()
    return job


def mark_job_printed(db: Session, agent: PrintAgent, job_id: int) -> PrintJob | None:
    job = db.get(PrintJob, job_id)
    if job is None:
        return None
    printer = db.get(Printer, job.printer_id)
    if printer is None or printer.agent_id != agent.id:
        return None
    if job.status not in (
        PrintJobStatus.CLAIMED.value,
        PrintJobStatus.PENDING.value,
    ):
        return None
    job.status = PrintJobStatus.PRINTED.value
    job.printed_at = datetime.now(timezone.utc)
    job.error_message = None
    job.updated_at = datetime.now(timezone.utc)
    db.flush()
    from modules.dashboard_notify.constants import PRINTING
    from modules.dashboard_notify.service import resolve_activity

    resolve_activity(db, PRINTING, ref_id=job.id)
    return job


def mark_job_failed(
    db: Session, agent: PrintAgent, job_id: int, error_message: str
) -> PrintJob | None:
    job = db.get(PrintJob, job_id)
    if job is None:
        return None
    printer = db.get(Printer, job.printer_id)
    if printer is None or printer.agent_id != agent.id:
        return None
    job.status = PrintJobStatus.FAILED.value
    job.error_message = (error_message or "unknown")[:500]
    job.updated_at = datetime.now(timezone.utc)
    db.flush()
    from modules.dashboard_notify.constants import PRINTING
    from modules.dashboard_notify.service import resolve_activity

    resolve_activity(db, PRINTING, ref_id=job.id)
    return job


def job_to_api_dict(job: PrintJob, db: Session) -> dict:
    printer = job.printer or db.get(Printer, job.printer_id)
    return {
        "id": job.id,
        "printer_id": job.printer_id,
        "printer_code": printer.code if printer else None,
        "local_printer_key": printer.local_printer_key if printer else None,
        "job_type": job.job_type,
        "payload_format": job.payload_format,
        "payload": job.payload,
        "kitchen_ticket_id": job.kitchen_ticket_id,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }
