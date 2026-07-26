from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from modules.gl.models import (
    AccountOpeningBalance,
    FiscalYear,
    FiscalYearStatus,
    GlAccount,
    GlAccountType,
    GlJournalEntry,
    GlJournalEntryStatus,
    GlJournalLine,
    GlPaymentMethodMap,
)
from modules.gl.hierarchy import (
    assert_postable_account,
    build_children_map,
    direct_children,
    display_balances_map,
    is_header_account,
)
from modules.gl.seed import account_type_label
from modules.platform.business_domain import BusinessDomain
from modules.settings.service import get_bool, get_setting, set_setting

GL_SETTING_ENABLED = "gl_enabled"
GL_SETTING_POST_MODE = "gl_post_mode"
GL_SETTING_CUTOVER = "gl_cutover_date"

VALID_POST_MODES = frozenset({"off", "shadow", "live"})
ACCOUNT_TYPE_ORDER: tuple[GlAccountType, ...] = (
    GlAccountType.ASSET,
    GlAccountType.LIABILITY,
    GlAccountType.EQUITY,
    GlAccountType.REVENUE,
    GlAccountType.EXPENSE,
)

_CODE_RE = re.compile(r"^[0-9A-Za-z.\-]{2,16}$")


class GLError(Exception):
    pass


def is_gl_enabled(db: Session) -> bool:
    return get_bool(db, GL_SETTING_ENABLED, default=False)


def get_gl_post_mode(db: Session) -> str:
    raw = (get_setting(db, GL_SETTING_POST_MODE, "live") or "live").strip().lower()
    if raw not in VALID_POST_MODES:
        return "live"
    return raw


def get_gl_cutover_date(db: Session) -> date | None:
    raw = (get_setting(db, GL_SETTING_CUTOVER, "") or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def entry_date_on_or_after_cutover(
    db: Session, entry_date: date, *, ignore_cutover: bool = False
) -> bool:
    if ignore_cutover:
        return True
    cutover = get_gl_cutover_date(db)
    if cutover is None:
        return True
    return entry_date >= cutover


def get_current_fiscal_year(db: Session) -> FiscalYear | None:
    return db.scalar(
        select(FiscalYear).where(FiscalYear.is_current.is_(True)).limit(1)
    )


def _current_fiscal_year_id(db: Session) -> int | None:
    fy = get_current_fiscal_year(db)
    return int(fy.id) if fy is not None else None


def get_opening_balance(db: Session, account_id: int, domain=None) -> Decimal:
    from modules.gl.domain import gl_entry_db_values

    fy_id = _current_fiscal_year_id(db)
    if fy_id is None:
        return Decimal("0")
    stmt = (
        select(func.coalesce(func.sum(AccountOpeningBalance.debit - AccountOpeningBalance.credit), 0))
        .join(GlAccount, AccountOpeningBalance.account_id == GlAccount.id)
        .where(
            AccountOpeningBalance.account_id == account_id,
            AccountOpeningBalance.fiscal_year_id == fy_id,
        )
    )
    vals = gl_entry_db_values(domain)
    if vals is not None:
        stmt = stmt.where(GlAccount.business_domain.in_(vals))
    net = db.scalar(stmt)
    return Decimal(str(net or 0)).quantize(Decimal("0.001"))


def get_opening_balances_map(db: Session, domain=None) -> dict[int, Decimal]:
    from modules.gl.domain import gl_entry_db_values

    fy_id = _current_fiscal_year_id(db)
    if fy_id is None:
        return {}
    stmt = (
        select(
            AccountOpeningBalance.account_id,
            func.coalesce(func.sum(AccountOpeningBalance.debit - AccountOpeningBalance.credit), 0),
        )
        .join(GlAccount, AccountOpeningBalance.account_id == GlAccount.id)
        .where(AccountOpeningBalance.fiscal_year_id == fy_id)
        .group_by(AccountOpeningBalance.account_id)
    )
    vals = gl_entry_db_values(domain)
    if vals is not None:
        stmt = stmt.where(GlAccount.business_domain.in_(vals))
    out: dict[int, Decimal] = {}
    for aid, net in db.execute(stmt).all():
        if aid is None:
            continue
        out[int(aid)] = Decimal(str(net or 0)).quantize(Decimal("0.001"))
    return out


def get_opening_balance_for_fiscal_year(
    db: Session, fiscal_year_id: int, account_id: int, domain=None
) -> Decimal:
    from modules.gl.domain import gl_entry_db_values

    stmt = (
        select(func.coalesce(func.sum(AccountOpeningBalance.debit - AccountOpeningBalance.credit), 0))
        .join(GlAccount, AccountOpeningBalance.account_id == GlAccount.id)
        .where(
            AccountOpeningBalance.account_id == account_id,
            AccountOpeningBalance.fiscal_year_id == fiscal_year_id,
        )
    )
    vals = gl_entry_db_values(domain)
    if vals is not None:
        stmt = stmt.where(GlAccount.business_domain.in_(vals))
    net = db.scalar(stmt)
    return Decimal(str(net or 0)).quantize(Decimal("0.001"))


def set_opening_balance(
    db: Session,
    fiscal_year_id: int,
    account_id: int,
    debit: Decimal,
    credit: Decimal,
    note: str | None = None,
) -> AccountOpeningBalance:
    row = db.scalar(
        select(AccountOpeningBalance).where(
            AccountOpeningBalance.fiscal_year_id == fiscal_year_id,
            AccountOpeningBalance.account_id == account_id,
        )
    )
    if row is None:
        row = AccountOpeningBalance(fiscal_year_id=fiscal_year_id, account_id=account_id)
        db.add(row)
    row.debit = Decimal(str(debit or 0)).quantize(Decimal("0.001"))
    row.credit = Decimal(str(credit or 0)).quantize(Decimal("0.001"))
    row.note = (note or "").strip() or None
    db.flush()
    return row


def list_fiscal_years(db: Session) -> list[FiscalYear]:
    return list(db.scalars(select(FiscalYear).order_by(FiscalYear.start_date.desc())).all())


def set_current_fiscal_year(db: Session, fiscal_year_id: int) -> FiscalYear:
    for fy in list_fiscal_years(db):
        fy.is_current = False
    fy = db.get(FiscalYear, fiscal_year_id)
    if fy is None:
        raise GLError("السنة المالية غير موجودة.")
    fy.is_current = True
    db.flush()
    _carry_forward_opening_balances(db, fy)
    return fy


def _carry_forward_opening_balances(db: Session, to_fy: FiscalYear) -> None:
    """ترحيل أرصدة الختام من السنة السابقة المقفلة كأرصدة افتتاح للسنة الجديدة."""
    from modules.gl.reports import account_period_movement

    existing = db.scalar(
        select(func.count(AccountOpeningBalance.id)).where(
            AccountOpeningBalance.fiscal_year_id == to_fy.id
        )
    )
    if existing and int(existing or 0) > 0:
        return
    from_fy = db.scalar(
        select(FiscalYear)
        .where(
            FiscalYear.id != to_fy.id,
            FiscalYear.end_date < to_fy.start_date,
        )
        .order_by(FiscalYear.end_date.desc())
        .limit(1)
    )
    if from_fy is None:
        return
    _close_fiscal_year_common(db, from_fy)
    for acc in db.scalars(select(GlAccount).where(GlAccount.is_active.is_(True))).all():
        if acc.account_type in (GlAccountType.REVENUE, GlAccountType.EXPENSE):
            continue
        opening = get_opening_balance_for_fiscal_year(db, int(from_fy.id), int(acc.id))
        debit, credit = account_period_movement(
            db, int(acc.id), from_fy.start_date, from_fy.end_date
        )
        closing = (opening + debit - credit).quantize(Decimal("0.001"))
        if closing == Decimal("0"):
            continue
        if closing > 0:
            set_opening_balance(db, int(to_fy.id), int(acc.id), closing, Decimal("0"))
        else:
            set_opening_balance(db, int(to_fy.id), int(acc.id), Decimal("0"), abs(closing))
    db.flush()


def _close_fiscal_year_common(db: Session, fy: FiscalYear) -> None:
    """توليد قيود إقفال السنة المالية (إقفال الإيرادات/المصروفات إلى الأرباح المحتجزة)."""
    from modules.gl.reports import account_period_movement

    if fy.status == FiscalYearStatus.CLOSED:
        return
    existing = db.scalar(
        select(GlJournalEntry.id).where(
            GlJournalEntry.source_type == "fiscal_year_close",
            GlJournalEntry.source_id == int(fy.id),
        )
    )
    if existing is not None:
        return
    income_summary = db.scalar(
        select(GlAccount.id).where(GlAccount.code == "3500", GlAccount.is_active.is_(True))
    )
    retained_earnings = db.scalar(
        select(GlAccount.id).where(GlAccount.code == "3600", GlAccount.is_active.is_(True))
    )
    if income_summary is None or retained_earnings is None:
        raise GLError("حسابات إقفال السنة غير موجودة (3500/3600).")
    net_income = Decimal("0")
    lines: list[tuple[int, Decimal, Decimal, str]] = []
    for acc in db.scalars(
        select(GlAccount).where(
            GlAccount.account_type.in_([GlAccountType.REVENUE, GlAccountType.EXPENSE]),
            GlAccount.is_active.is_(True),
        )
    ).all():
        opening = get_opening_balance_for_fiscal_year(db, int(fy.id), int(acc.id))
        debit, credit = account_period_movement(
            db, int(acc.id), fy.start_date, fy.end_date
        )
        closing = (opening + debit - credit).quantize(Decimal("0.001"))
        if closing == Decimal("0"):
            continue
        if acc.account_type == GlAccountType.REVENUE:
            # إيرادات (رصيد دائن) ندينها لإغلاقها ( debit = abs(closing) )
            lines.append((int(acc.id), abs(closing), Decimal("0"), f"إقفال {acc.name_ar}"))
        else:
            # مصروفات (رصيد مدين) ندائنها لإغلاقها
            lines.append((int(acc.id), Decimal("0"), abs(closing), f"إقفال {acc.name_ar}"))
        net_income -= closing

    # قيد إقفال الإيرادات/المصروفات إلى ملخص الدخل
    if net_income != 0:
        if net_income > 0:
            lines.append((int(income_summary), Decimal("0"), net_income, "صافي الدخل"))
        else:
            lines.append((int(income_summary), abs(net_income), Decimal("0"), "صافي الخسارة"))
    if lines:
        _post_closing_entry(db, fy, "إقفال السنة المالية", lines, suffix="close")
    # قيد نقل ملخص الدخل إلى الأرباح المحتجزة
    retained_lines: list[tuple[int, Decimal, Decimal, str]] = []
    if net_income > 0:
        retained_lines.append((int(income_summary), net_income, Decimal("0"), "نقل صافي الدخل"))
        retained_lines.append((int(retained_earnings), Decimal("0"), net_income, "الأرباح المحتجزة"))
    elif net_income < 0:
        retained_lines.append((int(retained_earnings), abs(net_income), Decimal("0"), "خصم الخسارة"))
        retained_lines.append((int(income_summary), Decimal("0"), abs(net_income), "تصفير ملخص الدخل"))
    if retained_lines:
        _post_closing_entry(db, fy, "ترحيل الأرباح المحتجزة", retained_lines, suffix="retain")
    fy.status = FiscalYearStatus.CLOSED
    fy.is_current = False
    db.flush()


def _post_closing_entry(
    db: Session,
    fy: FiscalYear,
    description: str,
    lines: list[tuple[int, Decimal, Decimal, str]],
    suffix: str,
) -> None:
    """يسجّل قيد إقفال السنة المالية بشكل مباشر (POSTED)."""
    from datetime import datetime, timezone

    if not lines:
        return
    total_debit = sum((d for _, d, _, _ in lines), Decimal("0"))
    total_credit = sum((c for _, _, c, _ in lines), Decimal("0"))
    if total_debit != total_credit:
        raise GLError(f"قيد الإقفال غير متوازن: {total_debit} ≠ {total_credit}")
    entry = GlJournalEntry(
        entry_date=fy.end_date,
        description_ar=description[:255],
        status=GlJournalEntryStatus.POSTED,
        source_type="fiscal_year_close",
        source_id=int(fy.id),
        idempotency_key=f"fy-close-{fy.id}-{suffix}",
        post_mode="live",
        business_domain=BusinessDomain.SHARED.value,
    )
    db.add(entry)
    db.flush()
    for i, (account_id, debit, credit, memo) in enumerate(lines, start=1):
        db.add(
            GlJournalLine(
                entry_id=entry.id,
                account_id=account_id,
                debit=debit,
                credit=credit,
                memo=(memo or "")[:255] or None,
                line_no=i,
            )
        )
    db.flush()


def close_fiscal_year(db: Session, fiscal_year_id: int) -> FiscalYear:
    """إقفال السنة المالية (إنشاء قيود الإقفال وتعيين الحالة إلى CLOSED)."""
    fy = db.get(FiscalYear, fiscal_year_id)
    if fy is None:
        raise GLError("السنة المالية غير موجودة.")
    if fy.status == FiscalYearStatus.CLOSED:
        raise GLError("السنة المالية مقفلة بالفعل.")
    _close_fiscal_year_common(db, fy)
    db.flush()
    return fy


def is_posting_date_allowed(db: Session, entry_date: date) -> bool:
    """يتحقق من أن تاريخ القيد يقع داخل سنة مالية مفتوحة."""
    fy = db.scalar(
        select(FiscalYear).where(
            FiscalYear.start_date <= entry_date,
            FiscalYear.end_date >= entry_date,
        )
    )
    if fy is None:
        return False
    return fy.status == FiscalYearStatus.OPEN


def create_fiscal_year(
    db: Session,
    name: str,
    start_date: date,
    end_date: date,
    *,
    set_as_current: bool = True,
) -> FiscalYear:
    if end_date <= start_date:
        raise GLError("تاريخ نهاية السنة يجب أن يكون بعد تاريخ البداية.")
    existing = db.scalar(select(FiscalYear).where(FiscalYear.name == name.strip()).limit(1))
    if existing is not None:
        raise GLError("يوجد سنة مالية بنفس الاسم.")
    fy = FiscalYear(
        name=name.strip(),
        start_date=start_date,
        end_date=end_date,
    )
    db.add(fy)
    db.flush()
    if set_as_current:
        set_current_fiscal_year(db, int(fy.id))
    return fy


def gl_status_summary(db: Session) -> dict[str, object]:
    from modules.payments.service import list_payment_methods

    enabled = is_gl_enabled(db)
    mode = get_gl_post_mode(db)
    account_count = db.scalar(select(func.count(GlAccount.id))) or 0
    entry_count = db.scalar(select(func.count(GlJournalEntry.id))) or 0
    map_count = db.scalar(select(func.count(GlPaymentMethodMap.id))) or 0
    mapped_pm_ids = {
        int(r[0])
        for r in db.execute(select(GlPaymentMethodMap.payment_method_id)).all()
        if r[0] is not None
    }
    dashboard_wallets: list = []
    unmapped_wallet_count = 0
    try:
        dashboard_wallets = [
            pm
            for pm in list_payment_methods(db, only_active=True)
            if pm.show_on_dashboard
        ]
        unmapped_wallet_count = sum(
            1 for pm in dashboard_wallets if int(pm.id) not in mapped_pm_ids
        )
    except Exception:
        unmapped_wallet_count = 0
    posting_active = enabled and mode != "off"
    is_live = enabled and mode == "live"
    is_shadow = enabled and mode == "shadow"
    setup_complete = (
        enabled
        and posting_active
        and unmapped_wallet_count == 0
        and account_count > 0
    )
    production_ready = setup_complete and is_live and entry_count > 0

    if not enabled:
        status_key = "disabled"
        status_label = "معطّل"
        status_color = "#64748b"
    elif production_ready:
        status_key = "production"
        status_label = "مفعّل تشغيلياً — المصدر المحاسبي الرسمي"
        status_color = "#15803d"
    elif is_live and setup_complete:
        status_key = "live_setup"
        status_label = "مباشر — نفّذ ترحيل الحركات السابقة"
        status_color = "#0369a1"
    elif is_live:
        status_key = "live_incomplete"
        status_label = "مباشر — أكمل الربط والترحيل"
        status_color = "#b45309"
    elif is_shadow:
        status_key = "shadow"
        status_label = "وضع مقارنة — انتقل للمباشر للنسخة النهائية"
        status_color = "#b45309"
    elif mode == "off":
        status_key = "paused"
        status_label = "الترحيل متوقف"
        status_color = "#b45309"
    else:
        status_key = "unknown"
        status_label = "غير معروف"
        status_color = "#64748b"

    return {
        "enabled": enabled,
        "post_mode": mode,
        "account_count": account_count,
        "entry_count": entry_count,
        "wallet_map_count": map_count,
        "unmapped_wallet_count": unmapped_wallet_count,
        "posting_active": posting_active,
        "is_live": is_live,
        "is_shadow": is_shadow,
        "setup_complete": setup_complete,
        "production_ready": production_ready,
        "status_key": status_key,
        "status_label": status_label,
        "status_color": status_color,
        "cutover_date": get_gl_cutover_date(db),
    }


def activate_production_gl(db: Session, *, cutover_date: date | None = None) -> None:
    """تفعيل GL في وضع مباشر — النسخة التشغيلية النهائية."""
    set_setting(db, GL_SETTING_ENABLED, "1")
    set_setting(db, GL_SETTING_POST_MODE, "live")
    if cutover_date is not None:
        set_setting(db, GL_SETTING_CUTOVER, cutover_date.isoformat())
    db.flush()


def list_accounts(db: Session, *, active_only: bool = True, domain=None) -> list[GlAccount]:
    from modules.gl.domain import filter_gl_accounts

    stmt = select(GlAccount).order_by(GlAccount.sort_order, GlAccount.code)
    if active_only:
        stmt = stmt.where(GlAccount.is_active.is_(True))
    return filter_gl_accounts(list(db.scalars(stmt).all()), domain)


def accounts_by_type(
    db: Session, domain=None
) -> list[tuple[GlAccountType, str, list[GlAccount]]]:
    accounts = list_accounts(db, active_only=False, domain=domain)
    grouped: dict[GlAccountType, list[GlAccount]] = {t: [] for t in ACCOUNT_TYPE_ORDER}
    for acc in accounts:
        grouped.setdefault(acc.account_type, []).append(acc)
    return [
        (t, account_type_label(t), grouped.get(t, []))
        for t in ACCOUNT_TYPE_ORDER
        if grouped.get(t)
    ]


def get_account(db: Session, account_id: int) -> GlAccount | None:
    return db.get(GlAccount, account_id)


def account_balance(db: Session, account_id: int, domain=None) -> Decimal:
    from modules.gl.domain import gl_entry_db_values
    from modules.gl.models import GlJournalEntry

    stmt = (
        select(func.coalesce(func.sum(GlJournalLine.debit - GlJournalLine.credit), 0))
        .select_from(GlJournalLine)
        .where(GlJournalLine.account_id == account_id)
    )
    vals = gl_entry_db_values(domain)
    if vals is not None:
        stmt = stmt.join(
            GlJournalEntry, GlJournalEntry.id == GlJournalLine.entry_id
        ).where(GlJournalEntry.business_domain.in_(vals))
    net = db.scalar(stmt)
    balance = Decimal(str(net or 0)).quantize(Decimal("0.001"))
    return (balance + get_opening_balance(db, account_id, domain=domain)).quantize(Decimal("0.001"))


def account_balances_map(db: Session, domain=None) -> dict[int, Decimal]:
    from modules.gl.domain import gl_entry_db_values
    from modules.gl.models import GlJournalEntry

    stmt = (
        select(
            GlJournalLine.account_id,
            func.coalesce(func.sum(GlJournalLine.debit - GlJournalLine.credit), 0),
        )
        .select_from(GlJournalLine)
        .group_by(GlJournalLine.account_id)
    )
    vals = gl_entry_db_values(domain)
    if vals is not None:
        stmt = stmt.join(
            GlJournalEntry, GlJournalEntry.id == GlJournalLine.entry_id
        ).where(GlJournalEntry.business_domain.in_(vals))
    out: dict[int, Decimal] = {}
    for aid, net in db.execute(stmt).all():
        if aid is None:
            continue
        out[int(aid)] = Decimal(str(net or 0)).quantize(Decimal("0.001"))
    for aid, ob in get_opening_balances_map(db, domain=domain).items():
        out[aid] = (out.get(aid, Decimal("0")) + ob).quantize(Decimal("0.001"))
    return out


@dataclass
class AccountLedgerRow:
    line_id: int
    entry_id: int
    entry_date: date
    description_ar: str
    source_type: str | None
    source_id: int | None
    memo: str | None
    debit: Decimal
    credit: Decimal
    running_balance: Decimal
    post_mode: str
    detail_url: str | None = None
    is_aggregate: bool = False
    item_count: int = 1


@dataclass
class AccountLedgerPage:
    account: GlAccount
    balance: Decimal
    period_balance: Decimal
    rows: list[AccountLedgerRow]
    total_lines: int
    linked_wallets: list[tuple[str, Decimal]]
    is_header: bool = False
    child_summaries: list[tuple[GlAccount, Decimal]] | None = None
    #: عرض مجمّع حسب جلسة/يوم (خزائن كاش ومصرف)
    session_view: bool = False


_DAY_SOURCE_LABELS: dict[str, str] = {
    "sale_payment": "تحصيلات مبيعات",
    "refund_payment": "إرجاعات نقدية",
    "hotel_booking_payment": "تحصيلات فندق",
    "hotel_booking_payment_refund": "إرجاعات فندق",
    "purchase_payment": "مدفوعات مشتريات/مصروف",
    "payment_transfer": "تحويلات وتسويات",
    "salary_advance": "سلف رواتب",
    "payroll_payment": "صرف رواتب",
    "other": "حركات أخرى",
}


def wallet_maps_for_account(db: Session, account_id: int) -> list[GlPaymentMethodMap]:
    return list(
        db.scalars(
            select(GlPaymentMethodMap)
            .where(GlPaymentMethodMap.gl_account_id == account_id)
            .options(selectinload(GlPaymentMethodMap.payment_method))
        ).all()
    )


def _is_vault_session_account(db: Session, acc: GlAccount) -> bool:
    """خزائن الكاش/المصرف: أكواد 111*/112* أو حساب مربوط بمحفظة تشغيلية."""
    code = (acc.code or "").strip()
    if code.startswith(("111", "112")):
        return True
    return bool(wallet_maps_for_account(db, int(acc.id)))


def _vault_shift_maps(
    db: Session, pairs: list[tuple[object, object]]
) -> dict[str, dict[int, int]]:
    """ربط مصدر القيد → معرف الجلسة (كاشير أو فندق)."""
    sale_pay_ids: set[int] = set()
    hotel_pay_ids: set[int] = set()
    hotel_refund_ids: set[int] = set()
    purchase_pay_ids: set[int] = set()
    for _ln, ent in pairs:
        st = (ent.source_type or "").strip()
        sid = ent.source_id
        if sid is None:
            continue
        sid_i = int(sid)
        if st == "sale_payment":
            sale_pay_ids.add(sid_i)
        elif st == "hotel_booking_payment":
            hotel_pay_ids.add(sid_i)
        elif st == "hotel_booking_payment_refund":
            hotel_refund_ids.add(sid_i)
        elif st == "purchase_payment":
            purchase_pay_ids.add(sid_i)

    out: dict[str, dict[int, int]] = {
        "sale_payment": {},
        "hotel_booking_payment": {},
        "hotel_booking_payment_refund": {},
        "purchase_payment_pos": {},
        "purchase_payment_hotel": {},
    }
    if sale_pay_ids:
        from sqlalchemy import column, table
        from modules.payments.models import SalePayment

        sales_t = table("sales", column("id"), column("pos_shift_id"))
        for sp_id, shift_id in db.execute(
            select(SalePayment.id, sales_t.c.pos_shift_id)
            .select_from(
                SalePayment.__table__.join(
                    sales_t, sales_t.c.id == SalePayment.sale_id
                )
            )
            .where(SalePayment.id.in_(sale_pay_ids))
        ).all():
            if shift_id is not None:
                out["sale_payment"][int(sp_id)] = int(shift_id)
    if hotel_pay_ids:
        from modules.hotel.booking_models import HotelBookingPayment

        for pid, shift_id in db.execute(
            select(HotelBookingPayment.id, HotelBookingPayment.hotel_shift_id).where(
                HotelBookingPayment.id.in_(hotel_pay_ids)
            )
        ).all():
            if shift_id is not None:
                out["hotel_booking_payment"][int(pid)] = int(shift_id)
    if hotel_refund_ids:
        from modules.hotel.booking_models import HotelBookingPaymentRefund

        for rid, shift_id in db.execute(
            select(
                HotelBookingPaymentRefund.id,
                HotelBookingPaymentRefund.hotel_shift_id,
            ).where(HotelBookingPaymentRefund.id.in_(hotel_refund_ids))
        ).all():
            if shift_id is not None:
                out["hotel_booking_payment_refund"][int(rid)] = int(shift_id)
    if purchase_pay_ids:
        from sqlalchemy import column, table
        from modules.payments.models import PurchasePayment

        purchases_t = table(
            "purchases",
            column("id"),
            column("pos_shift_id"),
            column("hotel_shift_id"),
        )
        for pp_id, pos_sid, hotel_sid in db.execute(
            select(
                PurchasePayment.id,
                purchases_t.c.pos_shift_id,
                purchases_t.c.hotel_shift_id,
            )
            .select_from(
                PurchasePayment.__table__.join(
                    purchases_t, purchases_t.c.id == PurchasePayment.purchase_id
                )
            )
            .where(PurchasePayment.id.in_(purchase_pay_ids))
        ).all():
            if pos_sid is not None:
                out["purchase_payment_pos"][int(pp_id)] = int(pos_sid)
            elif hotel_sid is not None:
                out["purchase_payment_hotel"][int(pp_id)] = int(hotel_sid)
    return out


def _vault_group_key(
    ent: object, shift_maps: dict[str, dict[int, int]]
) -> tuple:
    st = (getattr(ent, "source_type", None) or "").strip()
    sid = getattr(ent, "source_id", None)
    entry_date = getattr(ent, "entry_date")
    if sid is not None:
        sid_i = int(sid)
        if st == "sale_payment":
            ps = shift_maps["sale_payment"].get(sid_i)
            if ps:
                return ("pos_shift", ps)
        elif st == "hotel_booking_payment":
            hs = shift_maps["hotel_booking_payment"].get(sid_i)
            if hs:
                return ("hotel_shift", hs)
        elif st == "hotel_booking_payment_refund":
            hs = shift_maps["hotel_booking_payment_refund"].get(sid_i)
            if hs:
                return ("hotel_shift", hs)
        elif st == "purchase_payment":
            ps = shift_maps["purchase_payment_pos"].get(sid_i)
            if ps:
                return ("pos_shift", ps)
            hs = shift_maps["purchase_payment_hotel"].get(sid_i)
            if hs:
                return ("hotel_shift", hs)
    return ("day", entry_date, st or "other")


def _vault_group_labels(
    db: Session, keys: list[tuple]
) -> dict[tuple, tuple[str, str | None]]:
    """وصف الصف المجمّع + رابط التفاصيل."""
    pos_ids = {k[1] for k in keys if k[0] == "pos_shift"}
    hotel_ids = {k[1] for k in keys if k[0] == "hotel_shift"}
    pos_labels: dict[int, str] = {}
    hotel_labels: dict[int, str] = {}
    if pos_ids:
        from modules.authz.models import User
        from modules.pos_shifts.models import PosShift

        for sid, uid in db.execute(
            select(PosShift.id, PosShift.user_id).where(PosShift.id.in_(pos_ids))
        ).all():
            uname = ""
            if uid is not None:
                u = db.get(User, int(uid))
                uname = (u.username if u else "") or ""
            label = f"جلسة كاشير #{int(sid)}"
            if uname:
                label += f" — {uname}"
            pos_labels[int(sid)] = label
    if hotel_ids:
        from modules.hotel.shift_models import HotelShift

        for hs in db.scalars(
            select(HotelShift).where(HotelShift.id.in_(hotel_ids))
        ).all():
            name = (hs.shift_name_ar or "").strip() or f"#{hs.shift_number}"
            hotel_labels[int(hs.id)] = f"جلسة فندق #{int(hs.id)} — {name}"

    out: dict[tuple, tuple[str, str | None]] = {}
    for key in keys:
        kind = key[0]
        if kind == "pos_shift":
            sid = int(key[1])
            out[key] = (
                pos_labels.get(sid, f"جلسة كاشير #{sid}"),
                f"/reports/shifts/{sid}",
            )
        elif kind == "hotel_shift":
            sid = int(key[1])
            out[key] = (
                hotel_labels.get(sid, f"جلسة فندق #{sid}"),
                f"/admin/hotel/shift/{sid}/report",
            )
        else:
            d = key[1]
            st = str(key[2])
            label = _DAY_SOURCE_LABELS.get(st, st or "حركات")
            out[key] = (f"{label} — {d}", None)
    return out


def _aggregate_vault_ledger_rows(
    db: Session,
    chrono_pairs: list[tuple],
    *,
    opening: Decimal,
    account_id: int,
) -> list[AccountLedgerRow]:
    if not chrono_pairs:
        return []
    shift_maps = _vault_shift_maps(db, chrono_pairs)
    groups: dict[tuple, list[tuple]] = {}
    for pair in chrono_pairs:
        _ln, ent = pair
        key = _vault_group_key(ent, shift_maps)
        groups.setdefault(key, []).append(pair)

    def _group_sort_key(k: tuple) -> tuple:
        items = groups[k]
        last_ent = items[-1][1]
        return (last_ent.entry_date, int(last_ent.id))

    order = sorted(groups.keys(), key=_group_sort_key)

    labels = _vault_group_labels(db, order)
    running = opening
    rows: list[AccountLedgerRow] = []
    for key in order:
        items = groups[key]
        debit = Decimal("0")
        credit = Decimal("0")
        last_ln, last_ent = items[-1]
        first_ent = items[0][1]
        for ln, _ent in items:
            debit += Decimal(str(ln.debit or 0))
            credit += Decimal(str(ln.credit or 0))
        debit = debit.quantize(Decimal("0.001"))
        credit = credit.quantize(Decimal("0.001"))
        running = (running + debit - credit).quantize(Decimal("0.001"))
        desc, detail = labels.get(key, ("حركة مجمّعة", None))
        n = len(items)
        if n > 1:
            desc = f"{desc} · {n} حركة"
        kind = key[0]
        if detail is None and kind == "day":
            d = key[1]
            detail = f"/admin/gl/accounts/{account_id}?from={d}&to={d}&flat=1"
        source_type = (
            "pos_shift"
            if kind == "pos_shift"
            else ("hotel_shift" if kind == "hotel_shift" else str(key[2]))
        )
        source_id = int(key[1]) if kind in ("pos_shift", "hotel_shift") else None
        rows.append(
            AccountLedgerRow(
                line_id=int(last_ln.id),
                entry_id=int(last_ent.id),
                entry_date=last_ent.entry_date or first_ent.entry_date,
                description_ar=desc,
                source_type=source_type,
                source_id=source_id,
                memo=None,
                debit=debit,
                credit=credit,
                running_balance=running,
                post_mode=last_ent.post_mode,
                detail_url=detail,
                is_aggregate=True,
                item_count=n,
            )
        )
    return rows


def list_account_ledger(
    db: Session,
    account_id: int,
    *,
    limit: int = 80,
    offset: int = 0,
    from_date: date | None = None,
    to_date: date | None = None,
    domain=None,
    flat: bool = False,
) -> AccountLedgerPage:
    from modules.gl.domain import gl_entry_db_values
    from modules.gl.models import GlJournalEntry

    acc = db.get(GlAccount, account_id)
    if acc is None:
        raise GLError("الحساب غير موجود.")

    children_map = build_children_map(db)
    raw_balances = account_balances_map(db, domain=domain)
    all_display = display_balances_map(db, raw_balances)
    display_bal = all_display.get(account_id, Decimal("0"))
    is_hdr = is_header_account(db, account_id, children_map)

    if is_hdr:
        child_summaries: list[tuple[GlAccount, Decimal]] = []
        for child in direct_children(db, account_id, children_map):
            cb = all_display.get(int(child.id), Decimal("0"))
            child_summaries.append((child, cb))
        return AccountLedgerPage(
            account=acc,
            balance=display_bal,
            period_balance=Decimal("0"),
            rows=[],
            total_lines=0,
            linked_wallets=[],
            is_header=True,
            child_summaries=child_summaries,
        )

    domain_vals = gl_entry_db_values(domain)
    base = (
        select(GlJournalLine, GlJournalEntry)
        .join(GlJournalEntry, GlJournalEntry.id == GlJournalLine.entry_id)
        .where(GlJournalLine.account_id == account_id)
    )
    if domain_vals is not None:
        base = base.where(GlJournalEntry.business_domain.in_(domain_vals))
    if from_date is not None:
        base = base.where(GlJournalEntry.entry_date >= from_date)
    if to_date is not None:
        base = base.where(GlJournalEntry.entry_date <= to_date)

    chrono = base.order_by(
        GlJournalEntry.entry_date.asc(),
        GlJournalEntry.id.asc(),
        GlJournalLine.line_no.asc(),
    )
    opening = get_opening_balance(db, account_id, domain=domain)

    all_chrono = list(db.execute(chrono).all())
    period_balance = Decimal("0")
    for ln, _ent in all_chrono:
        period_balance += Decimal(str(ln.debit or 0)) - Decimal(str(ln.credit or 0))
    period_balance = period_balance.quantize(Decimal("0.001"))

    session_view = (not flat) and _is_vault_session_account(db, acc)

    if session_view:
        chrono_rows = _aggregate_vault_ledger_rows(
            db, all_chrono, opening=opening, account_id=account_id
        )
        total_lines = len(chrono_rows)
        end_idx = total_lines - offset
        start_idx = max(0, end_idx - limit)
        page_chrono = chrono_rows[start_idx:end_idx]
        rows = list(reversed(page_chrono))
    else:
        total_lines = len(all_chrono)
        end_idx = total_lines - offset
        start_idx = max(0, end_idx - limit)
        page_pairs = all_chrono[start_idx:end_idx]
        running = opening
        for ln, _ent in all_chrono[:start_idx]:
            running += Decimal(str(ln.debit or 0)) - Decimal(str(ln.credit or 0))
        running = running.quantize(Decimal("0.001"))
        chrono_page: list[AccountLedgerRow] = []
        for ln, ent in page_pairs:
            d = Decimal(str(ln.debit or 0)).quantize(Decimal("0.001"))
            c = Decimal(str(ln.credit or 0)).quantize(Decimal("0.001"))
            running = (running + d - c).quantize(Decimal("0.001"))
            chrono_page.append(
                AccountLedgerRow(
                    line_id=int(ln.id),
                    entry_id=int(ent.id),
                    entry_date=ent.entry_date,
                    description_ar=ent.description_ar,
                    source_type=ent.source_type,
                    source_id=ent.source_id,
                    memo=ln.memo,
                    debit=d,
                    credit=c,
                    running_balance=running,
                    post_mode=ent.post_mode,
                )
            )
        rows = list(reversed(chrono_page))

    from modules.payments.service import method_current_balance

    linked: list[tuple[str, Decimal]] = []
    for m in wallet_maps_for_account(db, account_id):
        pm = m.payment_method
        if pm is None:
            continue
        linked.append(
            (pm.name_ar, method_current_balance(db, int(pm.id)))
        )

    return AccountLedgerPage(
        account=acc,
        balance=display_bal,
        period_balance=period_balance,
        rows=rows,
        total_lines=total_lines,
        linked_wallets=linked,
        is_header=False,
        session_view=session_view,
    )


def _normalize_code(code: str) -> str:
    raw = (code or "").strip()
    if not _CODE_RE.match(raw):
        raise GLError("رمز الحساب: 2–16 حرفاً (أرقام، حروف، نقطة، شرطة).")
    return raw


def _normalize_name(name_ar: str) -> str:
    raw = (name_ar or "").strip()
    if len(raw) < 2:
        raise GLError("اسم الحساب قصير جداً.")
    if len(raw) > 160:
        raise GLError("اسم الحساب طويل جداً.")
    return raw


def create_account(
    db: Session,
    *,
    code: str,
    name_ar: str,
    account_type: GlAccountType,
    parent_id: int | None = None,
    notes: str | None = None,
    business_domain: str | None = None,
) -> GlAccount:
    from modules.gl.domain import account_domain_for_code

    code_n = _normalize_code(code)
    name_n = _normalize_name(name_ar)
    if db.scalar(select(GlAccount.id).where(GlAccount.code == code_n)):
        raise GLError("رمز الحساب مستخدم مسبقاً.")
    if parent_id is not None and db.get(GlAccount, parent_id) is None:
        raise GLError("الحساب الأب غير موجود.")
    max_sort = db.scalar(select(func.max(GlAccount.sort_order))) or 0
    dom = (business_domain or account_domain_for_code(code_n)).strip().lower()
    acc = GlAccount(
        code=code_n,
        name_ar=name_n,
        account_type=account_type,
        parent_id=parent_id,
        is_system=False,
        is_active=True,
        sort_order=int(max_sort) + 10,
        notes=(notes or "").strip() or None,
        business_domain=dom,
    )
    db.add(acc)
    db.flush()
    return acc


def update_account(
    db: Session,
    account_id: int,
    *,
    code: str | None = None,
    name_ar: str | None = None,
    notes: str | None = None,
    is_active: bool | None = None,
    show_on_dashboard: bool | None = None,
    business_domain: str | None = None,
) -> GlAccount:
    acc = db.get(GlAccount, account_id)
    if acc is None:
        raise GLError("الحساب غير موجود.")
    if name_ar is not None:
        acc.name_ar = _normalize_name(name_ar)
    if code is not None:
        if acc.is_system:
            raise GLError("لا يمكن تغيير رمز حساب نظامي — غيّر الاسم فقط.")
        code_n = _normalize_code(code)
        taken = db.scalar(
            select(GlAccount.id).where(
                GlAccount.code == code_n, GlAccount.id != account_id
            )
        )
        if taken:
            raise GLError("رمز الحساب مستخدم مسبقاً.")
        acc.code = code_n
    if notes is not None:
        acc.notes = notes.strip() or None
    if is_active is not None:
        if acc.is_system and not is_active:
            raise GLError("لا يمكن إيقاف حساب نظامي.")
        acc.is_active = is_active
    if show_on_dashboard is not None:
        if show_on_dashboard and is_header_account(db, account_id):
            raise GLError("لا يمكن عرض حساب تجميعي في لوحة التحكم — اختر حساباً فرعياً.")
        acc.show_on_dashboard = bool(show_on_dashboard)
    if business_domain is not None and not acc.is_system:
        dom = (business_domain or "").strip().lower()
        if dom in ("restaurant", "hotel", "shared"):
            acc.business_domain = dom
    db.flush()
    return acc


def list_wallet_maps(db: Session) -> list[GlPaymentMethodMap]:
    stmt = (
        select(GlPaymentMethodMap)
        .options(
            selectinload(GlPaymentMethodMap.payment_method),
            selectinload(GlPaymentMethodMap.gl_account),
        )
        .order_by(GlPaymentMethodMap.payment_method_id)
    )
    return list(db.scalars(stmt).all())


def try_post_gl_event(
    db: Session,
    *,
    source_type: str,
    source_id: int,
    idempotency_key: str,
) -> GlJournalEntry | None:
    """خطاف قديم — يُفضَّل استخدام modules.gl.posting مباشرة."""
    if not is_gl_enabled(db) or get_gl_post_mode(db) == "off":
        return None
    existing_id = db.scalar(
        select(GlJournalEntry.id).where(
            GlJournalEntry.idempotency_key == idempotency_key
        )
    )
    if existing_id:
        return db.get(GlJournalEntry, int(existing_id))
    return None


def list_journal_entries(
    db: Session,
    *,
    limit: int = 100,
    offset: int = 0,
    from_date: date | None = None,
    to_date: date | None = None,
    domain=None,
) -> list[GlJournalEntry]:
    from modules.gl.domain import gl_entry_db_values

    stmt = select(GlJournalEntry).options(
        selectinload(GlJournalEntry.lines).selectinload(GlJournalLine.account)
    )
    vals = gl_entry_db_values(domain)
    if vals is not None:
        stmt = stmt.where(GlJournalEntry.business_domain.in_(vals))
    if from_date is not None:
        stmt = stmt.where(GlJournalEntry.entry_date >= from_date)
    if to_date is not None:
        stmt = stmt.where(GlJournalEntry.entry_date <= to_date)
    stmt = (
        stmt.order_by(GlJournalEntry.id.desc())
        .offset(max(0, offset))
        .limit(min(max(1, limit), 500))
    )
    return list(db.scalars(stmt).all())


def journal_entry_count(
    db: Session,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    domain=None,
) -> int:
    from modules.gl.domain import gl_entry_db_values

    stmt = select(func.count(GlJournalEntry.id))
    vals = gl_entry_db_values(domain)
    if vals is not None:
        stmt = stmt.where(GlJournalEntry.business_domain.in_(vals))
    if from_date is not None:
        stmt = stmt.where(GlJournalEntry.entry_date >= from_date)
    if to_date is not None:
        stmt = stmt.where(GlJournalEntry.entry_date <= to_date)
    return int(db.scalar(stmt) or 0)


@dataclass
class ManualLineInput:
    account_id: int
    debit: Decimal
    credit: Decimal
    memo: str = ""


def create_manual_journal_entry(
    db: Session,
    *,
    entry_date: date,
    description_ar: str,
    lines: list[ManualLineInput],
    created_by_id: int | None = None,
    business_domain: str | None = None,
) -> GlJournalEntry:
    from modules.gl.posting import GlPostingError, _LineSpec, post_balanced_entry

    if not is_gl_enabled(db):
        raise GLError("فعّل GL أولاً.")
    if get_gl_post_mode(db) == "off":
        raise GLError("وضع الترحيل «متوقف» — غيّره من إعدادات GL.")

    desc = (description_ar or "").strip()
    if len(desc) < 2:
        raise GLError("وصف القيد مطلوب.")
    if not is_posting_date_allowed(db, entry_date):
        raise GLError("لا يمكن القيد في سنة مالية مغلقة أو غير موجودة.")
    if not lines:
        raise GLError("أضف سطراً واحداً على الأقل.")

    specs: list[_LineSpec] = []
    for i, ln in enumerate(lines, start=1):
        acc = db.get(GlAccount, ln.account_id)
        if acc is None or not acc.is_active:
            raise GLError(f"حساب غير صالح في السطر {i}.")
        try:
            assert_postable_account(db, int(acc.id))
        except ValueError as exc:
            raise GLError(str(exc)) from exc
        d = Decimal(str(ln.debit or 0)).quantize(Decimal("0.001"))
        c = Decimal(str(ln.credit or 0)).quantize(Decimal("0.001"))
        if d <= 0 and c <= 0:
            continue
        if d > 0 and c > 0:
            raise GLError(f"السطر {i}: لا يجمع مدين ودائن معاً.")
        specs.append(_LineSpec(acc.code, d, c, (ln.memo or "").strip()))

    if len(specs) < 2:
        raise GLError("القيد يحتاج سطرين على الأقل (مدين ودائن).")

    import uuid

    key = f"manual:{uuid.uuid4().hex}"
    try:
        entry = post_balanced_entry(
            db,
            idempotency_key=key,
            source_type="manual",
            source_id=0,
            description_ar=desc[:255],
            entry_date=entry_date,
            lines=specs,
            created_by_id=created_by_id,
            ignore_cutover=True,
            business_domain=business_domain,
        )
    except GlPostingError as exc:
        raise GLError(str(exc)) from exc
    if entry is None:
        raise GLError("تعذّر ترحيل القيد — تحقق من الإعدادات.")
    return entry


def set_wallet_map(db: Session, payment_method_id: int, gl_account_id: int) -> GlPaymentMethodMap:
    from modules.payments.models import PaymentMethod

    if db.get(PaymentMethod, payment_method_id) is None:
        raise GLError("المحفظة غير موجودة.")
    if db.get(GlAccount, gl_account_id) is None:
        raise GLError("حساب GL غير موجود.")
    try:
        assert_postable_account(db, gl_account_id)
    except ValueError as exc:
        raise GLError(str(exc)) from exc
    row = db.scalar(
        select(GlPaymentMethodMap).where(
            GlPaymentMethodMap.payment_method_id == payment_method_id
        )
    )
    if row is None:
        row = GlPaymentMethodMap(
            payment_method_id=payment_method_id, gl_account_id=gl_account_id
        )
        db.add(row)
    else:
        if int(row.gl_account_id) != int(gl_account_id):
            assert_wallet_map_can_change(db, payment_method_id)
        row.gl_account_id = gl_account_id
    db.flush()
    return row


def payment_method_has_activity(db: Session, payment_method_id: int) -> bool:
    from modules.delivery.models import DeliveryCashSettlement
    from modules.payments.models import (
        PaymentTransfer,
        Purchase,
        PurchasePayment,
        RefundPayment,
        SalePayment,
    )
    from modules.refunds.models import SaleReturn

    pm_id = int(payment_method_id)
    checks = [
        select(SalePayment.id).where(SalePayment.payment_method_id == pm_id).limit(1),
        select(Purchase.id).where(Purchase.payment_method_id == pm_id).limit(1),
        select(PurchasePayment.id).where(PurchasePayment.payment_method_id == pm_id).limit(1),
        select(RefundPayment.id).where(RefundPayment.payment_method_id == pm_id).limit(1),
        select(SaleReturn.id).where(SaleReturn.refund_payment_method_id == pm_id).limit(1),
        select(DeliveryCashSettlement.id)
        .where(DeliveryCashSettlement.cash_method_id == pm_id)
        .limit(1),
        select(PaymentTransfer.id)
        .where(
            (PaymentTransfer.from_payment_method_id == pm_id)
            | (PaymentTransfer.to_payment_method_id == pm_id)
        )
        .limit(1),
    ]
    return any(db.execute(stmt).scalar_one_or_none() is not None for stmt in checks)


def assert_wallet_map_can_change(db: Session, payment_method_id: int) -> None:
    if payment_method_has_activity(db, payment_method_id):
        raise GLError(
            "لا يمكن تغيير أو فك ربط محفظة عليها معاملات سابقة. أنشئ محفظة جديدة أو استخدم قيداً تصحيحياً."
        )


def delete_wallet_map(db: Session, payment_method_id: int) -> None:
    assert_wallet_map_can_change(db, payment_method_id)
    row = db.scalar(
        select(GlPaymentMethodMap).where(
            GlPaymentMethodMap.payment_method_id == int(payment_method_id)
        )
    )
    if row is None:
        return
    db.delete(row)
    db.flush()
