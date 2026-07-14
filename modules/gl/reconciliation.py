"""مطابقة GL مع المحافظ التشغيلية."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.gl.models import GlAccount, GlPaymentMethodMap
from modules.gl.posting import CODE_AP, CODE_AR
from modules.gl.service import account_balance
from modules.payments.service import method_current_balance
from modules.platform.business_domain import (
    BusinessDomain,
    payment_method_visible_for_domain,
)
from modules.receivables.service import receivables_summary


@dataclass
class WalletGlRow:
    payment_method_id: int
    payment_method_name: str
    gl_account_code: str | None
    gl_account_name: str | None
    operational_balance: Decimal
    gl_balance: Decimal
    difference: Decimal


@dataclass
class GlReconciliationSummary:
    wallet_rows: list[WalletGlRow]
    ar_operational: Decimal
    ar_gl: Decimal
    ar_difference: Decimal
    ap_note: str


def _account_balance_by_code(db: Session, code: str, domain=None) -> Decimal:
    acc_id = db.scalar(select(GlAccount.id).where(GlAccount.code == code))
    if acc_id is None:
        return Decimal("0")
    return account_balance(db, int(acc_id), domain=domain)


def build_reconciliation(db: Session, domain=None) -> GlReconciliationSummary:
    maps = list(
        db.scalars(
            select(GlPaymentMethodMap).order_by(GlPaymentMethodMap.payment_method_id)
        ).all()
    )
    wallet_rows: list[WalletGlRow] = []
    for m in maps:
        pm = m.payment_method
        gl = m.gl_account
        if pm is None or gl is None:
            continue
        pm_dom = getattr(pm, "business_domain", BusinessDomain.SHARED)
        if domain is not None and not payment_method_visible_for_domain(
            pm_dom, filter_domain=domain
        ):
            continue
        op = method_current_balance(db, int(pm.id))
        gl_bal = account_balance(db, int(gl.id), domain=domain)
        wallet_rows.append(
            WalletGlRow(
                payment_method_id=int(pm.id),
                payment_method_name=pm.name_ar,
                gl_account_code=gl.code,
                gl_account_name=gl.name_ar,
                operational_balance=op,
                gl_balance=gl_bal,
                difference=(op - gl_bal).quantize(Decimal("0.001")),
            )
        )

    ar_ops = receivables_summary(db).total_outstanding if domain in (
        None,
        BusinessDomain.RESTAURANT,
    ) else Decimal("0")
    ar_gl = _account_balance_by_code(db, CODE_AR, domain=domain)
    return GlReconciliationSummary(
        wallet_rows=wallet_rows,
        ar_operational=ar_ops,
        ar_gl=ar_gl,
        ar_difference=(ar_ops - ar_gl).quantize(Decimal("0.001")),
        ap_note=f"رصيد GL لحساب {CODE_AP} (ذمم مورد): {_account_balance_by_code(db, CODE_AP, domain=domain)} — "
        "يُقارن لاحقاً مع تقرير الذمم الدائنة.",
    )
