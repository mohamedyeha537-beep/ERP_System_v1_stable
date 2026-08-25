"""رقم واحد للخزينة في كل الشاشات: رصيد أسلوب الدفع المربوط."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.gl.hierarchy import display_balances_map
from modules.gl.models import GlAccount, GlPaymentMethodMap

_ZERO = Decimal("0")


def _is_vault_code(code: str) -> bool:
    c = (code or "").strip()
    return c.startswith(("111", "112", "113"))


def mapped_vault_wallet_totals(db: Session) -> dict[int, Decimal]:
    """مجموع المحافظ التشغيلية لكل حساب خزينة مربوط."""
    from modules.payments.service import payment_method_balances_map

    pm_bals = payment_method_balances_map(db)
    totals: dict[int, Decimal] = {}
    maps = list(db.scalars(select(GlPaymentMethodMap)).all())
    if not maps:
        return totals
    acc_ids = {int(m.gl_account_id) for m in maps if m.gl_account_id is not None}
    accounts = {
        int(a.id): a
        for a in db.scalars(select(GlAccount).where(GlAccount.id.in_(acc_ids))).all()
    } if acc_ids else {}
    for m in maps:
        if m.gl_account_id is None or m.payment_method_id is None:
            continue
        acc = accounts.get(int(m.gl_account_id))
        if acc is None or not _is_vault_code(str(acc.code or "")):
            continue
        aid = int(m.gl_account_id)
        totals[aid] = (
            totals.get(aid, _ZERO) + Decimal(str(pm_bals.get(int(m.payment_method_id), _ZERO)))
        ).quantize(Decimal("0.001"))
    return totals


def overlay_vault_wallet_balances(
    db: Session, raw: dict[int, Decimal]
) -> dict[int, Decimal]:
    """يستبدل رصيد حسابات الخزينة المربوطة برصيد المحفظة ثم يعيد التجميع."""
    overlaid = dict(raw)
    overlaid.update(mapped_vault_wallet_totals(db))
    return display_balances_map(db, overlaid)


def vault_display_balance(db: Session, account_id: int, raw: dict[int, Decimal] | None = None) -> Decimal:
    totals = mapped_vault_wallet_totals(db)
    if account_id in totals:
        return totals[account_id]
    if raw is None:
        from modules.gl.service import account_balances_map

        raw = account_balances_map(db, domain=None)
    return display_balances_map(db, raw).get(int(account_id), _ZERO)
