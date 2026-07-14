"""ربط حساب GL بمحفظة POS (جلسة البيع) — دون صفحة «أساليب الدفع»."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.gl.hierarchy import assert_postable_account, is_header_account
from modules.gl.models import GlAccount, GlAccountType, GlPaymentMethodMap
from modules.gl.service import GLError, set_wallet_map
from modules.payments.models import PaymentMethod, PaymentMethodKind
from modules.payments.service import PaymentsError, create_payment_method


def _kind_for_gl_account(acc: GlAccount) -> PaymentMethodKind | None:
    code = (acc.code or "").strip()
    if code.startswith("112"):
        return PaymentMethodKind.BANK
    if code.startswith("111"):
        return PaymentMethodKind.CASH
    if acc.account_type != GlAccountType.ASSET:
        return None
    return PaymentMethodKind.CASH


def provision_pos_wallet_for_gl_account(db: Session, account_id: int) -> PaymentMethod:
    """ينشئ (أو يعيد) محفظة POS ويربطها بحساب GL — للتحصيل في جلسة البيع."""
    acc = db.get(GlAccount, account_id)
    if acc is None or not acc.is_active:
        raise GLError("الحساب غير موجود أو غير نشط.")
    if is_header_account(db, int(acc.id)):
        raise GLError("الحساب تجميعي — اختر حساباً فرعياً.")
    try:
        assert_postable_account(db, int(acc.id))
    except ValueError as exc:
        raise GLError(str(exc)) from exc

    kind = _kind_for_gl_account(acc)
    if kind is None:
        raise GLError(
            "يمكن ربط جلسة البيع فقط لحسابات أصول نقد/مصرف (مثل 1110، 1121)."
        )

    existing_map = db.scalar(
        select(GlPaymentMethodMap).where(
            GlPaymentMethodMap.gl_account_id == int(acc.id)
        )
    )
    if existing_map is not None:
        pm = db.get(PaymentMethod, int(existing_map.payment_method_id))
        if pm is not None:
            pm.is_active = True
            pm.can_receive = True
            pm.can_pay = True
            pm.can_fund = True
            pm.show_on_dashboard = False
            acc.show_on_dashboard = True
            db.flush()
            return pm

    label = f"{acc.code} — {acc.name_ar}".strip()
    try:
        pm = create_payment_method(
            db,
            label[:160],
            kind,
            sort_order=int(acc.sort_order or 0),
            can_receive=True,
            can_pay=True,
            can_fund=True,
            show_on_dashboard=False,
        )
    except PaymentsError as exc:
        dup = db.scalar(
            select(PaymentMethod).where(PaymentMethod.name_ar == label[:160])
        )
        if dup is None:
            raise GLError(str(exc)) from exc
        pm = dup

    set_wallet_map(db, int(pm.id), int(acc.id))
    acc.show_on_dashboard = True
    db.flush()
    return pm


def hide_wallets_from_dashboard_when_gl(db: Session) -> None:
    """إخفاء بطاقات المحافظ من اللوحة — العرض من GL فقط."""
    from modules.gl.service import is_gl_enabled

    if not is_gl_enabled(db):
        return
    for pm in db.scalars(select(PaymentMethod)).all():
        if pm.show_on_dashboard:
            pm.show_on_dashboard = False
    db.flush()
