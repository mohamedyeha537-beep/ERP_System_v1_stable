from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from modules.gl.models import GlAccount, GlAccountType, GlJournalEntry, GlJournalLine, GlPaymentMethodMap
from modules.gl.hierarchy import (
    assert_postable_account,
    build_children_map,
    direct_children,
    display_balances_map,
    is_header_account,
)
from modules.gl.seed import account_type_label
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
    return Decimal(str(net or 0)).quantize(Decimal("0.001"))


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


def wallet_maps_for_account(db: Session, account_id: int) -> list[GlPaymentMethodMap]:
    return list(
        db.scalars(
            select(GlPaymentMethodMap)
            .where(GlPaymentMethodMap.gl_account_id == account_id)
            .options(selectinload(GlPaymentMethodMap.payment_method))
        ).all()
    )


def list_account_ledger(
    db: Session,
    account_id: int,
    *,
    limit: int = 80,
    offset: int = 0,
    from_date: date | None = None,
    to_date: date | None = None,
    domain=None,
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

    count_stmt = (
        select(func.count(GlJournalLine.id))
        .select_from(GlJournalLine)
        .join(GlJournalEntry, GlJournalEntry.id == GlJournalLine.entry_id)
        .where(GlJournalLine.account_id == account_id)
    )
    if domain_vals is not None:
        count_stmt = count_stmt.where(GlJournalEntry.business_domain.in_(domain_vals))
    if from_date is not None:
        count_stmt = count_stmt.where(GlJournalEntry.entry_date >= from_date)
    if to_date is not None:
        count_stmt = count_stmt.where(GlJournalEntry.entry_date <= to_date)
    total_lines = int(db.scalar(count_stmt) or 0)

    chrono = base.order_by(
        GlJournalEntry.entry_date.asc(),
        GlJournalEntry.id.asc(),
        GlJournalLine.line_no.asc(),
    )
    opening = Decimal("0")
    if offset > 0:
        for ln, _ent in db.execute(chrono.limit(offset)).all():
            opening += Decimal(str(ln.debit or 0)) - Decimal(str(ln.credit or 0))
        opening = opening.quantize(Decimal("0.001"))

    period_balance = Decimal("0")
    for ln, _ent in db.execute(chrono).all():
        period_balance += Decimal(str(ln.debit or 0)) - Decimal(str(ln.credit or 0))
    period_balance = period_balance.quantize(Decimal("0.001"))

    running = opening
    rows: list[AccountLedgerRow] = []
    for ln, ent in db.execute(chrono.offset(offset).limit(limit)).all():
        d = Decimal(str(ln.debit or 0)).quantize(Decimal("0.001"))
        c = Decimal(str(ln.credit or 0)).quantize(Decimal("0.001"))
        running = (running + d - c).quantize(Decimal("0.001"))
        rows.append(
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
