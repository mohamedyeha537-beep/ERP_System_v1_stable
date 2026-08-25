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
    wallet_ops_total: Decimal | None = None
    wallet_gl_diff: Decimal | None = None
    gl_book_balance: Decimal | None = None


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


def gl_dashboard_treasury_cards(db: Session, domain=None) -> list[GlDashboardCard]:
    """خزائن النقد = رصيد أسلوب الدفع نفسه المستخدم في التحويل. الذمم من GL."""
    from modules.gl.domain import filter_gl_accounts
    from modules.payments.service import (
        ensure_hotel_treasury_payment_methods,
        list_payment_methods_for_dashboard,
        payment_method_balances_map,
    )
    from modules.payments.shift_handoff_service import ensure_main_treasury_payment_methods

    if not is_gl_enabled(db):
        return []
    ensure_main_treasury_payment_methods(db)
    ensure_hotel_treasury_payment_methods(db)
    headers = header_account_ids(db)
    pm_balances = payment_method_balances_map(db)
    pm_to_gl: dict[int, GlAccount] = {}
    for m in db.scalars(
        select(GlPaymentMethodMap).options(selectinload(GlPaymentMethodMap.gl_account))
    ).all():
        if m.payment_method_id is not None and m.gl_account is not None:
            pm_to_gl[int(m.payment_method_id)] = m.gl_account

    cards: list[GlDashboardCard] = []
    seen_pm: set[int] = set()
    for pm in list_payment_methods_for_dashboard(db, only_active=True, domain=domain):
        seen_pm.add(int(pm.id))
        gl = pm_to_gl.get(int(pm.id))
        kind = "bank" if pm.kind.value == "BANK" else "cash"
        cards.append(
            GlDashboardCard(
                account_id=int(gl.id) if gl is not None else 0,
                code=str(gl.code) if gl is not None else "",
                name_ar=str(gl.name_ar) if gl is not None else str(pm.name_ar),
                card_kind=kind,
                balance=Decimal(str(pm_balances.get(int(pm.id), _ZERO))).quantize(
                    Decimal("0.001")
                ),
                wallet_pm_id=int(pm.id),
                wallet_names=[str(pm.name_ar)],
            )
        )

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
    from modules.gl.hierarchy import display_balances_map

    shown_gl_ids = {int(c.account_id) for c in cards if c.account_id}
    gl_bals = display_balances_map(db, account_balances_map(db, domain=None))
    for acc in accounts:
        if acc.id in headers or is_header_account(db, int(acc.id)):
            continue
        kind = _card_kind(acc)
        if kind in ("cash", "bank"):
            continue
        if int(acc.id) in shown_gl_ids:
            continue
        cards.append(
            GlDashboardCard(
                account_id=int(acc.id),
                code=str(acc.code),
                name_ar=str(acc.name_ar),
                card_kind=kind,
                balance=gl_bals.get(int(acc.id), _ZERO),
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
