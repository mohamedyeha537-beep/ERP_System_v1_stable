"""تقارير مالية من دفتر الأستاذ العام — مصدر الحقيقة المحاسبية."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.gl.models import GlAccount, GlAccountType, GlJournalEntry, GlJournalEntryStatus, GlJournalLine
from modules.gl.seed import account_type_label
from modules.gl.hierarchy import filter_leaf_accounts, header_account_ids
from modules.gl.domain import filter_gl_accounts, gl_entry_db_values
from modules.gl.service import ACCOUNT_TYPE_ORDER, get_opening_balance, list_accounts

_ZERO = Decimal("0")


def _q(v: Decimal) -> Decimal:
    return Decimal(str(v or 0)).quantize(Decimal("0.001"))


def _posted_entry_filter():
    return GlJournalEntry.status == GlJournalEntryStatus.POSTED


def _entry_domain_filter(filter_domain):
    vals = gl_entry_db_values(filter_domain)
    if vals is None:
        return True
    return GlJournalEntry.business_domain.in_(vals)


def account_balance_as_of(
    db: Session, account_id: int, as_of: date, domain=None
) -> Decimal:
    net = db.scalar(
        select(func.coalesce(func.sum(GlJournalLine.debit - GlJournalLine.credit), 0))
        .select_from(GlJournalLine)
        .join(GlJournalEntry, GlJournalEntry.id == GlJournalLine.entry_id)
        .where(
            GlJournalLine.account_id == account_id,
            _posted_entry_filter(),
            GlJournalEntry.entry_date <= as_of,
            _entry_domain_filter(domain),
        )
    )
    return (_q(Decimal(str(net or 0))) + get_opening_balance(db, account_id, domain=domain)).quantize(Decimal("0.001"))


def account_period_movement(
    db: Session, account_id: int, from_date: date, to_date: date, domain=None
) -> tuple[Decimal, Decimal]:
    """(إجمالي مدين، إجمالي دائن) في الفترة."""
    row = db.execute(
        select(
            func.coalesce(func.sum(GlJournalLine.debit), 0),
            func.coalesce(func.sum(GlJournalLine.credit), 0),
        )
        .select_from(GlJournalLine)
        .join(GlJournalEntry, GlJournalEntry.id == GlJournalLine.entry_id)
        .where(
            GlJournalLine.account_id == account_id,
            _posted_entry_filter(),
            GlJournalEntry.entry_date >= from_date,
            GlJournalEntry.entry_date <= to_date,
            _entry_domain_filter(domain),
        )
    ).one()
    return _q(Decimal(str(row[0] or 0))), _q(Decimal(str(row[1] or 0)))


def _tb_columns(acc_type: GlAccountType, balance: Decimal) -> tuple[Decimal, Decimal]:
    """رصيد الحساب في عمودي مدين/دائن لميزان المراجعة."""
    if balance == _ZERO:
        return _ZERO, _ZERO
    if acc_type in (GlAccountType.ASSET, GlAccountType.EXPENSE):
        if balance > 0:
            return balance, _ZERO
        return _ZERO, -balance
    if balance < 0:
        return _ZERO, -balance
    return balance, _ZERO


@dataclass
class TrialBalanceRow:
    account: GlAccount
    period_debit: Decimal
    period_credit: Decimal
    balance: Decimal
    tb_debit: Decimal
    tb_credit: Decimal


@dataclass
class TrialBalanceReport:
    as_of: date
    from_date: date
    rows: list[TrialBalanceRow]
    total_debit: Decimal
    total_credit: Decimal
    balanced: bool


def build_trial_balance(
    db: Session, *, from_date: date, to_date: date, domain=None
) -> TrialBalanceReport:
    accounts = filter_gl_accounts(list_accounts(db, active_only=False), domain)
    headers = header_account_ids(db)
    accounts = filter_leaf_accounts(accounts, headers)
    rows: list[TrialBalanceRow] = []
    total_d = total_c = _ZERO
    for acc in accounts:
        pd, pc = account_period_movement(db, acc.id, from_date, to_date, domain=domain)
        if pd == _ZERO and pc == _ZERO:
            bal = account_balance_as_of(db, acc.id, to_date, domain=domain)
            if bal == _ZERO:
                continue
        else:
            bal = account_balance_as_of(db, acc.id, to_date, domain=domain)
        td, tc = _tb_columns(acc.account_type, bal)
        rows.append(
            TrialBalanceRow(
                account=acc,
                period_debit=pd,
                period_credit=pc,
                balance=bal,
                tb_debit=td,
                tb_credit=tc,
            )
        )
        total_d += td
        total_c += tc
    rows.sort(key=lambda r: (r.account.sort_order, r.account.code))
    return TrialBalanceReport(
        as_of=to_date,
        from_date=from_date,
        rows=rows,
        total_debit=_q(total_d),
        total_credit=_q(total_c),
        balanced=total_d == total_c,
    )


@dataclass
class PlLine:
    account: GlAccount
    amount: Decimal


@dataclass
class ProfitLossReport:
    from_date: date
    to_date: date
    revenue_lines: list[PlLine]
    expense_lines: list[PlLine]
    total_revenue: Decimal
    total_expense: Decimal
    net_income: Decimal


def _pl_amount(acc_type: GlAccountType, period_debit: Decimal, period_credit: Decimal) -> Decimal:
    if acc_type == GlAccountType.REVENUE:
        return _q(period_credit - period_debit)
    return _q(period_debit - period_credit)


def build_profit_loss(
    db: Session, *, from_date: date, to_date: date, domain=None
) -> ProfitLossReport:
    accounts = filter_gl_accounts(list_accounts(db), domain)
    headers = header_account_ids(db)
    accounts = filter_leaf_accounts(accounts, headers)
    revenue_lines: list[PlLine] = []
    expense_lines: list[PlLine] = []
    total_rev = total_exp = _ZERO
    for acc in accounts:
        if acc.account_type not in (GlAccountType.REVENUE, GlAccountType.EXPENSE):
            continue
        pd, pc = account_period_movement(db, acc.id, from_date, to_date, domain=domain)
        amt = _pl_amount(acc.account_type, pd, pc)
        if amt == _ZERO:
            continue
        line = PlLine(account=acc, amount=amt)
        if acc.account_type == GlAccountType.REVENUE:
            revenue_lines.append(line)
            total_rev += amt
        else:
            expense_lines.append(line)
            total_exp += amt
    revenue_lines.sort(key=lambda x: x.account.code)
    expense_lines.sort(key=lambda x: x.account.code)
    return ProfitLossReport(
        from_date=from_date,
        to_date=to_date,
        revenue_lines=revenue_lines,
        expense_lines=expense_lines,
        total_revenue=_q(total_rev),
        total_expense=_q(total_exp),
        net_income=_q(total_rev - total_exp),
    )


@dataclass
class BalanceSheetLine:
    account: GlAccount
    balance: Decimal
    display_amount: Decimal


@dataclass
class BalanceSheetSection:
    account_type: GlAccountType
    label: str
    lines: list[BalanceSheetLine]
    subtotal: Decimal


@dataclass
class BalanceSheetReport:
    as_of: date
    sections: list[BalanceSheetSection]
    total_assets: Decimal
    total_liabilities: Decimal
    total_equity_accounts: Decimal
    unclosed_net_income: Decimal
    total_liabilities_equity: Decimal
    balanced: bool


def _bs_display(acc_type: GlAccountType, balance: Decimal) -> Decimal:
    """مبلغ موجب للعرض في الميزانية."""
    if balance == _ZERO:
        return _ZERO
    if acc_type == GlAccountType.ASSET:
        return balance if balance > 0 else -balance
    if acc_type in (GlAccountType.LIABILITY, GlAccountType.EQUITY):
        return -balance if balance < 0 else balance
    return balance


def unclosed_net_income_as_of(db: Session, as_of: date, domain=None) -> Decimal:
    headers = header_account_ids(db)
    total = _ZERO
    for acc in filter_leaf_accounts(filter_gl_accounts(list_accounts(db), domain), headers):
        if acc.account_type not in (GlAccountType.REVENUE, GlAccountType.EXPENSE):
            continue
        bal = account_balance_as_of(db, acc.id, as_of, domain=domain)
        if acc.account_type == GlAccountType.REVENUE:
            total += -bal
        else:
            total -= bal
    return _q(total)


def build_balance_sheet(db: Session, *, as_of: date, domain=None) -> BalanceSheetReport:
    headers = header_account_ids(db)
    sections: list[BalanceSheetSection] = []
    total_assets = total_liab = total_equity = _ZERO
    visible = filter_gl_accounts(list_accounts(db), domain)
    for acc_type in (GlAccountType.ASSET, GlAccountType.LIABILITY, GlAccountType.EQUITY):
        lines: list[BalanceSheetLine] = []
        sub = _ZERO
        for acc in filter_leaf_accounts(visible, headers):
            if acc.account_type != acc_type:
                continue
            bal = account_balance_as_of(db, acc.id, as_of, domain=domain)
            disp = _bs_display(acc_type, bal)
            if disp == _ZERO:
                continue
            lines.append(BalanceSheetLine(account=acc, balance=bal, display_amount=disp))
            sub += disp
        lines.sort(key=lambda x: x.account.code)
        sections.append(
            BalanceSheetSection(
                account_type=acc_type,
                label=account_type_label(acc_type),
                lines=lines,
                subtotal=_q(sub),
            )
        )
        if acc_type == GlAccountType.ASSET:
            total_assets = _q(sub)
        elif acc_type == GlAccountType.LIABILITY:
            total_liab = _q(sub)
        else:
            total_equity = _q(sub)

    unclosed = unclosed_net_income_as_of(db, as_of, domain=domain)
    total_le = _q(total_liab + total_equity + unclosed)
    return BalanceSheetReport(
        as_of=as_of,
        sections=sections,
        total_assets=total_assets,
        total_liabilities=total_liab,
        total_equity_accounts=total_equity,
        unclosed_net_income=unclosed,
        total_liabilities_equity=total_le,
        balanced=total_assets == total_le,
    )
