from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Path, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import GL_MANAGE
from modules.gl.models import AccountOpeningBalance, GlAccountType, GlPaymentMethodMap
from modules.gl.seed import account_type_label
from modules.gl.reconciliation import build_reconciliation
from modules.gl.role_maps import (
    OPERATIONAL_ROLES,
    list_role_maps,
    role_label,
    set_role_map,
)
from modules.gl.reports import build_balance_sheet, build_profit_loss, build_trial_balance
from modules.gl.service import (
    GL_SETTING_CUTOVER,
    GL_SETTING_ENABLED,
    GL_SETTING_POST_MODE,
    GLError,
    ManualLineInput,
    VALID_POST_MODES,
    accounts_by_type,
    account_balances_map,
    activate_production_gl,
    create_account,
    create_fiscal_year,
    create_manual_journal_entry,
    delete_wallet_map,
    get_current_fiscal_year,
    get_gl_cutover_date,
    gl_status_summary,
    is_gl_enabled,
    journal_entry_count,
    list_account_ledger,
    list_accounts,
    list_fiscal_years,
    list_journal_entries,
    list_wallet_maps,
    set_current_fiscal_year,
    set_opening_balance,
    set_wallet_map,
    update_account,
)
from modules.payments.service import list_payment_methods
from modules.gl.backfill import run_gl_backfill
from modules.settings.service import set_setting

router = APIRouter(prefix="/admin/gl", tags=["gl"])
_perm = require_permission(GL_MANAGE)


def _finance_domain_filter(request: Request, user: User):
    from modules.platform.business_domain import resolve_finance_domain

    return resolve_finance_domain(user, request.session)


_ACCOUNT_TYPES_FORM = [
    (GlAccountType.ASSET, account_type_label(GlAccountType.ASSET)),
    (GlAccountType.LIABILITY, account_type_label(GlAccountType.LIABILITY)),
    (GlAccountType.EQUITY, account_type_label(GlAccountType.EQUITY)),
    (GlAccountType.REVENUE, account_type_label(GlAccountType.REVENUE)),
    (GlAccountType.EXPENSE, account_type_label(GlAccountType.EXPENSE)),
]


def _err_redirect(url: str, msg: str) -> RedirectResponse:
    from urllib.parse import quote

    return RedirectResponse(f"{url}?err={quote(msg, safe='')}", status_code=303)


@router.get("", response_class=HTMLResponse)
def gl_admin_page(request: Request, db: DBSession, user: User = Depends(_perm)):
    from modules.platform.business_domain import domain_label, payment_method_visible_for_domain

    domain = _finance_domain_filter(request, user)
    status = gl_status_summary(db)
    wallet_maps = list_wallet_maps(db)
    if domain is not None:
        wallet_maps = [
            m
            for m in wallet_maps
            if m.payment_method is not None
            and payment_method_visible_for_domain(
                getattr(m.payment_method, "business_domain", None),
                filter_domain=domain,
            )
        ]
    cash_accounts = [
        a
        for a in list_accounts(db, active_only=True, domain=domain)
        if a.account_type.value in ("ASSET", "LIABILITY", "EQUITY")
    ]
    mapped_pm_ids = {m.payment_method_id for m in wallet_maps}
    unmapped_methods = [
        pm
        for pm in list_payment_methods(db, only_active=True)
        if pm.id not in mapped_pm_ids
        and pm.show_on_dashboard
        and payment_method_visible_for_domain(
            getattr(pm, "business_domain", None), filter_domain=domain
        )
    ]
    return templates.TemplateResponse(
        "admin_gl.html",
        {
            "request": request,
            "status": status,
            "wallet_maps": wallet_maps,
            "unmapped_methods": unmapped_methods,
            "cash_accounts": cash_accounts,
            "post_modes": [
                ("off", "متوقف — لا ترحيل تلقائي"),
                ("shadow", "مقارنة — للاختبار دون اعتماد التقارير"),
                ("live", "مباشر — النسخة التشغيلية (موصى به)"),
            ],
            "saved": request.query_params.get("saved") == "1",
            "err": request.query_params.get("err"),
            "err_msg": request.query_params.get("err"),
            "domain_label": domain_label(domain) if domain else "الكل",
            "finance_domain_filter": domain,
        },
    )


@router.get("/accounts", response_class=HTMLResponse)
def gl_accounts_page(request: Request, db: DBSession, user: User = Depends(_perm)):
    from modules.gl.hierarchy import account_tree_depth, display_balances_map, header_account_ids
    from modules.platform.business_domain import domain_label

    domain = _finance_domain_filter(request, user)
    grouped = accounts_by_type(db, domain=domain)
    parent_options = list_accounts(db, active_only=False, domain=domain)
    raw_balances = account_balances_map(db, domain=domain)
    balances = display_balances_map(db, raw_balances)
    header_ids = header_account_ids(db)
    depths = {acc.id: account_tree_depth(db, acc) for _, _, rows in grouped for acc in rows}
    status = gl_status_summary(db)
    pos_wallet_ids = {
        int(x)
        for x in db.scalars(select(GlPaymentMethodMap.gl_account_id)).all()
        if x is not None
    }
    return templates.TemplateResponse(
        "admin_gl_accounts.html",
        {
            "request": request,
            "grouped": grouped,
            "parent_options": parent_options,
            "account_types": _ACCOUNT_TYPES_FORM,
            "account_type_label": account_type_label,
            "balances": balances,
            "header_ids": header_ids,
            "depths": depths,
            "gl_status": status,
            "pos_wallet_ids": pos_wallet_ids,
            "saved": request.query_params.get("saved") == "1",
            "pos_linked": request.query_params.get("pos") == "1",
            "err_msg": request.query_params.get("err"),
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "الكل",
        },
    )


@router.get("/accounts/{account_id}", response_class=HTMLResponse)
def gl_account_ledger_page(
    account_id: int,
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
):
    from modules.platform.business_domain import domain_label

    page = max(0, int(request.query_params.get("page", "0") or "0"))
    limit = 80
    from_date: date | None = None
    to_date: date | None = None
    raw_from = (request.query_params.get("from") or "").strip()
    raw_to = (request.query_params.get("to") or "").strip()
    try:
        if raw_from:
            from_date = date.fromisoformat(raw_from)
        if raw_to:
            to_date = date.fromisoformat(raw_to)
    except ValueError:
        from_date = None
        to_date = None
    domain = _finance_domain_filter(request, user)
    try:
        ledger = list_account_ledger(
            db,
            account_id,
            limit=limit,
            offset=page * limit,
            from_date=from_date,
            to_date=to_date,
            domain=domain,
        )
    except GLError as e:
        return _err_redirect("/admin/gl/accounts", str(e))
    status = gl_status_summary(db)
    return templates.TemplateResponse(
        "admin_gl_account_ledger.html",
        {
            "request": request,
            "ledger": ledger,
            "gl_status": status,
            "page": page,
            "limit": limit,
            "has_next": (page + 1) * limit < ledger.total_lines,
            "from_date": raw_from,
            "to_date": raw_to,
            "account_type_label": account_type_label,
            "domain_label": domain_label(domain) if domain else "الكل",
        },
    )


@router.post("/accounts/create")
def gl_account_create(
    db: DBSession,
    _: User = Depends(_perm),
    code: str = Form(...),
    name_ar: str = Form(...),
    account_type: str = Form(...),
    parent_id: str = Form(""),
    notes: str = Form(""),
):
    try:
        acc_type = GlAccountType(account_type)
    except ValueError:
        return _err_redirect("/admin/gl/accounts", "نوع الحساب غير صالح.")
    pid: int | None = None
    if parent_id.strip():
        try:
            pid = int(parent_id.strip())
        except ValueError:
            return _err_redirect("/admin/gl/accounts", "الحساب الأب غير صالح.")
    try:
        create_account(
            db,
            code=code,
            name_ar=name_ar,
            account_type=acc_type,
            parent_id=pid,
            notes=notes,
        )
        db.commit()
    except GLError as e:
        return _err_redirect("/admin/gl/accounts", str(e))
    return RedirectResponse("/admin/gl/accounts?saved=1", status_code=303)


@router.post("/accounts/{account_id}/update")
def gl_account_update(
    account_id: int,
    db: DBSession,
    _: User = Depends(_perm),
    code: str = Form(""),
    name_ar: str = Form(...),
    notes: str = Form(""),
    is_active: str = Form(""),
    show_on_dashboard: str = Form(""),
):
    try:
        update_account(
            db,
            account_id,
            code=code.strip() or None,
            name_ar=name_ar,
            notes=notes,
            is_active=is_active in ("1", "on", "true"),
            show_on_dashboard=show_on_dashboard in ("1", "on", "true"),
        )
        db.commit()
    except GLError as e:
        return _err_redirect("/admin/gl/accounts", str(e))
    return RedirectResponse("/admin/gl/accounts?saved=1", status_code=303)


@router.post("/accounts/{account_id}/provision-pos-wallet")
def gl_provision_pos_wallet(
    account_id: int,
    db: DBSession,
    _: User = Depends(_perm),
):
    from modules.gl.pos_wallet import provision_pos_wallet_for_gl_account

    try:
        provision_pos_wallet_for_gl_account(db, account_id)
        db.commit()
    except GLError as e:
        return _err_redirect("/admin/gl/accounts", str(e))
    return RedirectResponse("/admin/gl/accounts?saved=1&pos=1", status_code=303)


def _parse_report_dates(request: Request) -> tuple[date, date, str, str, str]:
    """from, to, period, start_str, end_str"""
    period = (request.query_params.get("period") or "month").strip()
    raw_start = (request.query_params.get("start") or "").strip()
    raw_end = (request.query_params.get("end") or "").strip()
    today = datetime.now().date()
    if period == "custom" and raw_start and raw_end:
        try:
            fd = date.fromisoformat(raw_start)
            td = date.fromisoformat(raw_end)
            if fd <= td:
                return fd, td, "custom", raw_start, raw_end
        except ValueError:
            pass
    if period == "year":
        fd = today.replace(month=1, day=1)
        return fd, today, "year", fd.isoformat(), today.isoformat()
    if period == "week":
        fd = today - timedelta(days=today.weekday())
        return fd, today, "week", fd.isoformat(), today.isoformat()
    if period == "day":
        return today, today, "day", today.isoformat(), today.isoformat()
    # month default
    fd = today.replace(day=1)
    return fd, today, "month", fd.isoformat(), today.isoformat()


_PERIOD_LABELS = {
    "day": "اليوم",
    "week": "الأسبوع الحالي",
    "month": "الشهر الحالي",
    "year": "السنة الحالية",
    "custom": "فترة مخصصة",
}


@router.get("/reports", response_class=HTMLResponse)
def gl_reports_hub(request: Request, db: DBSession, user: User = Depends(_perm)):
    from modules.platform.business_domain import domain_label

    from_date, to_date, period, start_str, end_str = _parse_report_dates(request)
    report = (request.query_params.get("report") or "pl").strip()
    status = gl_status_summary(db)
    domain = _finance_domain_filter(request, user)
    ctx: dict = {
        "request": request,
        "gl_status": status,
        "period": period,
        "period_labels": _PERIOD_LABELS,
        "from_date": from_date,
        "to_date": to_date,
        "start_str": start_str,
        "end_str": end_str,
        "report": report,
        "finance_domain_filter": domain,
        "domain_label": domain_label(domain) if domain else "الكل",
    }
    if report == "tb":
        ctx["trial_balance"] = build_trial_balance(
            db, from_date=from_date, to_date=to_date, domain=domain
        )
    elif report == "bs":
        ctx["balance_sheet"] = build_balance_sheet(db, as_of=to_date, domain=domain)
    else:
        ctx["profit_loss"] = build_profit_loss(
            db, from_date=from_date, to_date=to_date, domain=domain
        )
    return templates.TemplateResponse("admin_gl_reports.html", ctx)


@router.get("/reports/export.xlsx")
def gl_reports_export_xlsx(
    request: Request, db: DBSession, user: User = Depends(_perm)
):
    from modules.gl.exports import export_gl_report

    from_date, to_date, _, _, _ = _parse_report_dates(request)
    report = (request.query_params.get("report") or "pl").strip()
    domain = _finance_domain_filter(request, user)
    return export_gl_report(
        db,
        report=report,
        from_date=from_date,
        to_date=to_date,
        fmt="xlsx",
        domain=domain,
    )


@router.get("/reports/export.csv")
def gl_reports_export_csv(
    request: Request, db: DBSession, user: User = Depends(_perm)
):
    from modules.gl.exports import export_gl_report

    from_date, to_date, _, _, _ = _parse_report_dates(request)
    report = (request.query_params.get("report") or "pl").strip()
    domain = _finance_domain_filter(request, user)
    return export_gl_report(
        db,
        report=report,
        from_date=from_date,
        to_date=to_date,
        fmt="csv",
        domain=domain,
    )


@router.get("/journal/export.xlsx")
def gl_journal_export_xlsx(
    request: Request, db: DBSession, user: User = Depends(_perm)
):
    from modules.gl.exports import export_gl_journal

    domain = _finance_domain_filter(request, user)

    from_date: date | None = None
    to_date: date | None = None
    raw_from = (request.query_params.get("from") or "").strip()
    raw_to = (request.query_params.get("to") or "").strip()
    try:
        if raw_from:
            from_date = date.fromisoformat(raw_from)
        if raw_to:
            to_date = date.fromisoformat(raw_to)
    except ValueError:
        from_date = None
        to_date = None
    return export_gl_journal(db, from_date=from_date, to_date=to_date, fmt="xlsx", domain=domain)


@router.get("/journal/export.csv")
def gl_journal_export_csv(
    request: Request, db: DBSession, user: User = Depends(_perm)
):
    from modules.gl.exports import export_gl_journal

    domain = _finance_domain_filter(request, user)

    from_date: date | None = None
    to_date: date | None = None
    raw_from = (request.query_params.get("from") or "").strip()
    raw_to = (request.query_params.get("to") or "").strip()
    try:
        if raw_from:
            from_date = date.fromisoformat(raw_from)
        if raw_to:
            to_date = date.fromisoformat(raw_to)
    except ValueError:
        from_date = None
        to_date = None
    return export_gl_journal(db, from_date=from_date, to_date=to_date, fmt="csv", domain=domain)


@router.get("/reconciliation/export.xlsx")
def gl_reconciliation_export_xlsx(
    request: Request, db: DBSession, user: User = Depends(_perm)
):
    from modules.gl.exports import export_gl_reconciliation

    domain = _finance_domain_filter(request, user)
    return export_gl_reconciliation(db, fmt="xlsx", domain=domain)


@router.get("/reconciliation/export.csv")
def gl_reconciliation_export_csv(
    request: Request, db: DBSession, user: User = Depends(_perm)
):
    from modules.gl.exports import export_gl_reconciliation

    domain = _finance_domain_filter(request, user)
    return export_gl_reconciliation(db, fmt="csv", domain=domain)


@router.get("/expense-maps", response_class=HTMLResponse)
def gl_expense_maps_page(
    request: Request, db: DBSession, user: User = Depends(_perm)
):
    from modules.platform.business_domain import domain_label

    domain = _finance_domain_filter(request, user)
    expense_accounts = [
        a
        for a in list_accounts(db, domain=domain)
        if a.account_type == GlAccountType.EXPENSE
    ]
    return templates.TemplateResponse(
        "admin_gl_expense_maps.html",
        {
            "request": request,
            "maps": list_expense_category_maps(db),
            "expense_accounts": expense_accounts,
            "saved": request.query_params.get("saved") == "1",
            "err_msg": request.query_params.get("err"),
            "domain_label": domain_label(domain) if domain else "الكل",
        },
    )


@router.post("/expense-maps")
def gl_expense_map_save(
    db: DBSession,
    _: User = Depends(_perm),
    category_label: str = Form(...),
    gl_account_id: int = Form(...),
):
    try:
        set_expense_category_map(db, category_label=category_label, gl_account_id=gl_account_id)
        db.commit()
    except GLError as e:
        return _err_redirect("/admin/gl/expense-maps", str(e))
    return RedirectResponse("/admin/gl/expense-maps?saved=1", status_code=303)


@router.post("/expense-maps/{map_id}/delete")
def gl_expense_map_delete(map_id: int, db: DBSession, _: User = Depends(_perm)):
    try:
        delete_expense_category_map(db, map_id)
        db.commit()
    except GLError as e:
        return _err_redirect("/admin/gl/expense-maps", str(e))
    return RedirectResponse("/admin/gl/expense-maps?saved=1", status_code=303)


@router.get("/journal/new", response_class=HTMLResponse)
def gl_journal_manual_form(
    request: Request, db: DBSession, user: User = Depends(_perm)
):
    from modules.platform.business_domain import domain_label

    domain = _finance_domain_filter(request, user)
    return templates.TemplateResponse(
        "admin_gl_journal_manual.html",
        {
            "request": request,
            "accounts": list_accounts(db, domain=domain),
            "account_types": _ACCOUNT_TYPES_FORM,
            "account_type_label": account_type_label,
            "err_msg": request.query_params.get("err"),
            "saved": request.query_params.get("saved") == "1",
            "domain_label": domain_label(domain) if domain else "الكل",
            "finance_domain_filter": domain,
        },
    )


@router.post("/journal/manual")
async def gl_journal_manual_post(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    entry_date: str = Form(...),
    description_ar: str = Form(...),
):
    try:
        ed = date.fromisoformat((entry_date or "").strip())
    except ValueError:
        return _err_redirect("/admin/gl/journal/new", "تاريخ القيد غير صالح.")
    raw_form = await request.form()
    account_ids = raw_form.getlist("account_id")
    debits = raw_form.getlist("debit")
    credits = raw_form.getlist("credit")
    memos = raw_form.getlist("memo")
    lines: list[ManualLineInput] = []
    for i, aid_raw in enumerate(account_ids):
        if not str(aid_raw).strip():
            continue
        try:
            aid = int(aid_raw)
        except ValueError:
            continue
        d_raw = debits[i] if i < len(debits) else "0"
        c_raw = credits[i] if i < len(credits) else "0"
        memo = memos[i] if i < len(memos) else ""
        try:
            d = Decimal(str(d_raw or "0").replace(",", "").strip() or "0")
            c = Decimal(str(c_raw or "0").replace(",", "").strip() or "0")
        except Exception:
            d, c = Decimal("0"), Decimal("0")
        lines.append(ManualLineInput(account_id=aid, debit=d, credit=c, memo=str(memo or "")))
    try:
        domain = _finance_domain_filter(request, user)
        entry_dom = domain.value if domain is not None else None
        create_manual_journal_entry(
            db,
            entry_date=ed,
            description_ar=description_ar,
            lines=lines,
            created_by_id=int(user.id),
            business_domain=entry_dom,
        )
        db.commit()
    except GLError as e:
        return _err_redirect("/admin/gl/journal/new", str(e))
    return RedirectResponse("/admin/gl/journal?saved=1", status_code=303)


@router.get("/journal", response_class=HTMLResponse)
def gl_journal_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
):
    from modules.platform.business_domain import domain_label

    page = max(0, int(request.query_params.get("page", "0") or "0"))
    limit = 50
    from_date: date | None = None
    to_date: date | None = None
    raw_from = (request.query_params.get("from") or "").strip()
    raw_to = (request.query_params.get("to") or "").strip()
    try:
        if raw_from:
            from_date = date.fromisoformat(raw_from)
        if raw_to:
            to_date = date.fromisoformat(raw_to)
    except ValueError:
        from_date = None
        to_date = None
    domain = _finance_domain_filter(request, user)
    entries = list_journal_entries(
        db,
        limit=limit,
        offset=page * limit,
        from_date=from_date,
        to_date=to_date,
        domain=domain,
    )
    total = journal_entry_count(db, from_date=from_date, to_date=to_date, domain=domain)
    return templates.TemplateResponse(
        "admin_gl_journal.html",
        {
            "request": request,
            "entries": entries,
            "page": page,
            "limit": limit,
            "total": total,
            "has_next": (page + 1) * limit < total,
            "backfill_done": request.query_params.get("backfill") == "1",
            "from_date": raw_from,
            "to_date": raw_to,
            "domain_label": domain_label(domain) if domain else "الكل",
            "finance_domain_filter": domain,
        },
    )


@router.post("/backfill")
def gl_backfill_action(db: DBSession, _: User = Depends(_perm)):
    from modules.gl.service import is_gl_enabled

    if not is_gl_enabled(db):
        return RedirectResponse("/admin/gl?err=gl_off", status_code=303)
    run_gl_backfill(db)
    db.commit()
    return RedirectResponse("/admin/gl/journal?backfill=1", status_code=303)


@router.get("/reconciliation", response_class=HTMLResponse)
def gl_reconciliation_page(
    request: Request, db: DBSession, user: User = Depends(_perm)
):
    from modules.platform.business_domain import domain_label

    domain = _finance_domain_filter(request, user)
    summary = build_reconciliation(db, domain=domain)
    return templates.TemplateResponse(
        "admin_gl_reconciliation.html",
        {
            "request": request,
            "summary": summary,
            "domain_label": domain_label(domain) if domain else "الكل",
            "finance_domain_filter": domain,
        },
    )


@router.post("/wallet-maps")
def gl_wallet_map_save(
    db: DBSession,
    _: User = Depends(_perm),
    payment_method_id: int = Form(...),
    gl_account_id: int = Form(...),
):
    try:
        set_wallet_map(db, payment_method_id, gl_account_id)
        db.commit()
    except GLError as e:
        return _err_redirect("/admin/gl", str(e))
    return RedirectResponse("/admin/gl?saved=1", status_code=303)


@router.post("/wallet-maps/delete")
def gl_wallet_map_delete(
    db: DBSession,
    _: User = Depends(_perm),
    payment_method_id: int = Form(...),
):
    try:
        delete_wallet_map(db, payment_method_id)
        db.commit()
    except GLError as e:
        return _err_redirect("/admin/gl", str(e))
    return RedirectResponse("/admin/gl?saved=1", status_code=303)


@router.post("/activate-production")
def gl_activate_production(db: DBSession, _: User = Depends(_perm)):
    from datetime import date as date_cls

    cutover = get_gl_cutover_date(db)
    activate_production_gl(db, cutover_date=cutover or date_cls.today())
    from modules.gl.pos_wallet import hide_wallets_from_dashboard_when_gl

    hide_wallets_from_dashboard_when_gl(db)
    db.commit()
    return RedirectResponse("/admin/gl?saved=1", status_code=303)


@router.get("/role-maps", response_class=HTMLResponse)
def gl_role_maps_page(request: Request, db: DBSession, user: User = Depends(_perm)):
    from modules.platform.business_domain import domain_label

    domain = _finance_domain_filter(request, user)
    asset_accounts = [
        a
        for a in list_accounts(db, domain=domain)
        if a.account_type == GlAccountType.ASSET
    ]
    expense_accounts = [
        a
        for a in list_accounts(db, domain=domain)
        if a.account_type == GlAccountType.EXPENSE
    ]
    maps = {m.role_key: m for m in list_role_maps(db)}
    roles = []
    for role_key, default_code, desc in OPERATIONAL_ROLES:
        m = maps.get(role_key)
        roles.append(
            {
                "key": role_key,
                "desc": desc,
                "default_code": default_code,
                "map": m,
                "account_type": "ASSET"
                if role_key in ("fixed_asset", "accumulated_depreciation")
                else "EXPENSE",
            }
        )
    return templates.TemplateResponse(
        "admin_gl_role_maps.html",
        {
            "request": request,
            "roles": roles,
            "asset_accounts": asset_accounts,
            "expense_accounts": expense_accounts,
            "saved": request.query_params.get("saved") == "1",
            "err_msg": request.query_params.get("err"),
            "domain_label": domain_label(domain) if domain else "الكل",
        },
    )


@router.post("/role-maps")
def gl_role_map_save(
    db: DBSession,
    _: User = Depends(_perm),
    role_key: str = Form(...),
    gl_account_id: int = Form(...),
):
    try:
        set_role_map(db, role_key=role_key.strip(), gl_account_id=gl_account_id)
        db.commit()
    except GLError as e:
        return _err_redirect("/admin/gl/role-maps", str(e))
    return RedirectResponse("/admin/gl/role-maps?saved=1", status_code=303)


@router.post("/post-depreciation")
def gl_post_depreciation_period(
    db: DBSession,
    _: User = Depends(_perm),
    start: str = Form(""),
    end: str = Form(""),
    redirect_to: str = Form("/reports/assets"),
):
    from modules.gl.posting import post_period_depreciation_shadow
    from modules.reporting import queries as report_queries

    if (start or "").strip() and (end or "").strip():
        custom = report_queries.parse_custom_range(start, end)
        if custom is None:
            return _err_redirect("/admin/gl/role-maps", "تواريخ الفترة غير صالحة.")
        s, e = custom
    else:
        s, e = report_queries.period_bounds("month")
    if not is_gl_enabled(db):
        return _err_redirect("/admin/gl", "فعّل GL أولاً.")
    try:
        n = post_period_depreciation_shadow(db, s, e)
        db.commit()
    except Exception as exc:
        return _err_redirect("/admin/gl/role-maps", str(exc)[:200])
    sep = "&" if "?" in redirect_to else "?"
    return RedirectResponse(f"{redirect_to}{sep}depr_posted={n}", status_code=303)


@router.post("/settings")
def gl_save_settings(
    db: DBSession,
    _: User = Depends(_perm),
    gl_enabled: str = Form("0"),
    gl_post_mode: str = Form("live"),
    gl_cutover_date: str = Form(""),
):
    enabled = "1" if gl_enabled in ("1", "on", "true") else "0"
    mode = (gl_post_mode or "live").strip().lower()
    if mode not in VALID_POST_MODES:
        return RedirectResponse("/admin/gl?err=mode", status_code=303)
    cutover_raw = (gl_cutover_date or "").strip()
    if cutover_raw:
        try:
            date.fromisoformat(cutover_raw)
        except ValueError:
            return RedirectResponse("/admin/gl?err=cutover", status_code=303)
    set_setting(db, GL_SETTING_ENABLED, enabled)
    set_setting(db, GL_SETTING_POST_MODE, mode)
    set_setting(db, GL_SETTING_CUTOVER, cutover_raw)
    if enabled == "1":
        from modules.gl.pos_wallet import hide_wallets_from_dashboard_when_gl

        hide_wallets_from_dashboard_when_gl(db)
    db.commit()
    return RedirectResponse("/admin/gl?saved=1", status_code=303)


@router.get("/fiscal", response_class=HTMLResponse)
def fiscal_years_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    err: str = "",
    saved: str = "",
):
    return templates.TemplateResponse(
        "admin_gl_fiscal.html",
        {
            "request": request,
            "fiscal_years": list_fiscal_years(db),
            "err": err or None,
            "saved": saved,
        },
    )


@router.post("/fiscal")
def fiscal_years_create(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    name: str = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
):
    try:
        s = date.fromisoformat(start_date)
        e = date.fromisoformat(end_date)
        create_fiscal_year(db, name, s, e, set_as_current=True)
        db.commit()
    except GLError as exc:
        return _err_redirect("/admin/gl/fiscal", str(exc))
    except ValueError:
        return _err_redirect("/admin/gl/fiscal", "تاريخ غير صالح")
    return RedirectResponse("/admin/gl/fiscal?saved=1", status_code=303)


@router.post("/fiscal/{fiscal_year_id}/set-current")
def fiscal_years_set_current(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    fiscal_year_id: int = Path(...),
):
    try:
        set_current_fiscal_year(db, fiscal_year_id)
        db.commit()
    except GLError as exc:
        return _err_redirect("/admin/gl/fiscal", str(exc))
    return RedirectResponse("/admin/gl/fiscal?saved=1", status_code=303)


@router.get("/opening-balances", response_class=HTMLResponse)
def opening_balances_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    err: str = "",
    saved: str = "",
):
    from modules.gl.models import AccountOpeningBalance

    fy = get_current_fiscal_year(db)
    rows = []
    if fy is not None:
        existing = {
            ob.account_id: ob
            for ob in db.scalars(
                select(AccountOpeningBalance).where(AccountOpeningBalance.fiscal_year_id == fy.id)
            ).all()
        }
        for acc in list_accounts(db, active_only=False):
            ob = existing.get(int(acc.id))
            rows.append(
                {
                    "account": acc,
                    "debit": ob.debit if ob else Decimal("0"),
                    "credit": ob.credit if ob else Decimal("0"),
                    "note": ob.note if ob else "",
                }
            )
    return templates.TemplateResponse(
        "admin_gl_opening_balances.html",
        {
            "request": request,
            "fiscal_year": fy,
            "rows": rows,
            "err": err or None,
            "saved": saved,
        },
    )


@router.post("/opening-balances")
async def opening_balances_save(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
):
    from decimal import InvalidOperation

    fy = get_current_fiscal_year(db)
    if fy is None:
        return _err_redirect("/admin/gl/opening-balances", "لا توجد سنة مالية حالية")
    try:
        form = await request.form()
        for acc in list_accounts(db, active_only=False):
            debit_raw = form.get(f"debit_{acc.id}") or "0"
            credit_raw = form.get(f"credit_{acc.id}") or "0"
            note = form.get(f"note_{acc.id}") or ""
            set_opening_balance(db, int(fy.id), int(acc.id), Decimal(debit_raw), Decimal(credit_raw), note)
        db.commit()
    except (ValueError, InvalidOperation) as exc:
        return _err_redirect("/admin/gl/opening-balances", f"قيمة غير صالحة: {exc}")
    return RedirectResponse("/admin/gl/opening-balances?saved=1", status_code=303)
