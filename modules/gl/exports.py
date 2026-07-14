"""تصدير تقارير GL إلى Excel (.xlsx) و CSV."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from modules.gl.reconciliation import GlReconciliationSummary, build_reconciliation
from modules.gl.reports import (
    BalanceSheetReport,
    ProfitLossReport,
    TrialBalanceReport,
    build_balance_sheet,
    build_profit_loss,
    build_trial_balance,
)
from modules.gl.models import GlJournalEntry
from modules.gl.service import list_journal_entries
from modules.reporting.exports import SheetSpec, csv_response, xlsx_response

SheetRows = tuple[str, list[str], list[tuple]]


def _amt(v: Decimal) -> str:
    return f"{Decimal(str(v or 0)).quantize(Decimal('0.001')):.3f}"


def _account_label(code: str, name: str) -> str:
    return f"{code} — {name}"


def profit_loss_sheets(pl: ProfitLossReport) -> list[SheetRows]:
    title = f"قائمة الدخل {pl.from_date} → {pl.to_date}"
    summary: list[tuple] = [
        ("إجمالي الإيرادات", _amt(pl.total_revenue)),
        ("إجمالي المصروفات", _amt(pl.total_expense)),
        ("صافي الربح / الخسارة", _amt(pl.net_income)),
    ]
    rev_rows = [
        (_account_label(ln.account.code, ln.account.name_ar), _amt(ln.amount))
        for ln in pl.revenue_lines
    ]
    exp_rows = [
        (_account_label(ln.account.code, ln.account.name_ar), _amt(ln.amount))
        for ln in pl.expense_lines
    ]
    return [
        ("ملخص", ["البند", "المبلغ (د.ل)"], summary),
        ("الإيرادات", ["الحساب", "المبلغ (د.ل)"], rev_rows or [("—", "0.000")]),
        ("المصروفات", ["الحساب", "المبلغ (د.ل)"], exp_rows or [("—", "0.000")]),
    ]


def balance_sheet_sheets(bs: BalanceSheetReport) -> list[SheetRows]:
    sheets: list[SheetRows] = []
    for sec in bs.sections:
        rows = [
            (_account_label(ln.account.code, ln.account.name_ar), _amt(ln.display_amount))
            for ln in sec.lines
        ]
        if rows:
            rows.append((f"إجمالي {sec.label}", _amt(sec.subtotal)))
        else:
            rows = [("—", "0.000")]
        sheets.append((sec.label[:31], ["الحساب", "المبلغ (د.ل)"], rows))
    sheets.append(
        (
            "ملخص",
            ["البند", "المبلغ (د.ل)"],
            [
                ("إجمالي الأصول", _amt(bs.total_assets)),
                ("إجمالي الخصوم", _amt(bs.total_liabilities)),
                ("حقوق الملكية (حسابات)", _amt(bs.total_equity_accounts)),
                ("ربح/خسارة متراكم", _amt(bs.unclosed_net_income)),
                ("الخصوم + حقوق الملكية", _amt(bs.total_liabilities_equity)),
            ],
        )
    )
    return sheets


def trial_balance_sheets(tb: TrialBalanceReport) -> list[SheetRows]:
    rows = [
        (
            _account_label(r.account.code, r.account.name_ar),
            _amt(r.period_debit) if r.period_debit > 0 else "",
            _amt(r.period_credit) if r.period_credit > 0 else "",
            _amt(r.tb_debit) if r.tb_debit > 0 else "",
            _amt(r.tb_credit) if r.tb_credit > 0 else "",
        )
        for r in tb.rows
    ]
    rows.append(
        (
            "الإجمالي",
            "",
            "",
            _amt(tb.total_debit),
            _amt(tb.total_credit),
        )
    )
    return [
        (
            "ميزان المراجعة",
            [
                "الحساب",
                "مدين فترة",
                "دائن فترة",
                "رصيد — مدين",
                "رصيد — دائن",
            ],
            rows,
        )
    ]


def journal_sheets(entries: list[GlJournalEntry]) -> list[SheetRows]:
    rows: list[tuple] = []
    for e in entries:
        for ln in e.lines:
            acc = ln.account
            rows.append(
                (
                    e.id,
                    e.entry_date.isoformat() if e.entry_date else "",
                    e.description_ar or "",
                    e.source_type or "",
                    e.source_id or "",
                    e.post_mode or "",
                    e.status.value if e.status else "",
                    acc.code if acc else "",
                    acc.name_ar if acc else "",
                    _amt(ln.debit) if ln.debit > 0 else "",
                    _amt(ln.credit) if ln.credit > 0 else "",
                    (ln.memo or "")[:255],
                )
            )
    if not rows:
        rows = [("", "", "لا توجد قيود", "", "", "", "", "", "", "", "", "")]
    return [
        (
            "قيود اليومية",
            [
                "رقم القيد",
                "التاريخ",
                "الوصف",
                "نوع المصدر",
                "معرف المصدر",
                "وضع الترحيل",
                "الحالة",
                "رمز الحساب",
                "اسم الحساب",
                "مدين",
                "دائن",
                "ملاحظة",
            ],
            rows,
        )
    ]


def reconciliation_sheets(summary: GlReconciliationSummary) -> list[SheetRows]:
    ar_rows = [
        ("ذمم تشغيلية", _amt(summary.ar_operational)),
        ("رصيد GL — 1200", _amt(summary.ar_gl)),
        ("الفرق", _amt(summary.ar_difference)),
    ]
    wallet_rows = [
        (
            row.payment_method_name,
            f"{row.gl_account_code} — {row.gl_account_name or ''}".strip(" —"),
            _amt(row.operational_balance),
            _amt(row.gl_balance),
            _amt(row.difference),
        )
        for row in summary.wallet_rows
    ] or [("—", "—", "0.000", "0.000", "0.000")]
    return [
        ("ذمم مدينة", ["البند", "المبلغ (د.ل)"], ar_rows),
        (
            "محافظ GL",
            ["المحفظة", "حساب GL", "رصيد تشغيلي", "رصيد GL", "فرق"],
            wallet_rows,
        ),
    ]


def _sheets_to_csv(sheets: list[SheetRows]) -> tuple[list[str], list[tuple]]:
    """دمج أوراق Excel في CSV واحد (أقسام مفصولة بصف فارغ)."""
    headers: list[str] = []
    rows: list[tuple] = []
    for sheet_name, hdrs, sheet_rows in sheets:
        if rows:
            rows.append(())
        rows.append((f"=== {sheet_name} ===",))
        if not headers:
            headers = list(hdrs)
        rows.append(tuple(hdrs))
        rows.extend(sheet_rows)
    if not headers:
        headers = ["البيان"]
    return headers, rows


def export_gl_report(
    db: Session,
    *,
    report: str,
    from_date: date,
    to_date: date,
    fmt: str,
    domain=None,
):
    report = (report or "pl").strip().lower()
    if report == "tb":
        data = build_trial_balance(db, from_date=from_date, to_date=to_date, domain=domain)
        sheets = trial_balance_sheets(data)
        base = f"gl-trial-balance-{to_date.isoformat()}"
    elif report == "bs":
        data = build_balance_sheet(db, as_of=to_date, domain=domain)
        sheets = balance_sheet_sheets(data)
        base = f"gl-balance-sheet-{to_date.isoformat()}"
    else:
        data = build_profit_loss(db, from_date=from_date, to_date=to_date, domain=domain)
        sheets = profit_loss_sheets(data)
        base = f"gl-profit-loss-{from_date.isoformat()}-{to_date.isoformat()}"
    if domain is not None:
        base = f"{base}-{domain.value if hasattr(domain, 'value') else domain}"

    specs = [SheetSpec(name=n, headers=h, rows=r) for n, h, r in sheets]
    if fmt == "csv":
        hdrs, rows = _sheets_to_csv(sheets)
        return csv_response(base, hdrs, rows)
    return xlsx_response(base, specs)


def export_gl_journal(
    db: Session,
    *,
    from_date: date | None,
    to_date: date | None,
    fmt: str,
    domain=None,
):
    entries = list_journal_entries(
        db, from_date=from_date, to_date=to_date, limit=50000, offset=0, domain=domain
    )
    sheets = journal_sheets(entries)
    suffix = ""
    if from_date:
        suffix += f"-from-{from_date.isoformat()}"
    if to_date:
        suffix += f"-to-{to_date.isoformat()}"
    base = f"gl-journal{suffix or '-all'}"
    if domain is not None:
        base = f"{base}-{domain.value if hasattr(domain, 'value') else domain}"
    specs = [SheetSpec(name=n, headers=h, rows=r) for n, h, r in sheets]
    if fmt == "csv":
        hdrs, rows = _sheets_to_csv(sheets)
        return csv_response(base, hdrs, rows)
    return xlsx_response(base, specs)


def export_gl_reconciliation(db: Session, *, fmt: str, domain=None):
    summary = build_reconciliation(db, domain=domain)
    sheets = reconciliation_sheets(summary)
    base = "gl-reconciliation"
    if domain is not None:
        base = f"{base}-{domain.value if hasattr(domain, 'value') else domain}"
    specs = [SheetSpec(name=n, headers=h, rows=r) for n, h, r in sheets]
    if fmt == "csv":
        hdrs, rows = _sheets_to_csv(sheets)
        return csv_response(base, hdrs, rows)
    return xlsx_response(base, specs)
