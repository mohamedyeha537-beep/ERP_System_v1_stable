from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from modules.catalog.uploads import delete_stored_relative_file

from modules.delivery.models import DeliveryCashSettlement
from modules.inventory.models import StockMovementType
from modules.inventory.service import apply_movement
from modules.payments.models import (
    LEGACY_OWNER_CAPITAL_PM_NAME,
    LEGACY_OWNER_DRAW_PM_NAME,
    OWNER_EQUITY_PM_NAME,
    SUPPLIER_CREDIT_PM_NAME,
    PaymentMethod,
    PaymentMethodKind,
    PaymentTransfer,
    PaymentTransferType,
    Purchase,
    PurchaseKind,
    PurchaseLine,
    PurchasePayment,
    RefundPayment,
    SalePayment,
)
from modules.sales.models import Sale, SaleStatus


class PaymentsError(Exception):
    pass


def ensure_default_payment_methods(db: Session) -> None:
    """ينشئ أسلوب «كاش» إن لم يوجد أي أسلوب دفع."""
    existing = db.execute(select(PaymentMethod).limit(1)).scalar_one_or_none()
    if existing is not None:
        ensure_supplier_credit_payment_method(db)
        return
    db.add(
        PaymentMethod(
            name_ar="كاش",
            kind=PaymentMethodKind.CASH,
            is_active=True,
            sort_order=0,
            can_receive=True,
            can_pay=True,
            can_fund=False,
        )
    )
    db.commit()
    ensure_supplier_credit_payment_method(db)
    ensure_owner_equity_payment_method(db)


def ensure_supplier_credit_payment_method(db: Session) -> PaymentMethod:
    """محفظة افتراضية لفواتير الشراء الآجلة (ذمم دائن للمورد)."""
    row = db.execute(
        select(PaymentMethod).where(PaymentMethod.name_ar == SUPPLIER_CREDIT_PM_NAME)
    ).scalar_one_or_none()
    if row is not None:
        row.can_receive = False
        row.can_pay = False
        row.can_fund = False
        row.is_system = True
        row.show_on_dashboard = False
        db.flush()
        return row
    pm = PaymentMethod(
        name_ar=SUPPLIER_CREDIT_PM_NAME,
        kind=PaymentMethodKind.OTHER,
        is_active=True,
        sort_order=900,
        can_receive=False,
        can_pay=False,
        can_fund=False,
        is_system=True,
        show_on_dashboard=False,
    )
    db.add(pm)
    db.flush()
    return pm


def _legacy_owner_equity_names() -> tuple[str, ...]:
    return (LEGACY_OWNER_DRAW_PM_NAME, LEGACY_OWNER_CAPITAL_PM_NAME)


def ensure_owner_equity_payment_method(db: Session) -> PaymentMethod:
    """حساب نظامي واحد لحركات المالك (إيداعات وسحوبات) — حقوق ملكية وليست إيراداً أو مصروفاً."""
    unified = db.execute(
        select(PaymentMethod).where(PaymentMethod.name_ar == OWNER_EQUITY_PM_NAME)
    ).scalar_one_or_none()
    legacy_rows = list(
        db.scalars(
            select(PaymentMethod).where(
                PaymentMethod.name_ar.in_(_legacy_owner_equity_names())
            )
        ).all()
    )
    if unified is None:
        if legacy_rows:
            unified = legacy_rows[0]
            unified.name_ar = OWNER_EQUITY_PM_NAME
        else:
            unified = PaymentMethod(
                name_ar=OWNER_EQUITY_PM_NAME,
                kind=PaymentMethodKind.OTHER,
                is_active=True,
                sort_order=15,
                can_receive=False,
                can_pay=True,
                can_fund=True,
                is_system=True,
            )
            db.add(unified)
            db.flush()
    unified.can_receive = False
    unified.can_pay = True
    unified.can_fund = True
    unified.is_system = True
    unified.is_active = True
    unified.show_on_dashboard = False
    unified.sort_order = 15
    for leg in legacy_rows:
        if leg.id == unified.id:
            continue
        db.execute(
            update(PaymentTransfer)
            .where(PaymentTransfer.to_payment_method_id == leg.id)
            .values(to_payment_method_id=unified.id)
        )
        db.execute(
            update(PaymentTransfer)
            .where(PaymentTransfer.from_payment_method_id == leg.id)
            .values(from_payment_method_id=unified.id)
        )
        try:
            delete_payment_method(db, leg.id, force_legacy=True)
        except PaymentsError:
            leg.is_active = False
    db.flush()
    return unified


def is_supplier_credit_payment_method(pm: PaymentMethod | None) -> bool:
    return pm is not None and pm.name_ar == SUPPLIER_CREDIT_PM_NAME


def is_owner_equity_payment_method(pm: PaymentMethod | None) -> bool:
    if pm is None:
        return False
    if pm.name_ar == OWNER_EQUITY_PM_NAME:
        return True
    return pm.name_ar in _legacy_owner_equity_names()


def _is_equity_system_payment_method(pm: PaymentMethod | None) -> bool:
    return is_owner_equity_payment_method(pm)


def payment_method_can_receive_transfer(pm: PaymentMethod) -> bool:
    """استلام تحويلات بين الحسابات — منفصل عن قبض نقطة البيع (can_receive)."""
    if (
        is_supplier_credit_payment_method(pm)
        or is_owner_equity_payment_method(pm)
    ):
        return False
    return pm.can_fund


def list_payment_methods_for_receive(
    db: Session, *, only_active: bool = True
) -> list[PaymentMethod]:
    return [
        m
        for m in list_payment_methods(db, only_active=only_active)
        if m.can_receive
        and not is_supplier_credit_payment_method(m)
        and not is_owner_equity_payment_method(m)
    ]


def list_payment_methods_for_pay(
    db: Session, *, only_active: bool = True
) -> list[PaymentMethod]:
    return [
        m
        for m in list_payment_methods(db, only_active=only_active)
        if m.can_pay
        and not is_supplier_credit_payment_method(m)
        and not _is_equity_system_payment_method(m)
    ]


def list_payment_methods_transfer_sources(
    db: Session, *, only_active: bool = True
) -> list[PaymentMethod]:
    return [
        m
        for m in list_payment_methods(db, only_active=only_active)
        if m.can_pay and not is_supplier_credit_payment_method(m)
    ]


def list_payment_methods_for_dashboard(
    db: Session, *, only_active: bool = True
) -> list[PaymentMethod]:
    """حسابات كاش/مصرف المعروضة في لوحة الخزينة."""
    return [
        m
        for m in list_payment_methods(db, only_active=only_active)
        if m.show_on_dashboard
        and m.kind in (PaymentMethodKind.CASH, PaymentMethodKind.BANK)
    ]


def list_payment_methods_transfer_targets(
    db: Session, *, only_active: bool = True
) -> list[PaymentMethod]:
    return [
        m
        for m in list_payment_methods(db, only_active=only_active)
        if payment_method_can_receive_transfer(m)
    ]


def list_payment_methods_owner_capital_targets(
    db: Session, *, only_active: bool = True
) -> list[PaymentMethod]:
    """حسابات كاش/مصرف يمكن إيداع رأس المال فيها."""
    return list_payment_methods_transfer_targets(db, only_active=only_active)


def list_payment_methods_for_purchase_term(
    db: Session, *, only_active: bool = True
) -> list[PaymentMethod]:
    """حسابات يمكن اختيارها عند إنشاء فاتورة شراء (آجل أو دفع فوري)."""
    credit = ensure_supplier_credit_payment_method(db)
    out: list[PaymentMethod] = []
    seen: set[int] = set()
    for m in list_payment_methods_for_pay(db, only_active=only_active):
        if m.id not in seen:
            out.append(m)
            seen.add(m.id)
    if credit.id not in seen and (not only_active or credit.is_active):
        out.insert(0, credit)
    return out


def payment_method_balances_map(db: Session) -> dict[int, Decimal]:
    """أرصدة كل الحسابات — استعلام واحد بدل تكرار wallet_breakdown."""
    return {row.method.id: row.net for row in wallet_breakdown(db, None, None)}


def method_current_balance(db: Session, payment_method_id: int) -> Decimal:
    return payment_method_balances_map(db).get(payment_method_id, Decimal("0"))


def sum_purchase_payments(db: Session, purchase_id: int) -> Decimal:
    total = db.execute(
        select(func.coalesce(func.sum(PurchasePayment.amount), 0)).where(
            PurchasePayment.purchase_id == purchase_id
        )
    ).scalar_one()
    return Decimal(str(total or 0)).quantize(Decimal("0.001"))


def purchase_outstanding(db: Session, purchase: Purchase) -> Decimal:
    paid = sum_purchase_payments(db, purchase.id)
    outstanding = Decimal(str(purchase.amount or 0)) - paid
    if outstanding < 0:
        return Decimal("0")
    return outstanding.quantize(Decimal("0.001"))


def record_purchase_payment(
    db: Session,
    *,
    purchase_id: int,
    payment_method_id: int,
    amount: Decimal,
    user_id: int | None = None,
    note: str | None = None,
    payment_proof_image_filename: str | None = None,
) -> PurchasePayment:
    p = db.get(Purchase, purchase_id)
    if p is None:
        raise PaymentsError("فاتورة الشراء غير موجودة.")
    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active:
        raise PaymentsError("أسلوب الدفع غير صالح.")
    if is_supplier_credit_payment_method(pm):
        raise PaymentsError("لا يمكن السداد عبر محفظة «ذمم دائن» — اختر كاش أو مصرف.")
    if not pm.can_pay:
        raise PaymentsError(f"الحساب «{pm.name_ar}» غير مسموح بالصرف.")
    if amount <= 0:
        raise PaymentsError("مبلغ الدفع يجب أن يكون أكبر من صفر.")
    outstanding = purchase_outstanding(db, p)
    if amount > outstanding:
        raise PaymentsError(
            f"المبلغ يتجاوز المتبقي ({outstanding} د.ل)."
        )
    pp = PurchasePayment(
        purchase_id=purchase_id,
        payment_method_id=payment_method_id,
        amount=amount.quantize(Decimal("0.001")),
        note=(note or "").strip() or None,
        payment_proof_image_filename=(payment_proof_image_filename or "").strip()
        or None,
        created_by_id=user_id,
    )
    db.add(pp)
    db.flush()
    return pp


def list_payment_methods(db: Session, only_active: bool = False) -> list[PaymentMethod]:
    stmt = select(PaymentMethod).order_by(PaymentMethod.sort_order, PaymentMethod.id)
    if only_active:
        stmt = stmt.where(PaymentMethod.is_active.is_(True))
    return list(db.scalars(stmt).all())


def create_payment_method(
    db: Session,
    name_ar: str,
    kind: PaymentMethodKind,
    sort_order: int = 0,
    *,
    can_receive: bool | None = None,
    can_pay: bool | None = None,
    can_fund: bool | None = None,
    show_on_dashboard: bool | None = None,
) -> PaymentMethod:
    name = name_ar.strip()
    if not name:
        raise PaymentsError("الاسم مطلوب.")
    exists = db.execute(
        select(PaymentMethod).where(PaymentMethod.name_ar == name)
    ).scalar_one_or_none()
    if exists is not None:
        raise PaymentsError("اسم أسلوب الدفع موجود مسبقاً.")
    recv = can_receive if can_receive is not None else kind != PaymentMethodKind.OTHER
    pay = can_pay if can_pay is not None else kind != PaymentMethodKind.OTHER
    fund = (
        can_fund
        if can_fund is not None
        else kind in (PaymentMethodKind.CASH, PaymentMethodKind.BANK)
    )
    on_dash = (
        show_on_dashboard
        if show_on_dashboard is not None
        else kind in (PaymentMethodKind.CASH, PaymentMethodKind.BANK)
    )
    pm = PaymentMethod(
        name_ar=name,
        kind=kind,
        is_active=True,
        sort_order=sort_order,
        can_receive=recv,
        can_pay=pay,
        can_fund=fund,
        is_system=False,
        show_on_dashboard=on_dash,
    )
    db.add(pm)
    db.flush()
    return pm


def update_payment_method(
    db: Session,
    pm_id: int,
    *,
    name_ar: str | None = None,
    kind: PaymentMethodKind | None = None,
    is_active: bool | None = None,
    sort_order: int | None = None,
    can_receive: bool | None = None,
    can_pay: bool | None = None,
    can_fund: bool | None = None,
    show_on_dashboard: bool | None = None,
) -> PaymentMethod:
    pm = db.get(PaymentMethod, pm_id)
    if pm is None:
        raise PaymentsError("أسلوب الدفع غير موجود.")
    if pm.is_system and (name_ar is not None or kind is not None):
        raise PaymentsError("لا يمكن تعديل اسم أو نوع الحساب النظامي.")
    if name_ar is not None:
        n = name_ar.strip()
        if not n:
            raise PaymentsError("الاسم مطلوب.")
        clash = db.execute(
            select(PaymentMethod).where(
                PaymentMethod.name_ar == n, PaymentMethod.id != pm_id
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise PaymentsError("اسم أسلوب الدفع موجود مسبقاً.")
        pm.name_ar = n
    if kind is not None:
        pm.kind = kind
    if is_active is not None:
        pm.is_active = is_active
    if sort_order is not None:
        pm.sort_order = sort_order
    if is_owner_equity_payment_method(pm):
        pm.can_receive = False
        if can_receive:
            raise PaymentsError("حساب المالك لا يُستخدم في نقطة البيع.")
        if can_pay is not None:
            pm.can_pay = can_pay
        if can_fund is not None:
            pm.can_fund = can_fund
    else:
        if can_receive is not None:
            pm.can_receive = can_receive
        if can_pay is not None:
            pm.can_pay = can_pay
        if can_fund is not None:
            pm.can_fund = can_fund
    if show_on_dashboard is not None:
        if pm.is_system and show_on_dashboard:
            raise PaymentsError("لا يمكن إظهار الحسابات النظامية في لوحة الخزينة.")
        if pm.kind == PaymentMethodKind.OTHER and show_on_dashboard:
            raise PaymentsError("حسابات «أخرى» لا تُعرض في لوحة الخزينة.")
        pm.show_on_dashboard = show_on_dashboard
    db.flush()
    return pm


def payment_method_allow_delete(pm: PaymentMethod) -> bool:
    if is_supplier_credit_payment_method(pm):
        return False
    if pm.name_ar in _legacy_owner_equity_names():
        return True
    if is_owner_equity_payment_method(pm):
        return True
    if pm.is_system:
        return False
    return True


def delete_payment_method(
    db: Session, pm_id: int, *, force_legacy: bool = False
) -> None:
    from modules.delivery.models import DeliveryCashSettlement
    from modules.refunds.models import SaleReturn

    pm = db.get(PaymentMethod, pm_id)
    if pm is None:
        return
    if force_legacy:
        if pm.name_ar not in _legacy_owner_equity_names():
            raise PaymentsError("لا يمكن الحذف القسري إلا للحسابات القديمة المدمجة.")
    elif not payment_method_allow_delete(pm):
        raise PaymentsError(
            "لا يمكن حذف هذا الحساب (نظامي أو أساسي). يمكنك تعطيله من «مفعّل»."
        )
    used_in_sale = db.execute(
        select(SalePayment.id).where(SalePayment.payment_method_id == pm_id).limit(1)
    ).scalar_one_or_none()
    used_in_purchase = db.execute(
        select(Purchase.id).where(Purchase.payment_method_id == pm_id).limit(1)
    ).scalar_one_or_none()
    used_in_pp = db.execute(
        select(PurchasePayment.id)
        .where(PurchasePayment.payment_method_id == pm_id)
        .limit(1)
    ).scalar_one_or_none()
    used_in_xfer = db.execute(
        select(PaymentTransfer.id)
        .where(
            (PaymentTransfer.from_payment_method_id == pm_id)
            | (PaymentTransfer.to_payment_method_id == pm_id)
        )
        .limit(1)
    ).scalar_one_or_none()
    used_in_refund = db.execute(
        select(SaleReturn.id)
        .where(SaleReturn.refund_payment_method_id == pm_id)
        .limit(1)
    ).scalar_one_or_none()
    used_in_delivery = db.execute(
        select(DeliveryCashSettlement.id)
        .where(DeliveryCashSettlement.cash_method_id == pm_id)
        .limit(1)
    ).scalar_one_or_none()
    if (
        used_in_sale
        or used_in_purchase
        or used_in_pp
        or used_in_xfer
        or used_in_refund
        or used_in_delivery
    ):
        raise PaymentsError(
            "لا يمكن حذف الحساب لأنه مستخدم في عمليات سابقة. عطّله بدلاً من الحذف."
        )
    db.delete(pm)
    db.flush()


def record_sale_payment(
    db: Session,
    sale_id: int,
    payment_method_id: int,
    amount: Decimal,
    *,
    payment_proof_image_filename: str | None = None,
) -> SalePayment:
    """يسجّل دفعة لبيع (تستدعى مع complete_sale)."""
    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active:
        raise PaymentsError("أسلوب الدفع غير صالح.")
    if not pm.can_receive:
        raise PaymentsError(f"الحساب «{pm.name_ar}» غير مسموح بالقبض عند البيع.")
    sp = SalePayment(
        sale_id=sale_id,
        payment_method_id=payment_method_id,
        amount=amount,
        payment_proof_image_filename=(payment_proof_image_filename or "").strip()
        or None,
    )
    db.add(sp)
    db.flush()
    return sp


def list_sale_payments(db: Session, sale_id: int) -> list[SalePayment]:
    return list(
        db.scalars(
            select(SalePayment)
            .where(SalePayment.sale_id == sale_id)
            .order_by(SalePayment.created_at, SalePayment.id)
        ).all()
    )


def sum_sale_payments(db: Session, sale_id: int) -> Decimal:
    total = db.execute(
        select(func.coalesce(func.sum(SalePayment.amount), 0)).where(
            SalePayment.sale_id == sale_id
        )
    ).scalar_one()
    return Decimal(str(total or 0)).quantize(Decimal("0.001"))


def record_refund_payment(
    db: Session,
    *,
    sale_return_id: int,
    payment_method_id: int,
    amount: Decimal,
    user_id: int | None,
    note: str | None = None,
) -> RefundPayment:
    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active:
        raise PaymentsError("أسلوب الدفع غير صالح.")
    if not pm.can_pay:
        raise PaymentsError(f"الحساب «{pm.name_ar}» غير مسموح برد المبالغ.")
    if amount <= 0:
        raise PaymentsError("مبلغ المرتجع يجب أن يكون أكبر من صفر.")
    rp = RefundPayment(
        sale_return_id=sale_return_id,
        payment_method_id=payment_method_id,
        amount=amount.quantize(Decimal("0.001")),
        note=(note or "").strip() or None,
        created_by_id=user_id,
    )
    db.add(rp)
    db.flush()
    return rp


def record_payment_transfer(
    db: Session,
    *,
    sale_return_id: int,
    from_payment_method_id: int,
    to_payment_method_id: int,
    amount: Decimal,
    user_id: int | None,
    note: str | None = None,
) -> PaymentTransfer:
    return record_manual_transfer(
        db,
        from_payment_method_id=from_payment_method_id,
        to_payment_method_id=to_payment_method_id,
        amount=amount,
        user_id=user_id,
        note=note,
        transfer_type=PaymentTransferType.REFUND_SETTLEMENT,
        sale_return_id=sale_return_id,
    )


def record_manual_transfer(
    db: Session,
    *,
    from_payment_method_id: int,
    to_payment_method_id: int,
    amount: Decimal,
    user_id: int | None,
    note: str | None = None,
    transfer_type: PaymentTransferType = PaymentTransferType.MANUAL,
    sale_return_id: int | None = None,
) -> PaymentTransfer:
    from_pm = db.get(PaymentMethod, from_payment_method_id)
    to_pm = db.get(PaymentMethod, to_payment_method_id)
    if from_pm is None or to_pm is None or not from_pm.is_active or not to_pm.is_active:
        raise PaymentsError("الحساب المصدر أو الوجهة غير صالح.")
    if from_payment_method_id == to_payment_method_id:
        raise PaymentsError("لا يمكن التحويل إلى نفس الحساب.")
    if is_supplier_credit_payment_method(from_pm) or is_supplier_credit_payment_method(to_pm):
        raise PaymentsError("لا يمكن التحويل من/إلى حساب ذمم المورد الآجل.")
    if not from_pm.can_pay:
        raise PaymentsError(f"الحساب «{from_pm.name_ar}» غير مسموح بالصرف أو التحويل الصادر.")
    if not payment_method_can_receive_transfer(to_pm):
        raise PaymentsError(
            f"الحساب «{to_pm.name_ar}» غير مسموح باستلام التحويلات. "
            "فعّل «استلام تحويلات» من إدارة الحسابات."
        )
    if amount <= 0:
        raise PaymentsError("مبلغ التحويل يجب أن يكون أكبر من صفر.")
    if transfer_type == PaymentTransferType.MANUAL:
        if is_owner_equity_payment_method(to_pm) and not is_owner_equity_payment_method(
            from_pm
        ):
            transfer_type = PaymentTransferType.OWNER_DRAW
        elif is_owner_equity_payment_method(from_pm) and not is_owner_equity_payment_method(
            to_pm
        ):
            transfer_type = PaymentTransferType.OWNER_CAPITAL
    if transfer_type != PaymentTransferType.OWNER_CAPITAL:
        bal = method_current_balance(db, from_payment_method_id)
        if amount > bal:
            raise PaymentsError(
                f"الرصيد غير كافٍ في «{from_pm.name_ar}» (المتاح: {bal} د.ل)."
            )
    tf = PaymentTransfer(
        transfer_type=transfer_type,
        sale_return_id=sale_return_id,
        from_payment_method_id=from_payment_method_id,
        to_payment_method_id=to_payment_method_id,
        amount=amount.quantize(Decimal("0.001")),
        note=(note or "").strip() or None,
        created_by_id=user_id,
    )
    db.add(tf)
    db.flush()
    return tf


def record_owner_draw(
    db: Session,
    *,
    from_payment_method_id: int,
    amount: Decimal,
    user_id: int | None,
    note: str | None = None,
) -> PaymentTransfer:
    """سحب صاحب المشروع — محاسبياً: تخفيض حقوق الملكية وليس مصروفاً تشغيلياً."""
    owner = ensure_owner_equity_payment_method(db)
    return record_manual_transfer(
        db,
        from_payment_method_id=from_payment_method_id,
        to_payment_method_id=owner.id,
        amount=amount,
        user_id=user_id,
        note=(note or "").strip() or "سحب مالك",
        transfer_type=PaymentTransferType.OWNER_DRAW,
    )


def record_owner_capital(
    db: Session,
    *,
    to_payment_method_id: int,
    amount: Decimal,
    user_id: int | None,
    note: str | None = None,
) -> PaymentTransfer:
    """إيداع رأس مال من المالك — محاسبياً: زيادة حقوق الملكية وليس إيراداً."""
    capital = ensure_owner_equity_payment_method(db)
    to_pm = db.get(PaymentMethod, to_payment_method_id)
    if to_pm is None or not to_pm.is_active:
        raise PaymentsError("حساب الوجهة غير صالح.")
    if _is_equity_system_payment_method(to_pm) or is_supplier_credit_payment_method(to_pm):
        raise PaymentsError("لا يمكن الإيداع في هذا الحساب.")
    if not payment_method_can_receive_transfer(to_pm):
        raise PaymentsError(
            f"الحساب «{to_pm.name_ar}» غير مؤهل لاستلام الإيداع. "
            "استخدم حساب كاش أو مصرف مفعّل للقبض."
        )
    return record_manual_transfer(
        db,
        from_payment_method_id=capital.id,
        to_payment_method_id=to_payment_method_id,
        amount=amount,
        user_id=user_id,
        note=(note or "").strip() or "إيداع رأس مال من المالك",
        transfer_type=PaymentTransferType.OWNER_CAPITAL,
    )


def _apply_purchase_payment_at_create(
    db: Session,
    purchase: Purchase,
    pm: PaymentMethod,
    *,
    user_id: int | None,
    pay_amount: Decimal | None = None,
    payment_proof_image_filename: str | None = None,
) -> None:
    """يسجّل دفعة عند إنشاء الفاتورة (كامل أو جزئي) ما لم تكن آجلة بالكامل."""
    if is_supplier_credit_payment_method(pm):
        return
    total = Decimal(str(purchase.amount or 0))
    amt = pay_amount if pay_amount is not None else total
    if amt <= 0:
        return
    if amt > total:
        amt = total
    record_purchase_payment(
        db,
        purchase_id=purchase.id,
        payment_method_id=pm.id,
        amount=amt,
        user_id=user_id,
        payment_proof_image_filename=payment_proof_image_filename,
    )


def record_expense(
    db: Session,
    *,
    payment_method_id: int,
    amount: Decimal,
    expense_category: str | None,
    supplier: str | None,
    note: str | None,
    user_id: int | None,
    created_at: datetime | None = None,
    supplier_invoice_ref: str | None = None,
    invoice_image_filename: str | None = None,
) -> Purchase:
    """مصروف عام (إيجار/راتب/فاتورة) — لا يضاف للمخزون."""
    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active:
        raise PaymentsError("أسلوب الدفع غير صالح.")
    if not pm.can_pay:
        raise PaymentsError(f"الحساب «{pm.name_ar}» غير مسموح بالصرف.")
    if amount <= 0:
        raise PaymentsError("المبلغ يجب أن يكون أكبر من صفر.")
    ref = (supplier_invoice_ref or "").strip() or None
    p = Purchase(
        payment_method_id=payment_method_id,
        kind=PurchaseKind.EXPENSE,
        amount=amount,
        expense_category=(expense_category or "").strip() or None,
        supplier=(supplier or "").strip() or None,
        supplier_invoice_ref=ref,
        invoice_image_filename=(invoice_image_filename or "").strip() or None,
        note=(note or "").strip() or None,
        created_by_id=user_id,
    )
    if created_at is not None:
        p.created_at = created_at
    db.add(p)
    db.flush()
    _apply_purchase_payment_at_create(db, p, pm, user_id=user_id)
    return p


def record_asset_purchase(
    db: Session,
    *,
    payment_method_id: int,
    supplier: str | None,
    note: str | None,
    lines: list[tuple[str, str | None, Decimal, Decimal, int, Decimal]],
    user_id: int | None,
    created_at: datetime | None = None,
    supplier_invoice_ref: str | None = None,
    invoice_image_filename: str | None = None,
) -> Purchase:
    """فاتورة أصول/أدوات للشركة (لا تباع، لا تأثير على المخزون).
    lines = [(item_name, unit, quantity, unit_cost, useful_life_months, salvage_value), ...]
    - useful_life_months = 0 → بند استهلاكي (مصروف فوري في شهر الشراء).
    - useful_life_months > 0 → أصل ثابت يُهلَك على فترة العمر الإنتاجي (القسط الثابت).
    """
    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active:
        raise PaymentsError("أسلوب الدفع غير صالح.")
    if not pm.can_pay:
        raise PaymentsError(f"الحساب «{pm.name_ar}» غير مسموح بالصرف.")
    if not lines:
        raise PaymentsError("أضف بنداً واحداً على الأقل.")

    total = Decimal("0")
    cleaned: list[tuple[str, str | None, Decimal, Decimal, int, Decimal]] = []
    for name, unit, qty, cost, life, salvage in lines:
        nm = (name or "").strip()
        if not nm:
            raise PaymentsError("اكتب اسم الأداة/الأصل لكل بند.")
        if qty <= 0:
            raise PaymentsError("الكمية يجب أن تكون أكبر من صفر.")
        if cost < 0:
            raise PaymentsError("سعر الوحدة لا يمكن أن يكون سالباً.")
        if life < 0:
            raise PaymentsError("العمر الإنتاجي لا يمكن أن يكون سالباً.")
        if salvage < 0:
            raise PaymentsError("قيمة الخردة لا يمكن أن تكون سالبة.")
        line_total = (qty * cost).quantize(Decimal("0.001"))
        if salvage > line_total:
            raise PaymentsError(
                f"قيمة الخردة لـ «{nm}» ({salvage}) لا يمكن أن تتجاوز إجمالي البند ({line_total})."
            )
        u = (unit or "").strip() or None
        total += line_total
        cleaned.append((nm, u, qty, cost, int(life), salvage))
    total = total.quantize(Decimal("0.001"))
    if total <= 0:
        raise PaymentsError("إجمالي الفاتورة يجب أن يكون أكبر من صفر.")

    ref = (supplier_invoice_ref or "").strip() or None
    p = Purchase(
        payment_method_id=payment_method_id,
        kind=PurchaseKind.ASSET,
        amount=total,
        supplier=(supplier or "").strip() or None,
        supplier_invoice_ref=ref,
        invoice_image_filename=(invoice_image_filename or "").strip() or None,
        note=(note or "").strip() or None,
        created_by_id=user_id,
    )
    if created_at is not None:
        p.created_at = created_at
    db.add(p)
    db.flush()

    for name, unit, qty, cost, life, salvage in cleaned:
        pl = PurchaseLine(
            purchase_id=p.id,
            product_id=None,
            item_name=name,
            unit=unit,
            quantity=qty.quantize(Decimal("0.0001")),
            unit_cost=cost.quantize(Decimal("0.001")),
            line_total=(qty * cost).quantize(Decimal("0.001")),
            useful_life_months=life,
            salvage_value=salvage.quantize(Decimal("0.001")),
        )
        db.add(pl)
    db.flush()
    _apply_purchase_payment_at_create(db, p, pm, user_id=user_id)
    return p


def record_inventory_purchase(
    db: Session,
    *,
    payment_method_id: int,
    supplier: str | None,
    note: str | None,
    lines: list[tuple[int, Decimal, Decimal]],
    user_id: int | None,
    warehouse_id: int,
    created_at: datetime | None = None,
    supplier_invoice_ref: str | None = None,
    invoice_image_filename: str | None = None,
    payment_proof_image_filename: str | None = None,
    pay_now_amount: Decimal | None = None,
    pay_now_method_id: int | None = None,
) -> Purchase:
    """فاتورة شراء بضاعة: lines = [(product_id, quantity, unit_cost), ...].
    تُضاف الكميات إلى رصيد المخزون عبر apply_movement (PURCHASE)،
    والإجمالي يخصم من المحفظة.
    - يُسمح فقط بمكوّنات المخزون (STOCK_ONLY)؛ المنتجات النهائية لا تُشترى لأنها
      تُصنَّع داخلياً من مكوّناتها.
    """
    from modules.catalog.models import Product, ProductKind
    from modules.inventory.service import WarehouseError, resolve_warehouse_id

    try:
        wid = resolve_warehouse_id(db, warehouse_id)
    except WarehouseError as e:
        raise PaymentsError(str(e)) from e

    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active:
        raise PaymentsError("أسلوب الدفع غير صالح.")
    if not lines:
        raise PaymentsError("أضف بنداً واحداً على الأقل لفاتورة الشراء.")

    pids = [pid for pid, _q, _c in lines]
    bad_products = list(
        db.scalars(
            select(Product).where(Product.id.in_(pids), Product.kind != ProductKind.STOCK_ONLY)
        ).all()
    )
    if bad_products:
        names = [bp.name_ar for bp in bad_products]
        raise PaymentsError(
            "لا يمكن شراء منتجات نهائية تُباع كما هي: "
            + "، ".join(names)
            + ". اشترِ مكوّنات المخزون التي تُصنَّع منها."
        )

    total = Decimal("0")
    for _pid, qty, cost in lines:
        if qty <= 0:
            raise PaymentsError("الكمية يجب أن تكون أكبر من صفر.")
        if cost < 0:
            raise PaymentsError("سعر الوحدة لا يمكن أن يكون سالباً.")
        total += (qty * cost)
    total = total.quantize(Decimal("0.001"))
    if total <= 0:
        raise PaymentsError("إجمالي الفاتورة يجب أن يكون أكبر من صفر.")

    ref = (supplier_invoice_ref or "").strip() or None
    p = Purchase(
        payment_method_id=payment_method_id,
        kind=PurchaseKind.INVENTORY,
        amount=total,
        supplier=(supplier or "").strip() or None,
        supplier_invoice_ref=ref,
        invoice_image_filename=(invoice_image_filename or "").strip() or None,
        payment_proof_image_filename=(payment_proof_image_filename or "").strip()
        or None,
        note=(note or "").strip() or None,
        created_by_id=user_id,
    )
    if created_at is not None:
        p.created_at = created_at
    db.add(p)
    db.flush()

    for pid, qty, cost in lines:
        line_total = (qty * cost).quantize(Decimal("0.001"))
        pl = PurchaseLine(
            purchase_id=p.id,
            product_id=pid,
            quantity=qty.quantize(Decimal("0.0001")),
            unit_cost=cost.quantize(Decimal("0.001")),
            line_total=line_total,
        )
        db.add(pl)
        # إضافة الكمية إلى المخزون
        apply_movement(
            db,
            product_id=pid,
            quantity_delta=qty,
            movement_type=StockMovementType.PURCHASE,
            user_id=user_id,
            warehouse_id=wid,
            note=f"شراء فاتورة #{p.id}",
        )
    db.flush()
    if is_supplier_credit_payment_method(pm):
        if pay_now_method_id and pay_now_amount and pay_now_amount > 0:
            pay_pm = db.get(PaymentMethod, pay_now_method_id)
            if pay_pm is None or not pay_pm.is_active:
                raise PaymentsError("محفظة الدفع الفوري غير صالحة.")
            record_purchase_payment(
                db,
                purchase_id=p.id,
                payment_method_id=pay_pm.id,
                amount=pay_now_amount,
                user_id=user_id,
                payment_proof_image_filename=payment_proof_image_filename,
            )
    else:
        _apply_purchase_payment_at_create(
            db,
            p,
            pm,
            user_id=user_id,
            pay_amount=pay_now_amount,
            payment_proof_image_filename=payment_proof_image_filename,
        )
    return p


def delete_purchase(db: Session, purchase_id: int) -> None:
    """يحذف عملية صرف. لو كانت INVENTORY: نخصم الكميات من المخزون مرة أخرى."""
    p = db.get(Purchase, purchase_id)
    if p is None:
        return
    static_root = Path(__file__).resolve().parents[2] / "app" / "static"
    delete_stored_relative_file(static_root, p.invoice_image_filename)
    delete_stored_relative_file(static_root, p.payment_proof_image_filename)
    if p.kind == PurchaseKind.INVENTORY:
        wid = p.warehouse_id
        from modules.inventory.service import get_main_warehouse

        if wid is None:
            wid = get_main_warehouse(db).id
        for ln in p.lines:
            apply_movement(
                db,
                product_id=ln.product_id,
                quantity_delta=-ln.quantity,
                movement_type=StockMovementType.ADJUSTMENT,
                user_id=None,
                warehouse_id=wid,
                note=f"إلغاء شراء فاتورة #{p.id}",
            )
    db.delete(p)
    db.flush()


def get_sale_payment(db: Session, sale_id: int) -> SalePayment | None:
    return db.execute(
        select(SalePayment)
        .where(SalePayment.sale_id == sale_id)
        .order_by(SalePayment.created_at, SalePayment.id)
    ).scalars().first()


# --- Wallet calculations ---


@dataclass
class WalletRow:
    method: PaymentMethod
    sales_in: Decimal
    refunds_out: Decimal
    transfers_in: Decimal
    transfers_out: Decimal
    delivery_fees_out: Decimal
    purchases_out: Decimal
    net: Decimal


def wallet_breakdown(
    db: Session, start: datetime | None, end: datetime | None
) -> list[WalletRow]:
    """يحسب لكل أسلوب دفع: مدخل المبيعات، المرتجعات، التسويات، خرج المشتريات، الصافي خلال الفترة.
    إن كانت start/end None: حسبة كل الوقت."""
    methods = list_payment_methods(db, only_active=False)

    sales_q = (
        select(SalePayment.payment_method_id, func.coalesce(func.sum(SalePayment.amount), 0))
        .group_by(SalePayment.payment_method_id)
    )
    if start is not None:
        sales_q = sales_q.where(SalePayment.created_at >= start)
    if end is not None:
        sales_q = sales_q.where(SalePayment.created_at < end)
    sales_map: dict[int, Decimal] = {
        int(mid): Decimal(str(s or 0)) for mid, s in db.execute(sales_q).all()
    }

    refunds_q = (
        select(
            RefundPayment.payment_method_id,
            func.coalesce(func.sum(RefundPayment.amount), 0),
        )
        .group_by(RefundPayment.payment_method_id)
    )
    if start is not None:
        refunds_q = refunds_q.where(RefundPayment.created_at >= start)
    if end is not None:
        refunds_q = refunds_q.where(RefundPayment.created_at < end)
    refunds_map: dict[int, Decimal] = {
        int(mid): Decimal(str(s or 0)) for mid, s in db.execute(refunds_q).all()
    }

    transfers_in_q = (
        select(
            PaymentTransfer.to_payment_method_id,
            func.coalesce(func.sum(PaymentTransfer.amount), 0),
        )
        .group_by(PaymentTransfer.to_payment_method_id)
    )
    if start is not None:
        transfers_in_q = transfers_in_q.where(PaymentTransfer.created_at >= start)
    if end is not None:
        transfers_in_q = transfers_in_q.where(PaymentTransfer.created_at < end)
    transfers_in_map: dict[int, Decimal] = {
        int(mid): Decimal(str(s or 0))
        for mid, s in db.execute(transfers_in_q).all()
    }

    transfers_out_q = (
        select(
            PaymentTransfer.from_payment_method_id,
            func.coalesce(func.sum(PaymentTransfer.amount), 0),
        )
        .group_by(PaymentTransfer.from_payment_method_id)
    )
    if start is not None:
        transfers_out_q = transfers_out_q.where(PaymentTransfer.created_at >= start)
    if end is not None:
        transfers_out_q = transfers_out_q.where(PaymentTransfer.created_at < end)
    transfers_out_map: dict[int, Decimal] = {
        int(mid): Decimal(str(s or 0))
        for mid, s in db.execute(transfers_out_q).all()
    }

    delivery_fees_q = (
        select(
            DeliveryCashSettlement.cash_method_id,
            func.coalesce(func.sum(DeliveryCashSettlement.amount), 0),
        )
        .group_by(DeliveryCashSettlement.cash_method_id)
    )
    if start is not None:
        delivery_fees_q = delivery_fees_q.where(DeliveryCashSettlement.created_at >= start)
    if end is not None:
        delivery_fees_q = delivery_fees_q.where(DeliveryCashSettlement.created_at < end)
    delivery_fees_map: dict[int, Decimal] = {
        int(mid): Decimal(str(s or 0)) for mid, s in db.execute(delivery_fees_q).all()
    }

    purchases_q = select(
        PurchasePayment.payment_method_id,
        func.coalesce(func.sum(PurchasePayment.amount), 0),
    ).group_by(PurchasePayment.payment_method_id)
    if start is not None:
        purchases_q = purchases_q.where(PurchasePayment.created_at >= start)
    if end is not None:
        purchases_q = purchases_q.where(PurchasePayment.created_at < end)
    purchases_map: dict[int, Decimal] = {
        int(mid): Decimal(str(s or 0)) for mid, s in db.execute(purchases_q).all()
    }

    rows: list[WalletRow] = []
    for m in methods:
        sin = sales_map.get(m.id, Decimal("0"))
        rout = refunds_map.get(m.id, Decimal("0"))
        tin = transfers_in_map.get(m.id, Decimal("0"))
        tout = transfers_out_map.get(m.id, Decimal("0"))
        dfout = delivery_fees_map.get(m.id, Decimal("0"))
        pout = purchases_map.get(m.id, Decimal("0"))
        rows.append(
            WalletRow(
                method=m,
                sales_in=sin,
                refunds_out=rout,
                transfers_in=tin,
                transfers_out=tout,
                delivery_fees_out=dfout,
                purchases_out=pout,
                net=(sin + tin - rout - tout - dfout - pout).quantize(Decimal("0.001")),
            )
        )
    return rows


@dataclass
class PurchasesSummary:
    count: int
    total: Decimal


def _purchases_summary_q(
    db: Session, start: datetime, end: datetime, kind: PurchaseKind | None
) -> PurchasesSummary:
    stmt = select(
        func.count(Purchase.id), func.coalesce(func.sum(Purchase.amount), 0)
    ).where(Purchase.created_at >= start, Purchase.created_at < end)
    if kind is not None:
        stmt = stmt.where(Purchase.kind == kind)
    cnt, total = db.execute(stmt).one()
    return PurchasesSummary(count=int(cnt or 0), total=Decimal(str(total or 0)))


def purchases_summary(
    db: Session, start: datetime, end: datetime
) -> PurchasesSummary:
    """ملخص جميع عمليات الصرف (شراء + مصروف)."""
    return _purchases_summary_q(db, start, end, None)


def inventory_purchases_summary(
    db: Session, start: datetime, end: datetime
) -> PurchasesSummary:
    return _purchases_summary_q(db, start, end, PurchaseKind.INVENTORY)


def expenses_summary(
    db: Session, start: datetime, end: datetime
) -> PurchasesSummary:
    return _purchases_summary_q(db, start, end, PurchaseKind.EXPENSE)


def list_purchases(
    db: Session,
    start: datetime,
    end: datetime,
    kind: PurchaseKind | None = None,
    limit: int = 500,
) -> list[Purchase]:
    stmt = (
        select(Purchase)
        .where(Purchase.created_at >= start, Purchase.created_at < end)
        .order_by(Purchase.created_at.desc(), Purchase.id.desc())
        .limit(limit)
    )
    if kind is not None:
        stmt = stmt.where(Purchase.kind == kind)
    return list(db.scalars(stmt).all())
