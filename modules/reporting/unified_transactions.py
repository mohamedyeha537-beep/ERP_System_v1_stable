"""سجل عمليات موحّد — مبيعات، مقبوضات، مشتريات، مصروفات (الأحدث أولاً)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from modules.platform.business_domain import BusinessDomain, purchase_domain_db_values

KIND_SALE = "sale"
KIND_RECEIPT = "receipt"
KIND_PURCHASE = "purchase"
KIND_EXPENSE = "expense"
KIND_REFUND = "refund"
KIND_TRANSFER = "transfer"

KIND_LABELS = {
    KIND_SALE: "مبيعات",
    KIND_RECEIPT: "مقبوضات",
    KIND_PURCHASE: "مشتريات",
    KIND_EXPENSE: "مصروفات",
    KIND_REFUND: "مرتجع",
    KIND_TRANSFER: "تحويل",
}

KIND_OPTIONS = [
    ("all", "الكل"),
    (KIND_SALE, "مبيعات"),
    (KIND_RECEIPT, "مقبوضات"),
    (KIND_PURCHASE, "مشتريات"),
    (KIND_EXPENSE, "مصروفات"),
    (KIND_REFUND, "مرتجعات"),
    (KIND_TRANSFER, "تحويلات"),
]

_PAGE_SIZE = 40
_EXPORT_CAP = 8000


@dataclass
class UnifiedTxRow:
    kind: str
    kind_label: str
    created_at: datetime
    ref: str
    party_name: str
    amount: Decimal
    signed_amount: Decimal
    method_name: str
    note: str
    detail_url: str
    sort_id: int
    employee_name: str = ""
    source_kind: str = ""
    source_id: int = 0


@dataclass
class UnifiedTxSummary:
    total_count: int
    sales_total: Decimal
    receipts_total: Decimal
    purchases_total: Decimal
    expenses_total: Decimal
    refunds_total: Decimal
    transfers_total: Decimal


def _user_display_names(db: Session, user_ids: set[int]) -> dict[int, str]:
    """اسم الموظف المرتبط بالمستخدم، أو اسم الدخول إن لم يُربط."""
    if not user_ids:
        return {}
    from modules.authz.models import User
    from modules.hr.models import Employee

    users = {
        int(u.id): (u.username or "").strip()
        for u in db.scalars(select(User).where(User.id.in_(user_ids))).all()
    }
    emp_by_user = {
        int(e.user_id): (e.full_name_ar or "").strip()
        for e in db.scalars(select(Employee).where(Employee.user_id.in_(user_ids))).all()
        if e.user_id
    }
    out: dict[int, str] = {}
    for uid in user_ids:
        out[uid] = emp_by_user.get(uid) or users.get(uid) or ""
    return out


def _q_match(text: str | None, q: str) -> bool:
    if not q:
        return True
    return q in (text or "").casefold()


def _customer_label(customer) -> str:
    if customer is None:
        return "—"
    for attr in ("name", "full_name", "name_ar", "display_name"):
        val = getattr(customer, attr, None)
        if val and str(val).strip():
            return str(val).strip()
    phone = getattr(customer, "phone", None)
    if phone:
        return str(phone).strip()
    return f"عميل #{getattr(customer, 'id', '?')}"


def _collect_rows(
    db: Session,
    start: datetime,
    end: datetime,
    *,
    domain: BusinessDomain | None,
    kind: str,
    q: str,
) -> list[UnifiedTxRow]:
    """يجمع كل العمليات المطابقة ثم يرتّبها (الأحدث أولاً)."""
    from modules.gl.wallet_labels import label_from_info_map, wallet_gl_info_map

    rows: list[UnifiedTxRow] = []
    gl_info = wallet_gl_info_map(db)
    q_norm = (q or "").strip().casefold()
    kind = (kind or "all").strip().lower()
    if kind not in {k for k, _ in KIND_OPTIONS}:
        kind = "all"

    include_pos = domain in (None, BusinessDomain.RESTAURANT)
    include_hotel = domain in (None, BusinessDomain.HOTEL)
    include_purchases = True  # مشتريات/مصروفات حسب domain على السجل

    # ── مبيعات POS ──
    if include_pos and kind in ("all", KIND_SALE):
        from modules.sales.models import Sale, SaleStatus

        sales = list(
            db.scalars(
                select(Sale)
                .where(
                    Sale.status == SaleStatus.COMPLETED,
                    Sale.created_at >= start,
                    Sale.created_at < end,
                )
                .options(selectinload(Sale.customer))
                .order_by(Sale.created_at.desc(), Sale.id.desc())
                .limit(_EXPORT_CAP)
            ).all()
        )
        for sale in sales:
            party = _customer_label(sale.customer)
            ref = (
                sale.final_invoice_number
                or sale.receipt_number
                or f"فاتورة #{sale.id}"
            )
            note = ""
            if sale.context_type and hasattr(sale.context_type, "value"):
                note = str(sale.context_type.value)
            if not _q_match(party, q_norm) and not _q_match(ref, q_norm) and not _q_match(
                str(sale.id), q_norm
            ):
                continue
            amt = Decimal(str(sale.total or 0)).quantize(Decimal("0.001"))
            rows.append(
                UnifiedTxRow(
                    kind=KIND_SALE,
                    kind_label=KIND_LABELS[KIND_SALE],
                    created_at=sale.created_at,
                    ref=str(ref),
                    party_name=party,
                    amount=amt,
                    signed_amount=amt,
                    method_name="—",
                    note=note,
                    detail_url=f"/pos/receipt/{sale.id}?doc=invoice",
                    sort_id=int(sale.id),
                )
            )

    # ── مقبوضات POS (دفعات الفواتير) ──
    if include_pos and kind in ("all", KIND_RECEIPT):
        from modules.payments.models import SalePayment
        from modules.sales.models import Sale

        pay_rows = db.execute(
            select(SalePayment, Sale)
            .join(Sale, Sale.id == SalePayment.sale_id)
            .options(
                selectinload(SalePayment.method),
                selectinload(Sale.customer),
            )
            .where(
                SalePayment.created_at >= start,
                SalePayment.created_at < end,
            )
            .order_by(SalePayment.created_at.desc(), SalePayment.id.desc())
            .limit(_EXPORT_CAP)
        ).all()
        for sp, sale in pay_rows:
            party = _customer_label(sale.customer)
            ref = f"تحصيل فاتورة #{sale.id}"
            method = label_from_info_map(gl_info, sp.method) if sp.method else "—"
            if (
                not _q_match(party, q_norm)
                and not _q_match(ref, q_norm)
                and not _q_match(method, q_norm)
            ):
                continue
            amt = Decimal(str(sp.amount or 0)).quantize(Decimal("0.001"))
            rows.append(
                UnifiedTxRow(
                    kind=KIND_RECEIPT,
                    kind_label=KIND_LABELS[KIND_RECEIPT],
                    created_at=sp.created_at,
                    ref=ref,
                    party_name=party,
                    amount=amt,
                    signed_amount=amt,
                    method_name=str(method),
                    note="مطعم / POS",
                    detail_url=f"/pos/receipt/{sale.id}?doc=invoice",
                    sort_id=int(sp.id),
                )
            )

    # ── مقبوضات / مرتجعات الفندق ──
    if include_hotel and kind in ("all", KIND_RECEIPT, KIND_REFUND):
        from modules.hotel.revenue_stats import hotel_collections_detail

        hotel_rows, _ = hotel_collections_detail(db, start, end)
        for hr in hotel_rows:
            is_refund = hr.movement_type == "مرتجع"
            row_kind = KIND_REFUND if is_refund else KIND_RECEIPT
            if kind not in ("all", row_kind):
                continue
            if (
                not _q_match(hr.guest_name, q_norm)
                and not _q_match(hr.booking_ref, q_norm)
                and not _q_match(hr.payment_method, q_norm)
                and not _q_match(hr.note, q_norm)
            ):
                continue
            amt = Decimal(str(hr.amount or 0)).quantize(Decimal("0.001"))
            signed = -amt if is_refund else amt
            rows.append(
                UnifiedTxRow(
                    kind=row_kind,
                    kind_label=KIND_LABELS[row_kind],
                    created_at=hr.created_at,
                    ref=f"{hr.booking_ref} · {hr.movement_type}",
                    party_name=hr.guest_name or "—",
                    amount=amt,
                    signed_amount=signed,
                    method_name=hr.payment_method or "—",
                    note=(hr.note or ("عربون" if hr.is_deposit else "فندق"))[:160],
                    detail_url=f"/admin/hotel/bookings/{hr.booking_id}",
                    sort_id=int(hr.movement_id),
                )
            )

    # ── مشتريات ومصروفات ──
    if include_purchases and kind in ("all", KIND_PURCHASE, KIND_EXPENSE):
        from modules.payments.models import Purchase, PurchaseKind

        stmt = (
            select(Purchase)
            .where(Purchase.created_at >= start, Purchase.created_at < end)
            .options(selectinload(Purchase.method))
            .order_by(Purchase.created_at.desc(), Purchase.id.desc())
            .limit(_EXPORT_CAP)
        )
        domain_vals = purchase_domain_db_values(domain)
        if domain_vals is not None:
            stmt = stmt.where(Purchase.business_domain.in_(domain_vals))
        if kind == KIND_PURCHASE:
            stmt = stmt.where(Purchase.kind == PurchaseKind.INVENTORY)
        elif kind == KIND_EXPENSE:
            stmt = stmt.where(
                or_(
                    Purchase.kind == PurchaseKind.EXPENSE,
                    Purchase.kind == PurchaseKind.ASSET,
                )
            )
        else:
            stmt = stmt.where(
                Purchase.kind.in_(
                    [PurchaseKind.INVENTORY, PurchaseKind.EXPENSE, PurchaseKind.ASSET]
                )
            )

        for p in db.scalars(stmt).all():
            if p.kind == PurchaseKind.INVENTORY:
                row_kind = KIND_PURCHASE
            else:
                row_kind = KIND_EXPENSE
            if kind not in ("all", row_kind):
                continue
            party = (p.supplier or p.expense_category or "—").strip() or "—"
            ref = (
                p.supplier_invoice_ref
                or p.receipt_batch_no
                or f"{'شراء' if row_kind == KIND_PURCHASE else 'مصروف'} #{p.id}"
            )
            method = label_from_info_map(gl_info, p.method) if p.method else "—"
            note = (p.note or p.expense_category or "")[:160]
            if (
                not _q_match(party, q_norm)
                and not _q_match(ref, q_norm)
                and not _q_match(method, q_norm)
                and not _q_match(note, q_norm)
            ):
                continue
            amt = Decimal(str(p.amount or 0)).quantize(Decimal("0.001"))
            rows.append(
                UnifiedTxRow(
                    kind=row_kind,
                    kind_label=KIND_LABELS[row_kind],
                    created_at=p.created_at,
                    ref=str(ref),
                    party_name=party,
                    amount=amt,
                    signed_amount=-amt,
                    method_name=str(method),
                    note=note,
                    detail_url=f"/admin/purchases/{p.id}",
                    sort_id=int(p.id),
                )
            )

    # ── تحويلات بين الحسابات (يدوي / مالك) ──
    if kind in ("all", KIND_TRANSFER):
        from modules.payments.models import PaymentTransfer, PaymentTransferType

        xfer_types = (
            PaymentTransferType.MANUAL,
            PaymentTransferType.OWNER_DRAW,
            PaymentTransferType.OWNER_CAPITAL,
        )
        xfers = list(
            db.scalars(
                select(PaymentTransfer)
                .where(
                    PaymentTransfer.created_at >= start,
                    PaymentTransfer.created_at < end,
                    PaymentTransfer.transfer_type.in_(xfer_types),
                )
                .options(
                    selectinload(PaymentTransfer.from_method),
                    selectinload(PaymentTransfer.to_method),
                )
                .order_by(PaymentTransfer.created_at.desc(), PaymentTransfer.id.desc())
                .limit(_EXPORT_CAP)
            ).all()
        )
        domain_vals = purchase_domain_db_values(domain)
        user_ids = {int(tf.created_by_id) for tf in xfers if tf.created_by_id}
        emp_names = _user_display_names(db, user_ids)
        for tf in xfers:
            from_m = tf.from_method
            to_m = tf.to_method
            if domain_vals is not None:
                from_dom = getattr(getattr(from_m, "business_domain", None), "value", None)
                to_dom = getattr(getattr(to_m, "business_domain", None), "value", None)
                if from_dom not in domain_vals and to_dom not in domain_vals:
                    continue
            from_name = label_from_info_map(gl_info, from_m) if from_m else "—"
            to_name = label_from_info_map(gl_info, to_m) if to_m else "—"
            party = f"{from_name} → {to_name}"
            ref = f"تحويل #{tf.id}"
            note = (tf.note or "").strip()
            if (
                not _q_match(party, q_norm)
                and not _q_match(ref, q_norm)
                and not _q_match(note, q_norm)
                and not _q_match(from_name, q_norm)
                and not _q_match(to_name, q_norm)
            ):
                continue
            amt = Decimal(str(tf.amount or 0)).quantize(Decimal("0.001"))
            rows.append(
                UnifiedTxRow(
                    kind=KIND_TRANSFER,
                    kind_label=KIND_LABELS[KIND_TRANSFER],
                    created_at=tf.created_at,
                    ref=ref,
                    party_name=party,
                    amount=amt,
                    signed_amount=Decimal("0.000"),
                    method_name=f"{from_name} → {to_name}",
                    note=note,
                    detail_url=f"/reports/transactions/transfer/{tf.id}",
                    sort_id=int(tf.id),
                    employee_name=emp_names.get(int(tf.created_by_id or 0), ""),
                    source_kind="transfer",
                    source_id=int(tf.id),
                )
            )

    _epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    rows.sort(
        key=lambda r: (
            r.created_at or _epoch,
            r.sort_id,
        ),
        reverse=True,
    )
    return rows


def summarize(rows: list[UnifiedTxRow]) -> UnifiedTxSummary:
    zero = Decimal("0.000")
    sales = receipts = purchases = expenses = refunds = transfers = zero
    for r in rows:
        if r.kind == KIND_SALE:
            sales += r.amount
        elif r.kind == KIND_RECEIPT:
            receipts += r.amount
        elif r.kind == KIND_PURCHASE:
            purchases += r.amount
        elif r.kind == KIND_EXPENSE:
            expenses += r.amount
        elif r.kind == KIND_REFUND:
            refunds += r.amount
        elif r.kind == KIND_TRANSFER:
            transfers += r.amount
    return UnifiedTxSummary(
        total_count=len(rows),
        sales_total=sales.quantize(Decimal("0.001")),
        receipts_total=receipts.quantize(Decimal("0.001")),
        purchases_total=purchases.quantize(Decimal("0.001")),
        expenses_total=expenses.quantize(Decimal("0.001")),
        refunds_total=refunds.quantize(Decimal("0.001")),
        transfers_total=transfers.quantize(Decimal("0.001")),
    )


def list_unified_transactions(
    db: Session,
    start: datetime,
    end: datetime,
    *,
    domain: BusinessDomain | None = None,
    kind: str = "all",
    q: str = "",
    page: int = 1,
    page_size: int = _PAGE_SIZE,
) -> tuple[list[UnifiedTxRow], UnifiedTxSummary, int, int]:
    """يرجع (صفحة الصفوف، الملخص، إجمالي العدد، عدد الصفحات)."""
    all_rows = _collect_rows(db, start, end, domain=domain, kind=kind, q=q)
    summary = summarize(all_rows)
    total = len(all_rows)
    page = max(1, int(page or 1))
    page_size = max(10, min(100, int(page_size or _PAGE_SIZE)))
    total_pages = max(1, (total + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * page_size
    page_rows = all_rows[offset : offset + page_size]
    return page_rows, summary, total, total_pages


def export_unified_rows(
    db: Session,
    start: datetime,
    end: datetime,
    *,
    domain: BusinessDomain | None = None,
    kind: str = "all",
    q: str = "",
) -> list[UnifiedTxRow]:
    return _collect_rows(db, start, end, domain=domain, kind=kind, q=q)
