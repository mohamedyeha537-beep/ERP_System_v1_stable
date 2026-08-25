"""نص ESC/POS للفاتورة وأمر التجهيز — للطباعة عبر الوكيل المحلي."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from modules.printing.arabic_line import shape_arabic_escpos
from modules.sales.models import ExternalOrderType, Sale, SaleContext
from modules.sales.receipt_layout import build_receipt_sections


def format_qty_plain(value) -> str:
    from app.number_format import format_qty_plain as _fmt

    return _fmt(value)


def format_money_plain(value) -> str:
    from app.number_format import format_money_plain as _fmt

    return _fmt(value)


def format_points_plain(value) -> str:
    dec = Decimal(str(value or 0)).quantize(Decimal("0.001"))
    text = format(dec.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _line(text: str) -> str:
    return shape_arabic_escpos(text)


def build_sale_receipt_text(
    db: Session,
    sale: Sale,
    *,
    store_name: str,
    title: str,
    is_prebill: bool,
    cashier_name: str | None = None,
    room_hint: str | None = None,
    guest_name: str | None = None,
    guest_phone: str | None = None,
    payment_method_name: str | None = None,
    loyalty_points_display: str | None = None,
    loyalty_points_redeemed_display: str | None = None,
    loyalty_redeem_dinar_display: str | None = None,
    loyalty_balance_display: str | None = None,
    loyalty_show_footer: bool = False,
    loyalty_show_earned: bool = False,
    loyalty_points_earned_dinar_display: str | None = None,
    receipt_show_payment_breakdown: bool = False,
    receipt_invoice_total_display: str | None = None,
    receipt_loyalty_discount_display: str | None = None,
    receipt_amount_paid_display: str | None = None,
    receipt_amount_customer_total_display: str | None = None,
    paper_width: int = 80,
) -> str:
    """نفس بنود الفاتورة على الشاشة — تنسيق حراري مبسط (بدون جدول HTML)."""
    cols = 48 if paper_width >= 80 else 32
    sep = "=" * cols
    lines: list[str] = []

    lines.append(sep)
    lines.append(_line(store_name))
    lines.append(_line(title))
    lines.append(sep)
    lines.append(_line(f"رقم: {sale.id}"))
    if sale.created_at:
        from app.datetime_local import format_local_dt

        lines.append(_line(format_local_dt(sale.created_at, "%Y-%m-%d %H:%M")))
    if cashier_name:
        lines.append(_line(f"الكاشير: {cashier_name}"))
    if sale.table is not None:
        lines.append(_line(f"الطاولة: {sale.table.name_ar}"))
    if sale.context_type == SaleContext.ROOM and room_hint:
        lines.append(_line(f"شقة: {room_hint}"))
    if guest_name or guest_phone:
        g = []
        if guest_name:
            g.append(guest_name)
        if guest_phone:
            g.append(f"هاتف: {guest_phone}")
        lines.append(_line(" - ".join(g)))
    if (
        sale.context_type == SaleContext.EXTERNAL
        and sale.external_order_type == ExternalOrderType.DELIVERY
    ):
        lines.append(_line(f"توصيل - {sale.delivery_zone_name or 'منطقة'}"))
        if sale.customer is not None:
            cn = (sale.customer.name or "").strip()
            cp = (sale.customer.phone or "").strip()
            if cn:
                lines.append(_line(f"الزبون: {cn}"))
            if cp:
                lines.append(_line(f"هاتف: {cp}"))

    lines.append(_line("-" * cols))
    lines.append(_line("الصنف | كم | سعر | جملة"))
    lines.append(_line("-" * cols))

    sections = build_receipt_sections(db, sale)
    for sec in sections:
        lines.append(_line(sec.root_title))
        for sg in sec.subgroups:
            if sg.title:
                lines.append(_line(f"  {sg.title}"))
            for ln in sg.lines:
                if ln.product is None:
                    continue
                name = ln.product.name_ar or "-"
                qty = format_qty_plain(ln.quantity)
                unit = format_money_plain(ln.unit_price)
                total = format_money_plain(ln.line_total)
                lines.append(_line(name))
                lines.append(_line(f"  {qty} x {unit} = {total} د.ل"))
                note = (getattr(ln, "line_note", None) or "").strip()
                if note:
                    lines.append(_line(f"  ملاحظة: {note}"))
        lines.append("")

    lines.append(sep)
    total_label = "الإجمالي"
    if (
        sale.context_type == SaleContext.EXTERNAL
        and sale.external_order_type == ExternalOrderType.DELIVERY
    ):
        total_label = "قيمة الطلب"
    if receipt_show_payment_breakdown:
        inv = receipt_invoice_total_display or format_money_plain(sale.total)
        disc = receipt_loyalty_discount_display or "0"
        paid = receipt_amount_paid_display or "0"
        lines.append(_line(f"{total_label}: {inv} د.ل"))
        lines.append(_line(f"خصم نقاط ولاء: -{disc} د.ل"))
        lines.append(_line(f"المدفوع: {paid} د.ل"))
    else:
        lines.append(_line(f"{total_label}: {format_money_plain(sale.total)} د.ل"))
    if (
        sale.context_type == SaleContext.EXTERNAL
        and sale.external_order_type == ExternalOrderType.DELIVERY
    ):
        fee = sale.delivery_fee or Decimal("0")
        lines.append(_line(f"أجرة التوصيل: {format_money_plain(fee)} د.ل"))
        if receipt_show_payment_breakdown and receipt_amount_customer_total_display:
            cust_total = receipt_amount_customer_total_display
        else:
            cust_total = format_money_plain(sale.total + fee)
        lines.append(_line(f"الاجمالي على الزبون: {cust_total} د.ل"))
    pay_line = None
    if payment_method_name:
        pay_line = f"طريقة الدفع: {payment_method_name}"
    elif not is_prebill and sale.context_type == SaleContext.ROOM:
        pay_line = (
            "على حساب الشقة"
            + (f" {room_hint}" if room_hint else "")
            + " — يُحصَّل لاحقاً"
        )
    if pay_line:
        lines.append(_line(pay_line))
    if loyalty_show_earned or (
        loyalty_show_footer and (loyalty_points_display or "0") not in ("0", "")
    ):
        redeemed = loyalty_points_redeemed_display or "0"
        if redeemed not in ("0", "") and not receipt_show_payment_breakdown:
            dinar = loyalty_redeem_dinar_display or "0"
            lines.append(
                _line(f"خصم نقاط: {redeemed} (-{dinar} د.ل)")
            )
        earned = loyalty_points_display if loyalty_points_display is not None else "0"
        earned_dinar = loyalty_points_earned_dinar_display or "0"
        lines.append(_line(f"نقاط مكتسبة: {earned} نقطة ({earned_dinar} د.ل)"))
    elif loyalty_points_display is not None and loyalty_points_display not in ("0", ""):
        lines.append(_line(f"النقاط المكتسبة: {loyalty_points_display}"))
    if is_prebill:
        lines.append(_line("(غير مدفوع)"))
    elif sale.context_type == SaleContext.ROOM:
        lines.append("")
        lines.append(_line("الاسم: _________________________________"))
        lines.append(_line("التوقيع: _______________________________"))
    lines.append(sep)
    return "\n".join(lines)


def build_sale_whatsapp_text(
    db: Session,
    sale: Sale,
    *,
    store_name: str,
    customer_name: str = "",
) -> str:
    """نص فاتورة كاملة للإرسال على واتساب (بدون صورة)."""
    from modules.customers.service import build_receipt_loyalty_context
    from modules.payments.service import sum_sale_payments
    from modules.refunds.service import sale_outstanding_total

    loyalty = build_receipt_loyalty_context(db, sale)
    greeting = f"مرحباً {customer_name.strip()}،\n" if (customer_name or "").strip() else "مرحباً،\n"
    parts: list[str] = [
        greeting.rstrip(),
        f"فاتورتكم #{sale.id}",
        f"— {store_name}",
        "",
    ]
    if sale.table is not None:
        parts.append(f"الطاولة: {sale.table.name_ar}")
    if (
        sale.context_type == SaleContext.EXTERNAL
        and sale.external_order_type == ExternalOrderType.DELIVERY
    ):
        parts.append(f"توصيل — {sale.delivery_zone_name or 'منطقة توصيل'}")
    if sale.created_at:
        from app.datetime_local import format_local_dt

        parts.append(format_local_dt(sale.created_at, "%Y-%m-%d %H:%M"))
    parts.append("")
    parts.append("تفاصيل الفاتورة:")
    parts.append("")

    sections = build_receipt_sections(db, sale)
    for sec in sections:
        parts.append(f"*{sec.root_title}*")
        for sg in sec.subgroups:
            if sg.title:
                parts.append(sg.title)
            for ln in sg.lines:
                if ln.product is None:
                    continue
                name = (ln.product.name_ar or "-").strip()
                qty = format_qty_plain(ln.quantity)
                unit = format_money_plain(ln.unit_price)
                total = format_money_plain(ln.line_total)
                parts.append(f"• {name}")
                parts.append(f"  {qty} × {unit} = {total} د.ل")
                note = (getattr(ln, "line_note", None) or "").strip()
                if note:
                    parts.append(f"  ↳ {note}")
        parts.append("")

    total_label = "الإجمالي"
    if (
        sale.context_type == SaleContext.EXTERNAL
        and sale.external_order_type == ExternalOrderType.DELIVERY
    ):
        total_label = "قيمة الطلب"
    if loyalty.get("receipt_show_payment_breakdown"):
        inv = loyalty.get("receipt_invoice_total_display") or format_money_plain(sale.total)
        disc = loyalty.get("receipt_loyalty_discount_display") or "0"
        paid = loyalty.get("receipt_amount_paid_display") or "0"
        parts.append(f"{total_label}: {inv} د.ل")
        parts.append(f"خصم نقاط ولاء: −{disc} د.ل")
        parts.append(f"المدفوع: {paid} د.ل")
    else:
        parts.append(f"{total_label}: {format_money_plain(sale.total)} د.ل")

    if (
        sale.context_type == SaleContext.EXTERNAL
        and sale.external_order_type == ExternalOrderType.DELIVERY
    ):
        fee = sale.delivery_fee or Decimal("0")
        parts.append(f"أجرة التوصيل: {format_money_plain(fee)} د.ل")
        cust_total = loyalty.get("receipt_amount_customer_total_display")
        if not cust_total:
            cust_total = format_money_plain(sale.total + fee)
        parts.append(f"الإجمالي على الزبون: {cust_total} د.ل")

    paid_sum = sum_sale_payments(db, sale.id)
    balance = sale_outstanding_total(db, sale.id)
    if balance > Decimal("0.0005"):
        if paid_sum > 0 and not loyalty.get("receipt_show_payment_breakdown"):
            parts.append(f"المدفوع: {format_money_plain(paid_sum)} د.ل")
        parts.append(f"المتبقي: {format_money_plain(balance)} د.ل")

    return "\n".join(parts).strip()
