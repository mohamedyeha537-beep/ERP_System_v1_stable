"""بطاقات لوحة التحكم — حسابات GL بدلاً من تكرار المحافظ."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from modules.gl.hierarchy import display_balances_map, header_account_ids, is_header_account
from modules.gl.models import GlAccount, GlAccountType, GlPaymentMethodMap
from modules.gl.service import account_balances_map, is_gl_enabled

_ZERO = Decimal("0")

_DEFAULT_DASHBOARD_CODES = frozenset({"1110", "1120", "1200", "2100"})


@dataclass
class GlDashboardCard:
    account_id: int
    code: str
    name_ar: str
    card_kind: str  # cash | bank | ar | ap | asset | liability | equity
    balance: Decimal
    wallet_pm_id: int | None = None
    wallet_names: list[str] | None = None


def _card_kind(acc: GlAccount) -> str:
    code = (acc.code or "").strip()
    if code.startswith("112"):
        return "bank"
    if code.startswith("111"):
        return "cash"
    if code.startswith("120"):
        return "ar"
    if code.startswith("210"):
        return "ap"
    if acc.account_type == GlAccountType.LIABILITY:
        return "ap"
    if acc.account_type == GlAccountType.EQUITY:
        return "equity"
    if acc.account_type == GlAccountType.ASSET:
        return "asset"
    return "asset"


def _wallet_info(db: Session, account_id: int) -> tuple[int | None, list[str]]:
    maps = list(
        db.scalars(
            select(GlPaymentMethodMap)
            .where(GlPaymentMethodMap.gl_account_id == account_id)
            .options(selectinload(GlPaymentMethodMap.payment_method))
            .order_by(GlPaymentMethodMap.payment_method_id)
        ).all()
    )
    names: list[str] = []
    first_pm: int | None = None
    for m in maps:
        pm = m.payment_method
        if pm is None:
            continue
        if first_pm is None:
            first_pm = int(pm.id)
        names.append(str(pm.name_ar))
    return first_pm, names


def gl_dashboard_treasury_cards(db: Session, domain=None) -> list[GlDashboardCard]:
    """حسابات GL المعروضة في بلوك «الخزينة والذمم» — رصيد من الدفتر وليس المحفظة التشغيلية."""
    from modules.gl.domain import filter_gl_accounts

    if not is_gl_enabled(db):
        return []
    headers = header_account_ids(db)
    raw = account_balances_map(db, domain=domain)
    balances = display_balances_map(db, raw)
    accounts = filter_gl_accounts(
        list(
            db.scalars(
                select(GlAccount)
                .where(GlAccount.is_active.is_(True), GlAccount.show_on_dashboard.is_(True))
                .order_by(GlAccount.sort_order, GlAccount.code)
            ).all()
        ),
        domain,
    )
    cards: list[GlDashboardCard] = []
    for acc in accounts:
        if acc.id in headers or is_header_account(db, int(acc.id)):
            continue
        pm_id, pm_names = _wallet_info(db, int(acc.id))
        cards.append(
            GlDashboardCard(
                account_id=int(acc.id),
                code=str(acc.code),
                name_ar=str(acc.name_ar),
                card_kind=_card_kind(acc),
                balance=balances.get(int(acc.id), _ZERO),
                wallet_pm_id=pm_id,
                wallet_names=pm_names or None,
            )
        )
    return cards


def ensure_gl_dashboard_column(db: Session) -> None:
    """ترقية قاعدة قديمة — عمود show_on_dashboard قبل أي استخدام للنموذج."""
    from sqlalchemy import inspect, text

    bind = db.get_bind()
    if bind is None:
        return
    insp = inspect(bind)
    if "gl_accounts" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("gl_accounts")}
    if "show_on_dashboard" not in cols:
        db.execute(
            text(
                "ALTER TABLE gl_accounts ADD COLUMN show_on_dashboard BOOLEAN NOT NULL DEFAULT 0"
            )
        )
        db.flush()


def ensure_gl_dashboard_defaults(db: Session) -> None:
    """يفعّل الحسابات الافتراضية للوحة التحكم."""
    ensure_gl_dashboard_column(db)

    mapped_ids = {
        int(x)
        for x in db.scalars(select(GlPaymentMethodMap.gl_account_id)).all()
        if x is not None
    }
    for acc in db.scalars(select(GlAccount)).all():
        if acc.code in _DEFAULT_DASHBOARD_CODES or int(acc.id) in mapped_ids:
            if not acc.show_on_dashboard and not is_header_account(db, int(acc.id)):
                acc.show_on_dashboard = True
    db.flush()
