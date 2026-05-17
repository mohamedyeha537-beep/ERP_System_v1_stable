from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.deps import DBSession, require_any_permission, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import PAYMENTS_MANAGE, REPORTS_VIEW, SALES_CREATE
from modules.pos_shifts.models import PosShift, PosShiftStatus
from modules.payments.models import PaymentMethodKind
from modules.payments.service import (
    PaymentsError,
    list_payment_methods_transfer_sources,
    list_payment_methods_transfer_targets,
    payment_method_balances_map,
    record_manual_transfer,
)
from modules.payments.treasury_service import (
    daily_balance_rows,
    list_ledger_entries,
    treasury_summary_for_kind,
    treasury_summary_for_method,
)
from modules.pos_shifts.service import (
    PosShiftError,
    build_shift_sale_rows,
    close_shift,
    compute_expected_cash,
    compute_expected_bank,
    get_shift,
    get_open_shift_for_user,
    list_completed_sales_for_shift,
    open_shift,
    sync_session_pos_shift,
)
from modules.reporting.exports import csv_response
from modules.settings.service import get_setting

router = APIRouter(prefix="/pos", tags=["pos-shift"])
_pos_perm = require_permission(SALES_CREATE)
_treasury_manage = require_permission(PAYMENTS_MANAGE)
_treasury_view = require_any_permission(SALES_CREATE, PAYMENTS_MANAGE, REPORTS_VIEW)


def _parse_money(raw: str) -> Decimal:
    s = (raw or "").strip().replace(",", ".")
    if not s:
        raise ValueError("empty")
    return Decimal(s).quantize(Decimal("0.001"))


def _owns_shift(user: User, sh: PosShift) -> bool:
    return sh.user_id == user.id


def _render_shift_page(
    request: Request,
    db: DBSession,
    user: User,
    *,
    error: str | None,
    need_open: bool,
    closed_shift_id: int | None,
    err_param: str | None,
    status_code: int = 200,
):
    sync_session_pos_shift(request=request, db=db, user_id=user.id)
    open_s = get_open_shift_for_user(db, user.id)
    recent = list(
        db.scalars(
            select(PosShift)
            .where(
                PosShift.user_id == user.id,
                PosShift.status == PosShiftStatus.CLOSED,
            )
            .order_by(PosShift.id.desc())
            .limit(12)
        ).all()
    )
    store_name = get_setting(db, "store_name", "نقطة البيع")
    msg = err_param or error
    return templates.TemplateResponse(
        "pos_shift.html",
        {
            "request": request,
            "user": user,
            "open_shift": open_s,
            "recent_closed": recent,
            "need_open": need_open,
            "closed_shift_id": closed_shift_id,
            "error": msg,
            "store_name": store_name,
        },
        status_code=status_code,
    )


@router.get("/shift", response_class=HTMLResponse)
def pos_shift_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_pos_perm),
    need: str | None = Query(None),
    closed: str | None = Query(None),
    err: str | None = Query(None),
):
    closed_id: int | None = None
    if closed:
        try:
            closed_id = int(closed)
        except ValueError:
            closed_id = None
    return _render_shift_page(
        request,
        db,
        user,
        error=None,
        need_open=bool(need),
        closed_shift_id=closed_id,
        err_param=err,
        status_code=200,
    )


@router.post("/shift/open", response_class=HTMLResponse)
def pos_shift_open(
    request: Request,
    db: DBSession,
    user: User = Depends(_pos_perm),
    opening_note: str = Form(""),
):
    try:
        sh = open_shift(db, user.id, opening_note=opening_note)
        db.commit()
        request.session["pos_shift_id"] = sh.id
    except PosShiftError as e:
        db.rollback()
        return _render_shift_page(
            request,
            db,
            user,
            error=str(e),
            need_open=False,
            closed_shift_id=None,
            err_param=None,
            status_code=400,
        )
    return RedirectResponse("/pos", status_code=302)


@router.post("/shift/close", response_class=HTMLResponse)
def pos_shift_close(
    request: Request,
    db: DBSession,
    user: User = Depends(_pos_perm),
    counted_cash: str = Form(...),
    counted_bank: str = Form(""),
    closing_note: str = Form(""),
):
    sid = request.session.get("pos_shift_id")
    try:
        shift_id = int(sid) if sid is not None else 0
    except (TypeError, ValueError):
        shift_id = 0
    try:
        cc = _parse_money(counted_cash)
    except (InvalidOperation, ValueError):
        return RedirectResponse(
            "/pos/shift?err=" + quote("المبلغ المعدود (كاش) غير صالح."),
            status_code=302,
        )
    cb: Decimal | None = None
    bank_raw = (counted_bank or "").strip()
    if bank_raw:
        try:
            cb = _parse_money(bank_raw)
        except (InvalidOperation, ValueError):
            return RedirectResponse(
                "/pos/shift?err=" + quote("رصيد المصرف المعدود غير صالح."),
                status_code=302,
            )
    try:
        close_shift(
            db,
            shift_id=shift_id,
            user_id=user.id,
            counted_cash=cc,
            counted_bank=cb,
            closing_note=closing_note,
        )
        db.commit()
    except PosShiftError as e:
        db.rollback()
        return RedirectResponse("/pos/shift?err=" + quote(str(e)), status_code=302)
    request.session.pop("pos_shift_id", None)
    return RedirectResponse(f"/pos/shift?closed={shift_id}", status_code=302)


@router.get("/shift/{shift_id}/export.csv")
def pos_shift_export_csv(
    shift_id: int,
    db: DBSession,
    user: User = Depends(_pos_perm),
):
    sh = get_shift(db, shift_id)
    if sh is None or not _owns_shift(user, sh):
        return RedirectResponse("/pos/shift?err=" + quote("غير مصرح."), status_code=302)
    rows = build_shift_sale_rows(db, shift_id)
    summary_rows: list[tuple] = [
        ("نوع السطر", "1", "2", "3", "4"),
        ("ملخص", f"جلسة #{shift_id}", "", "", ""),
        ("الحالة", sh.status.value, "", "", ""),
        ("فتح", sh.opened_at.strftime("%Y-%m-%d %H:%M") if sh.opened_at else "", "", "", ""),
        (
            "إغلاق",
            sh.closed_at.strftime("%Y-%m-%d %H:%M") if sh.closed_at else "",
            "",
            "",
            "",
        ),
        (
            "كاش متوقع",
            str(sh.expected_cash) if sh.expected_cash is not None else "",
            "",
            "",
            "",
        ),
        (
            "كاش معدود",
            str(sh.counted_cash) if sh.counted_cash is not None else "",
            "",
            "",
            "",
        ),
        (
            "الفرق (معدود − متوقع)",
            str(sh.cash_difference) if sh.cash_difference is not None else "",
            "",
            "",
            "",
        ),
        (
            "مصرف متوقع",
            str(sh.expected_bank) if sh.expected_bank is not None else "",
            "",
            "",
            "",
        ),
        (
            "مصرف معدود",
            str(sh.counted_bank) if sh.counted_bank is not None else "",
            "",
            "",
            "",
        ),
        (
            "فرق المصرف",
            str(sh.bank_difference) if sh.bank_difference is not None else "",
            "",
            "",
            "",
        ),
        ("", "", "", "", ""),
        ("فاتورة", "سياق", "الدفع", "الإجمالي", "وقت الإنشاء"),
    ]
    detail = [
        (r.sale_id, r.context, r.payment_label, r.total, r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "")
        for r in rows
    ]
    return csv_response(
        f"pos_shift_{shift_id}",
        ["عمود1", "عمود2", "عمود3", "عمود4", "عمود5"],
        summary_rows + detail,
    )


@router.get("/shift/{shift_id}/report", response_class=HTMLResponse)
def pos_shift_report_print(
    request: Request,
    shift_id: int,
    db: DBSession,
    user: User = Depends(_pos_perm),
):
    sh = get_shift(db, shift_id)
    if sh is None or not _owns_shift(user, sh):
        return RedirectResponse("/pos/shift?err=" + quote("غير مصرح."), status_code=302)
    rows = build_shift_sale_rows(db, shift_id)
    sales = list_completed_sales_for_shift(db, shift_id)
    total = sum(
        (Decimal(str(s.total or 0)) for s in sales),
        Decimal("0"),
    ).quantize(Decimal("0.001"))
    store_name = get_setting(db, "store_name", "نقطة البيع")
    preview_expected = None
    if sh.status == PosShiftStatus.OPEN:
        preview_expected = compute_expected_cash(db, shift_id)
    return templates.TemplateResponse(
        "pos_shift_report.html",
        {
            "request": request,
            "shift": sh,
            "rows": rows,
            "sales_count": len(sales),
            "sales_total": total,
            "store_name": store_name,
            "preview_expected_cash": preview_expected,
        },
    )


@router.get("/treasury", response_class=HTMLResponse)
def pos_treasury_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_treasury_view),
    kind: str = Query("cash"),
    pm: int | None = Query(None),
    dir: str = Query("all"),
    day: str | None = Query(None),
):
    import logging

    from modules.payments.models import PaymentMethod
    from modules.payments.service import list_payment_methods_for_dashboard

    log = logging.getLogger("pos.treasury")
    try:
        pm_kind = PaymentMethodKind(kind.upper())
    except ValueError:
        pm_kind = PaymentMethodKind.CASH
    if pm_kind not in (PaymentMethodKind.CASH, PaymentMethodKind.BANK):
        pm_kind = PaymentMethodKind.CASH

    filter_day: date | None = None
    if day:
        try:
            filter_day = date.fromisoformat(day.strip())
        except ValueError:
            filter_day = None

    direction = (dir or "all").lower()
    if direction not in ("all", "in", "out"):
        direction = "all"

    treasury_accounts = list_payment_methods_for_dashboard(db, only_active=True)
    selected_pm_id = pm
    if selected_pm_id is not None:
        acc = db.get(PaymentMethod, selected_pm_id)
        if acc is None or not acc.show_on_dashboard:
            selected_pm_id = None

    try:
        if selected_pm_id is not None:
            summary = treasury_summary_for_method(db, selected_pm_id)
            pm_kind = summary.kind
            all_entries = list_ledger_entries(
                db, pm_kind, payment_method_id=selected_pm_id
            )
        else:
            summary = treasury_summary_for_kind(db, pm_kind)
            all_entries = list_ledger_entries(db, pm_kind)
        daily = daily_balance_rows(db, pm_kind, entries=all_entries)
        entries = all_entries
        if filter_day is not None:
            entries = [e for e in entries if e.at.date() == filter_day]
        if direction == "in":
            entries = [e for e in entries if e.direction == "IN"]
        elif direction == "out":
            entries = [e for e in entries if e.direction == "OUT"]
    except Exception as exc:
        log.exception("treasury page failed: %s", exc)
        label = "خزينة الكاش" if pm_kind == PaymentMethodKind.CASH else "خزينة المصرف"
        return templates.TemplateResponse(
            "pos_treasury.html",
            {
                "request": request,
                "user": user,
                "kind": pm_kind.value.lower(),
                "kind_label": label,
                "selected_pm_id": selected_pm_id,
                "treasury_accounts": treasury_accounts,
                "current_balance": Decimal("0"),
                "summary": None,
                "entries": [],
                "daily_rows": [],
                "filter_dir": direction,
                "filter_day": filter_day.isoformat() if filter_day else "",
                "store_name": get_setting(db, "store_name", "نقطة البيع"),
                "page_error": "تعذّر تحميل حركة الخزينة. راجع سجل السيرفر أو أعد التشغيل.",
            },
        )

    return templates.TemplateResponse(
        "pos_treasury.html",
        {
            "request": request,
            "user": user,
            "kind": pm_kind.value.lower(),
            "kind_label": summary.label_ar,
            "selected_pm_id": selected_pm_id,
            "treasury_accounts": treasury_accounts,
            "current_balance": summary.current_balance,
            "summary": summary,
            "entries": entries,
            "daily_rows": daily,
            "filter_dir": direction,
            "filter_day": filter_day.isoformat() if filter_day else "",
            "store_name": get_setting(db, "store_name", "نقطة البيع"),
            "page_error": None,
        },
    )


@router.get("/treasury/transfer", response_class=HTMLResponse)
def pos_treasury_transfer_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_treasury_view),
    kind: str = Query("cash"),
    error: str | None = Query(None),
    ok: str | None = Query(None),
):
    import logging

    log = logging.getLogger("pos.treasury")
    try:
        pm_kind = PaymentMethodKind(kind.upper())
    except ValueError:
        pm_kind = PaymentMethodKind.CASH
    try:
        sources = list_payment_methods_transfer_sources(db, only_active=True)
        targets = list_payment_methods_transfer_targets(db, only_active=True)
        all_balances = payment_method_balances_map(db)
        balances = {
            m.id: all_balances.get(m.id, Decimal("0"))
            for m in {*sources, *targets}
        }
    except Exception as exc:
        log.exception("treasury transfer page failed: %s", exc)
        return templates.TemplateResponse(
            "pos_treasury_transfer.html",
            {
                "request": request,
                "kind": pm_kind.value.lower(),
                "sources": [],
                "targets": [],
                "balances": {},
                "error": error,
                "ok": ok,
                "page_error": "تعذّر تحميل صفحة التحويل. راجع سجل السيرفر أو أعد تشغيل التطبيق.",
            },
        )
    return templates.TemplateResponse(
        "pos_treasury_transfer.html",
        {
            "request": request,
            "kind": pm_kind.value.lower(),
            "sources": sources,
            "targets": targets,
            "balances": balances,
            "error": error,
            "ok": ok,
            "page_error": None,
        },
    )


@router.post("/treasury/transfer", response_class=HTMLResponse)
def pos_treasury_transfer_submit(
    request: Request,
    db: DBSession,
    user: User = Depends(_treasury_manage),
    from_payment_method_id: str = Form(...),
    to_payment_method_id: str = Form(...),
    amount: str = Form(...),
    note: str = Form(""),
    kind: str = Form("cash"),
):
    try:
        from_id = int(from_payment_method_id)
        to_id = int(to_payment_method_id)
        amt = _parse_money(amount)
    except (ValueError, InvalidOperation):
        return RedirectResponse(
            "/pos/treasury/transfer?kind="
            + quote(kind)
            + "&error="
            + quote("بيانات التحويل غير صالحة."),
            status_code=302,
        )
    try:
        record_manual_transfer(
            db,
            from_payment_method_id=from_id,
            to_payment_method_id=to_id,
            amount=amt,
            user_id=user.id,
            note=note,
        )
        db.commit()
    except PaymentsError as e:
        db.rollback()
        return RedirectResponse(
            "/pos/treasury/transfer?kind="
            + quote(kind)
            + "&error="
            + quote(str(e)),
            status_code=302,
        )
    except Exception as e:
        db.rollback()
        import logging

        logging.getLogger("pos.treasury").exception("treasury transfer submit failed: %s", e)
        return RedirectResponse(
            "/pos/treasury/transfer?kind="
            + quote(kind)
            + "&error="
            + quote("تعذّر تنفيذ التحويل. راجع سجل السيرفر."),
            status_code=302,
        )
    return RedirectResponse(
        "/pos/treasury/transfer?kind=" + quote(kind) + "&ok=1",
        status_code=302,
    )
