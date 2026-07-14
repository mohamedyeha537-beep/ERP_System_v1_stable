"""ذمم مدينة — ديون العملاء (فواتير غير مسدّدة أو مسدّدة جزئياً)."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, unquote

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from modules.customers.models import Customer
from modules.hotel.models import HotelRoom, RoomCharge
from modules.payments.models import PaymentMethod, SalePayment
from modules.payments.service import PaymentsError, list_payment_methods_for_receive, record_sale_payment
from modules.refunds.models import SaleReturn, SaleReturnStatus
from modules.refunds.service import sale_loyalty_discount_total, sale_outstanding_total
from modules.sales.models import Sale, SaleContext, SaleStatus


class ReceivablesError(Exception):
    pass


class InvoicePayStatus(str, enum.Enum):
    PAID = "paid"
    PARTIAL = "partial"
    UNPAID = "unpaid"


@dataclass
class ReceivableInvoiceRow:
    sale_id: int
    created_at: datetime | None
    context_type: SaleContext
    party_label: str
    customer_id: int | None
    debt_key: str
    debt_reason: str
    net_total: Decimal
    paid_total: Decimal
    outstanding: Decimal
    pay_status: InvoicePayStatus
    has_open_room_charge: bool
    room_id: int | None = None
    room_number: str | None = None
    guest_name: str | None = None


@dataclass
class CustomerDebtRow:
    debt_key: str
    customer_id: int | None
    party_label: str
    invoice_count: int
    total_outstanding: Decimal
    total_paid: Decimal
    pay_status: InvoicePayStatus


@dataclass
class ReceivablesSummary:
    total_outstanding: Decimal
    invoice_count_with_balance: int
    unpaid_count: int
    partial_count: int
    paid_count: int


@dataclass
class ReceivableInvoiceDetail:
    row: ReceivableInvoiceRow
    sale: Sale
    payments: list[SalePayment]
    room_charge: RoomCharge | None


def _classify_pay_status(
    net: Decimal, paid: Decimal, outstanding: Decimal
) -> InvoicePayStatus:
    if net <= 0 or outstanding <= 0:
        return InvoicePayStatus.PAID
    if paid <= 0:
        return InvoicePayStatus.UNPAID
    return InvoicePayStatus.PARTIAL


def _room_number_from_charge(
    db: Session, room_charge: RoomCharge | None
) -> str | None:
    if room_charge is None:
        return None
    if room_charge.room is not None:
        num = str(room_charge.room.number or "").strip()
        if num:
            return num
    if room_charge.room_id:
        rm = db.get(HotelRoom, int(room_charge.room_id))
        if rm is not None:
            num = str(rm.number or "").strip()
            if num:
                return num
    return None


def _format_room_party_label(
    room_number: str | None,
    guest_name: str | None = None,
    *,
    sale_id: int | None = None,
) -> str:
    if room_number:
        base = f"شقة/غرفة {room_number}"
        if guest_name:
            return f"{base} — {guest_name}"
        return base
    if sale_id is not None:
        return f"شقة/غرفة — غير مربوطة (فاتورة #{sale_id})"
    return "شقة/غرفة — غير مربوطة"


def _party_label(
    db: Session,
    sale: Sale,
    *,
    room_charge: RoomCharge | None,
    customer: Customer | None = None,
    room_number: str | None = None,
) -> str:
    if sale.context_type == SaleContext.ROOM or room_charge is not None:
        num = room_number or _room_number_from_charge(db, room_charge)
        guest = None
        if room_charge is not None:
            guest = (room_charge.guest_name_snapshot or "").strip() or None
        return _format_room_party_label(num, guest, sale_id=sale.id)
    if sale.context_type == SaleContext.EXTERNAL and sale.customer_id and customer:
        name = (customer.name or "").strip() or "عميل"
        phone = (customer.phone or "").strip()
        return f"{name}" + (f" — {phone}" if phone else "")
    if sale.table_id and sale.table:
        return f"طاولة: {sale.table.name_ar}"
    if sale.context_type == SaleContext.EXTERNAL:
        return "طلب خارجي"
    return "طاولة / محلي"


def _debt_reason(
    db: Session,
    sale: Sale,
    *,
    room_charge: RoomCharge | None,
    customer: Customer | None = None,
    room_number: str | None = None,
) -> str:
    if sale.context_type == SaleContext.ROOM or room_charge is not None:
        num = room_number or _room_number_from_charge(db, room_charge)
        parts = []
        if num:
            parts.append(f"قيد على حساب شقة/غرفة رقم {num}")
        else:
            parts.append(f"قيد على حساب شقة (فاتورة #{sale.id} — رقم الغرفة غير مسجّل)")
        if room_charge is not None:
            guest = (room_charge.guest_name_snapshot or "").strip()
            if guest:
                parts.append(f"النزيل: {guest}")
            if room_charge.note:
                parts.append(f"ملاحظة القيد: {room_charge.note}")
        return " · ".join(parts)
    if sale.context_type == SaleContext.EXTERNAL and customer:
        name = (customer.name or "").strip() or "عميل"
        phone = (customer.phone or "").strip()
        base = f"طلب خارجي — {name}"
        return f"{base} ({phone})" if phone else base
    if sale.context_type == SaleContext.TABLE and sale.table:
        return f"فاتورة طاولة {sale.table.name_ar} — لم يُسجّل تحصيل كامل"
    if sale.context_type == SaleContext.EXTERNAL:
        return "طلب خارجي — لم يُسجّل تحصيل كامل"
    return "فاتورة محلية — لم يُسجّل تحصيل كامل"


def make_debt_key(
    sale: Sale,
    *,
    room_charge: RoomCharge | None,
    party_label: str,
) -> str:
    if sale.customer_id:
        return f"c:{int(sale.customer_id)}"
    if room_charge is not None:
        guest = quote((room_charge.guest_name_snapshot or "").strip(), safe="")
        return f"r:{int(room_charge.room_id)}:{guest}"
    return f"p:{quote(party_label, safe='')}"


def parse_debt_key(key: str) -> tuple[str, ...]:
    raw = (key or "").strip()
    if raw.startswith("c:"):
        return ("customer", raw[2:])
    if raw.startswith("r:"):
        rest = raw[2:]
        if ":" not in rest:
            raise ReceivablesError("مفتاح الزبون غير صالح.")
        room_s, guest_enc = rest.split(":", 1)
        return ("room_guest", room_s, unquote(guest_enc))
    if raw.startswith("p:"):
        return ("party", unquote(raw[2:]))
    raise ReceivablesError("مفتاح الزبون غير صالح.")


def _load_completed_sales_with_amounts(db: Session) -> list[tuple[Sale, Decimal, Decimal]]:
    paid_sq = (
        select(
            SalePayment.sale_id.label("sale_id"),
            func.coalesce(func.sum(SalePayment.amount), 0).label("paid"),
        )
        .group_by(SalePayment.sale_id)
        .subquery()
    )
    ret_sq = (
        select(
            SaleReturn.original_sale_id.label("sale_id"),
            func.coalesce(func.sum(SaleReturn.total), 0).label("returned"),
        )
        .where(SaleReturn.status == SaleReturnStatus.POSTED)
        .group_by(SaleReturn.original_sale_id)
        .subquery()
    )
    rows = db.execute(
        select(
            Sale,
            func.coalesce(ret_sq.c.returned, 0),
            func.coalesce(paid_sq.c.paid, 0),
        )
        .outerjoin(ret_sq, ret_sq.c.sale_id == Sale.id)
        .outerjoin(paid_sq, paid_sq.c.sale_id == Sale.id)
        .where(Sale.status == SaleStatus.COMPLETED)
        .options(selectinload(Sale.table))
        .order_by(Sale.id.desc())
    ).all()
    return [(sale, Decimal(str(ret or 0)), Decimal(str(paid or 0))) for sale, ret, paid in rows]


def _customers_by_id(db: Session, customer_ids: set[int]) -> dict[int, Customer]:
    if not customer_ids:
        return {}
    rows = db.scalars(select(Customer).where(Customer.id.in_(customer_ids))).all()
    return {int(c.id): c for c in rows}


def _room_charges_by_sale_id(db: Session) -> dict[int, RoomCharge]:
    rows = db.scalars(
        select(RoomCharge).options(selectinload(RoomCharge.room))
    ).all()
    return {int(rc.sale_id): rc for rc in rows}


def _open_room_charge_sale_ids(db: Session) -> set[int]:
    ids = db.scalars(
        select(RoomCharge.sale_id).where(RoomCharge.is_settled.is_(False))
    ).all()
    return {int(i) for i in ids}


def _aggregate_party_status(
    invoice_rows: list[ReceivableInvoiceRow],
) -> InvoicePayStatus:
    statuses = {r.pay_status for r in invoice_rows if r.outstanding > 0}
    if not statuses:
        return InvoicePayStatus.PAID
    if InvoicePayStatus.PARTIAL in statuses:
        return InvoicePayStatus.PARTIAL
    if InvoicePayStatus.UNPAID in statuses and len(statuses) == 1:
        return InvoicePayStatus.UNPAID
    return InvoicePayStatus.PARTIAL


def build_receivable_rows(
    db: Session,
    *,
    pay_filter: str | None = None,
    context_filter: str | None = None,
    only_with_balance: bool = False,
    debt_key_filter: str | None = None,
) -> list[ReceivableInvoiceRow]:
    room_open = _open_room_charge_sale_ids(db)
    room_by_sale = _room_charges_by_sale_id(db)
    raw = _load_completed_sales_with_amounts(db)
    customer_ids = {
        int(s.customer_id) for s, _, _ in raw if s.customer_id is not None
    }
    customers = _customers_by_id(db, customer_ids)
    out: list[ReceivableInvoiceRow] = []
    for sale, returned, paid in raw:
        net = Decimal(str(sale.total or 0)) - returned
        if net < 0:
            net = Decimal("0")
        net = net.quantize(Decimal("0.001"))
        paid = paid.quantize(Decimal("0.001"))
        loyalty = sale_loyalty_discount_total(db, int(sale.id))
        outstanding = (net - paid - loyalty).quantize(Decimal("0.001"))
        if outstanding < 0:
            outstanding = Decimal("0")
        status = _classify_pay_status(net, paid, outstanding)
        rc = room_by_sale.get(int(sale.id))
        has_room = sale.id in room_open
        customer = (
            customers.get(int(sale.customer_id)) if sale.customer_id else None
        )
        room_num = None
        guest = None
        rid = None
        if rc is not None:
            rid = int(rc.room_id)
            guest = (rc.guest_name_snapshot or "").strip() or None
            room_num = _room_number_from_charge(db, rc)
        elif sale.context_type == SaleContext.ROOM:
            rc_one = db.scalar(
                select(RoomCharge)
                .where(RoomCharge.sale_id == sale.id)
                .options(selectinload(RoomCharge.room))
            )
            if rc_one is not None:
                rc = rc_one
                rid = int(rc.room_id)
                guest = (rc.guest_name_snapshot or "").strip() or None
                room_num = _room_number_from_charge(db, rc)

        party = _party_label(
            db, sale, room_charge=rc, customer=customer, room_number=room_num
        )
        dkey = make_debt_key(sale, room_charge=rc, party_label=party)

        if debt_key_filter and dkey != debt_key_filter:
            continue

        if context_filter and context_filter != "all":
            try:
                ctx = SaleContext(context_filter.upper())
            except ValueError:
                ctx = None
            if ctx is not None and sale.context_type != ctx:
                continue

        if pay_filter and pay_filter != "all":
            try:
                pf = InvoicePayStatus(pay_filter.lower())
            except ValueError:
                pf = None
            if pf is not None and status != pf:
                continue

        if only_with_balance and outstanding <= 0:
            continue

        out.append(
            ReceivableInvoiceRow(
                sale_id=sale.id,
                created_at=sale.created_at,
                context_type=sale.context_type,
                party_label=party,
                customer_id=sale.customer_id,
                debt_key=dkey,
                debt_reason=_debt_reason(
                    db, sale, room_charge=rc, customer=customer, room_number=room_num
                ),
                net_total=net,
                paid_total=paid,
                outstanding=outstanding,
                pay_status=status,
                has_open_room_charge=has_room,
                room_id=rid,
                room_number=room_num,
                guest_name=guest,
            )
        )
    return out


def list_unlinked_room_receivable_rows(
    db: Session, *, only_with_balance: bool = True
) -> list[ReceivableInvoiceRow]:
    """فواتير سياق شقة بلا قيد غرفة — تظهر في الديون لكن لا في تسوية الغرف."""
    rows = build_receivable_rows(db, only_with_balance=only_with_balance)
    return [
        r
        for r in rows
        if r.context_type == SaleContext.ROOM and r.room_id is None
    ]


def receivables_summary(db: Session) -> ReceivablesSummary:
    rows = build_receivable_rows(db)
    total = Decimal("0")
    with_bal = 0
    unpaid = partial = paid = 0
    for r in rows:
        if r.outstanding > 0:
            total += r.outstanding
            with_bal += 1
        if r.pay_status == InvoicePayStatus.UNPAID:
            unpaid += 1
        elif r.pay_status == InvoicePayStatus.PARTIAL:
            partial += 1
        else:
            paid += 1
    return ReceivablesSummary(
        total_outstanding=total.quantize(Decimal("0.001")),
        invoice_count_with_balance=with_bal,
        unpaid_count=unpaid,
        partial_count=partial,
        paid_count=paid,
    )


def _group_party_label(invs: list[ReceivableInvoiceRow]) -> str:
    """عنوان مجمّع يوضّح أرقام الغرف عند تعدد الفواتير."""
    rooms = sorted({str(i.room_number) for i in invs if i.room_number})
    if len(rooms) == 1:
        guests = sorted({g for g in (i.guest_name for i in invs if i.guest_name) if g})
        guest = guests[0] if len(guests) == 1 else None
        return _format_room_party_label(rooms[0], guest)
    if len(rooms) > 1:
        return "شقق/غرف: " + "، ".join(rooms)
    return invs[0].party_label


def customer_debt_groups(rows: list[ReceivableInvoiceRow]) -> list[CustomerDebtRow]:
    """تجميع الديون حسب العميل / النزيل / الطرف."""
    buckets: dict[str, list[ReceivableInvoiceRow]] = {}
    for r in rows:
        if r.outstanding <= 0:
            continue
        buckets.setdefault(r.debt_key, []).append(r)

    groups: list[CustomerDebtRow] = []
    for key, invs in buckets.items():
        total_out = sum((i.outstanding for i in invs), Decimal("0")).quantize(
            Decimal("0.001")
        )
        total_paid = sum((i.paid_total for i in invs), Decimal("0")).quantize(
            Decimal("0.001")
        )
        groups.append(
            CustomerDebtRow(
                debt_key=key,
                customer_id=invs[0].customer_id,
                party_label=_group_party_label(invs),
                invoice_count=len(invs),
                total_outstanding=total_out,
                total_paid=total_paid,
                pay_status=_aggregate_party_status(invs),
            )
        )
    groups.sort(key=lambda x: x.total_outstanding, reverse=True)
    return groups


def debtor_title_from_key(db: Session, debt_key: str) -> str:
    groups = customer_debt_groups(build_receivable_rows(db, only_with_balance=True))
    for g in groups:
        if g.debt_key == debt_key:
            return g.party_label
    try:
        parsed = parse_debt_key(debt_key)
    except ReceivablesError:
        return "زبون / طرف"
    if parsed[0] == "customer":
        c = db.get(Customer, int(parsed[1]))
        if c:
            name = (c.name or "").strip() or "عميل"
            phone = (c.phone or "").strip()
            return f"{name}" + (f" — {phone}" if phone else "")
    if parsed[0] == "room_guest":
        from modules.hotel.models import HotelRoom

        room = db.get(HotelRoom, int(parsed[1]))
        guest = parsed[2]
        if room:
            return (
                f"شقة/غرفة {room.number} — {guest}"
                if guest
                else f"شقة/غرفة {room.number}"
            )
        return guest or "شقة/غرفة"
    if parsed[0] == "party":
        return parsed[1]
    return "زبون / طرف"


def get_receivable_invoice_row(db: Session, sale_id: int) -> ReceivableInvoiceRow | None:
    for r in build_receivable_rows(db):
        if r.sale_id == sale_id:
            return r
    return None


def get_receivable_invoice_detail(
    db: Session, sale_id: int
) -> ReceivableInvoiceDetail | None:
    from modules.sales.service import load_sale_with_lines

    row = get_receivable_invoice_row(db, sale_id)
    if row is None:
        return None
    sale = load_sale_with_lines(db, sale_id)
    if sale is None:
        return None
    payments = list(
        db.scalars(
            select(SalePayment)
            .where(SalePayment.sale_id == sale_id)
            .order_by(SalePayment.created_at, SalePayment.id)
        ).all()
    )
    rc = db.scalar(
        select(RoomCharge)
        .where(RoomCharge.sale_id == sale_id)
        .options(selectinload(RoomCharge.room))
    )
    return ReceivableInvoiceDetail(row=row, sale=sale, payments=payments, room_charge=rc)


def parse_collection_amount(raw: str) -> Decimal:
    text = (raw or "").strip().replace(",", ".")
    if not text:
        raise ReceivablesError("أدخل مبلغ التحصيل.")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ReceivablesError("مبلغ التحصيل غير صالح.") from exc
    return amount.quantize(Decimal("0.001"))


def collect_sale_receivable(
    db: Session,
    *,
    sale_id: int,
    payment_method_id: int,
    amount: Decimal,
    user_id: int | None,
    note: str | None = None,
) -> SalePayment:
    """تحصيل كامل أو جزئي لفاتورة — يُحدّث حالة قيد الغرفة عند السداد الكامل."""
    outstanding = sale_outstanding_total(db, sale_id)
    if outstanding <= 0:
        raise ReceivablesError("لا يوجد مبلغ مستحق على هذه الفاتورة.")
    amt = amount.quantize(Decimal("0.001"))
    if amt <= 0:
        raise ReceivablesError("مبلغ التحصيل يجب أن يكون أكبر من صفر.")
    if amt > outstanding:
        raise ReceivablesError(
            f"المبلغ يتجاوز المتبقي ({outstanding} د.ل)."
        )
    try:
        sp = record_sale_payment(db, sale_id, payment_method_id, amt)
    except PaymentsError as exc:
        raise ReceivablesError(str(exc)) from exc

    rc = db.scalar(
        select(RoomCharge).where(
            RoomCharge.sale_id == sale_id,
            RoomCharge.is_settled.is_(False),
        )
    )
    remaining = sale_outstanding_total(db, sale_id)
    if rc is not None and remaining <= 0:
        now = datetime.now(timezone.utc)
        rc.is_settled = True
        rc.settled_at = now
        rc.settled_by_id = user_id
        rc.settlement_payment_method_id = payment_method_id
    if note and (note or "").strip():
        _ = note  # reserved for future payment note field
    db.flush()
    return sp


def list_receive_methods_for_collection(db: Session) -> list[PaymentMethod]:
    return list_payment_methods_for_receive(db, only_active=True)
