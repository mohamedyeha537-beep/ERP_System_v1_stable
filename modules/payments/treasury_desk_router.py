"""شاشات أمين الخزينة التشغيلية — لوحة، استلامات، عهد، صرف، إقفال."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import DBSession, require_any_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import PAYMENTS_MANAGE, REPORTS_VIEW, TREASURY_HANDOFF_APPROVE
from modules.payments.service import PaymentsError, record_expense, record_purchase_payment
from modules.payments.treasury_desk import (
    TreasuryDeskError,
    build_advance_view,
    build_required_actions,
    close_purchase_advance,
    close_treasury_session,
    create_purchase_advance,
    get_last_closed_treasury_session,
    get_open_treasury_session,
    list_bank_receipt_rows,
    list_cash_receipt_rows,
    list_money_holders,
    list_purchase_advances,
    list_recent_receipt_vouchers,
    load_treasury_balances,
    open_treasury_session,
    parse_charge_domain,
    return_purchase_advance,
    session_movements,
    today_movement,
    treasury_pay_balances,
    treasury_pay_methods,
)

router = APIRouter(prefix="/pos/treasury", tags=["treasury-desk"])
_view = require_any_permission(PAYMENTS_MANAGE, TREASURY_HANDOFF_APPROVE, REPORTS_VIEW)
_act = require_any_permission(PAYMENTS_MANAGE, TREASURY_HANDOFF_APPROVE)


def _money(raw: str) -> Decimal:
    s = (raw or "").strip().replace(",", ".")
    if not s:
        raise InvalidOperation
    return Decimal(s).quantize(Decimal("0.001"))


def _err(path: str, message: str) -> RedirectResponse:
    return RedirectResponse(path + ("&" if "?" in path else "?") + "err=" + quote(message), 302)


def _work_domain(request: Request, user: User):
    from modules.platform.business_domain import domain_label, resolve_finance_domain

    domain = resolve_finance_domain(user, request.session)
    return domain, domain_label(domain) if domain else "الكل"


@router.get("/desk", response_class=HTMLResponse)
def treasury_desk_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_view),
):
    from modules.gl.dashboard import gl_dashboard_treasury_cards
    from modules.gl.service import is_gl_enabled

    domain, domain_label = _work_domain(request, user)
    bals = load_treasury_balances(db, domain=domain)
    session = get_open_treasury_session(db)
    last = get_last_closed_treasury_session(db)
    vaults = gl_dashboard_treasury_cards(db, domain=domain) if is_gl_enabled(db) else []
    reception_deficits: list[tuple[str, Decimal]] = []
    try:
        from modules.hotel.shift_handoff import hotel_reception_wallet_deficits

        reception_deficits = hotel_reception_wallet_deficits(db)
    except Exception:  # noqa: BLE001
        reception_deficits = []
    return templates.TemplateResponse(
        "treasury/desk.html",
        {
            "request": request,
            "user": user,
            "bals": bals,
            "actions": build_required_actions(db, domain=domain),
            "movement": today_movement(db),
            "session": session,
            "last_session": last,
            "vaults": vaults,
            "finance_domain": domain.value if domain else None,
            "domain_label": domain_label,
            "reception_deficits": reception_deficits,
            "err": request.query_params.get("err"),
            "ok": request.query_params.get("ok"),
        },
    )


def _receipts_page(
    request: Request,
    db,
    user,
    *,
    tab: str,
    domain: str | None,
    title: str,
    receive_ok_path: str,
):
    tab = tab if tab in ("cash", "bank") else "cash"
    return templates.TemplateResponse(
        "treasury/receipts.html",
        {
            "request": request,
            "user": user,
            "tab": tab,
            "domain": domain or "all",
            "page_title": title,
            "receive_ok_path": receive_ok_path,
            "cash_rows": list_cash_receipt_rows(db, domain=domain),
            "bank_rows": list_bank_receipt_rows(db, domain=domain),
            "err": request.query_params.get("err"),
            "ok": request.query_params.get("ok"),
        },
    )


@router.post("/repair-hotel-reception", response_class=HTMLResponse)
def treasury_repair_hotel_reception(
    db: DBSession,
    user: User = Depends(_act),
):
    from modules.hotel.shift_handoff import (
        HotelShiftHandoffError,
        reconcile_hotel_reception_wallet_deficits,
    )

    try:
        reconcile_hotel_reception_wallet_deficits(
            db,
            user_id=user.id,
            context="تسوية إدارية — عجز استقبال الفندق",
        )
        db.commit()
    except (HotelShiftHandoffError, PaymentsError) as exc:
        db.rollback()
        return RedirectResponse(
            f"/pos/treasury/desk?err={quote(str(exc))}",
            status_code=302,
        )
    return RedirectResponse("/pos/treasury/desk?ok=reception_repaired", status_code=302)


@router.get("/receipts", response_class=HTMLResponse)
def treasury_receipts_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_view),
    tab: str = Query("cash"),
    domain: str = Query(""),
):
    dom = domain if domain in ("hotel", "restaurant") else None
    if dom == "hotel":
        return RedirectResponse("/pos/treasury/hotel-shifts?tab=" + quote(tab), 302)
    if dom == "restaurant":
        return RedirectResponse("/pos/treasury/pos-shifts?tab=" + quote(tab), 302)
    return _receipts_page(
        request,
        db,
        user,
        tab=tab,
        domain=None,
        title="استلامات الورديات",
        receive_ok_path="/pos/treasury/receipts",
    )


@router.get("/hotel-shifts", response_class=HTMLResponse)
def treasury_hotel_shifts_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_view),
    tab: str = Query("cash"),
):
    return _receipts_page(
        request,
        db,
        user,
        tab=tab,
        domain="hotel",
        title="تسليم ورديات الاستقبال",
        receive_ok_path="/pos/treasury/hotel-shifts",
    )


@router.get("/pos-shifts", response_class=HTMLResponse)
def treasury_pos_shifts_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_view),
    tab: str = Query("cash"),
):
    return _receipts_page(
        request,
        db,
        user,
        tab=tab,
        domain="restaurant",
        title="تسليم المطعم والكافيه",
        receive_ok_path="/pos/treasury/pos-shifts",
    )


@router.post("/receipts/pos/{shift_id}/receive", response_class=HTMLResponse)
def treasury_receive_pos(
    shift_id: int,
    db: DBSession,
    user: User = Depends(_act),
    handoff_cash: str = Form(""),
    handoff_bank: str = Form(""),
    handoff_note: str = Form(""),
):
    from modules.payments.shift_handoff_service import (
        ShiftHandoffError,
        approve_shift_handoff,
        parse_handoff_amount,
    )

    try:
        approve_shift_handoff(
            db,
            shift_id=shift_id,
            user_id=user.id,
            admin_username=user.username,
            handoff_cash=parse_handoff_amount(handoff_cash),
            handoff_bank=parse_handoff_amount(handoff_bank),
            handoff_note=handoff_note,
        )
        db.commit()
    except ShiftHandoffError as exc:
        db.rollback()
        return _err("/pos/treasury/pos-shifts", str(exc))
    return RedirectResponse("/pos/treasury/pos-shifts?ok=received", 302)


@router.post("/receipts/hotel/{shift_id}/receive", response_class=HTMLResponse)
def treasury_receive_hotel(
    shift_id: int,
    db: DBSession,
    user: User = Depends(_act),
    handoff_cash: str = Form(""),
    handoff_bank: str = Form(""),
    handoff_note: str = Form(""),
):
    from modules.hotel.shift_handoff import (
        HotelShiftHandoffError,
        approve_hotel_shift_handoff,
        parse_handoff_amount,
    )

    try:
        approve_hotel_shift_handoff(
            db,
            shift_id=shift_id,
            user_id=user.id,
            admin_username=user.username,
            handoff_cash=parse_handoff_amount(handoff_cash),
            handoff_bank=parse_handoff_amount(handoff_bank),
            handoff_note=handoff_note,
        )
        db.commit()
    except HotelShiftHandoffError as exc:
        db.rollback()
        return _err("/pos/treasury/hotel-shifts", str(exc))
    return RedirectResponse("/pos/treasury/hotel-shifts?ok=received", 302)


@router.post("/receipts/bank/{handover_id}/match", response_class=HTMLResponse)
def treasury_match_bank(
    handover_id: int,
    db: DBSession,
    user: User = Depends(_act),
    received_bank: str = Form(...),
    note: str = Form(""),
):
    from modules.payments.shift_handovers import ShiftHandoverError, match_bank_transfer

    try:
        amt = _money(received_bank)
        match_bank_transfer(
            db,
            handover_id=handover_id,
            received_bank=amt,
            user_id=user.id,
            note=note,
        )
        db.commit()
    except (InvalidOperation, ShiftHandoverError) as exc:
        db.rollback()
        msg = "مبلغ غير صالح." if isinstance(exc, InvalidOperation) else str(exc)
        return _err("/pos/treasury/banks", msg)
    return RedirectResponse("/pos/treasury/banks?ok=matched", 302)


@router.get("/advances", response_class=HTMLResponse)
def treasury_advances_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_view),
):
    return templates.TemplateResponse(
        "treasury/advances.html",
        {
            "request": request,
            "user": user,
            "holders": list_money_holders(db),
            "err": request.query_params.get("err"),
        },
    )


@router.get("/custody", response_class=HTMLResponse)
def treasury_custody_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_view),
    status: str = Query("OPEN"),
):
    from modules.hr.service import list_employees

    return templates.TemplateResponse(
        "treasury/custody.html",
        {
            "request": request,
            "user": user,
            "rows": list_purchase_advances(db, status=status or None),
            "status_filter": status,
            "employees": list_employees(db, only_active=True),
            "pay_methods": treasury_pay_methods(db),
            "err": request.query_params.get("err"),
            "ok": request.query_params.get("ok"),
        },
    )


@router.get("/custody/{advance_id}", response_class=HTMLResponse)
def treasury_custody_detail(
    advance_id: int,
    request: Request,
    db: DBSession,
    user: User = Depends(_view),
):
    from modules.payments.purchase_advance_models import PurchaseAdvance

    row = db.get(PurchaseAdvance, int(advance_id))
    if row is None:
        return RedirectResponse("/pos/treasury/custody?err=" + quote("العهدة غير موجودة."), 302)
    return templates.TemplateResponse(
        "treasury/custody_detail.html",
        {
            "request": request,
            "user": user,
            "adv": build_advance_view(db, row),
            "err": request.query_params.get("err"),
            "ok": request.query_params.get("ok"),
        },
    )


@router.post("/custody/create", response_class=HTMLResponse)
def treasury_custody_create(
    db: DBSession,
    user: User = Depends(_act),
    employee_id: str = Form(...),
    amount: str = Form(...),
    source_pm_id: str = Form(...),
    purpose: str = Form(""),
):
    try:
        adv = create_purchase_advance(
            db,
            employee_id=int(employee_id),
            amount=_money(amount),
            source_pm_id=int(source_pm_id),
            purpose=purpose,
            user_id=user.id,
        )
        db.commit()
    except (ValueError, InvalidOperation, TreasuryDeskError, PaymentsError) as exc:
        db.rollback()
        msg = "بيانات غير صالحة." if isinstance(exc, (ValueError, InvalidOperation)) else str(exc)
        return _err("/pos/treasury/custody", msg)
    return RedirectResponse(f"/pos/treasury/custody/{adv.id}?ok=created", 302)


@router.post("/custody/{advance_id}/return", response_class=HTMLResponse)
def treasury_custody_return(
    advance_id: int,
    db: DBSession,
    user: User = Depends(_act),
    amount: str = Form(...),
):
    try:
        return_purchase_advance(
            db, advance_id=advance_id, amount=_money(amount), user_id=user.id
        )
        db.commit()
    except (InvalidOperation, TreasuryDeskError, PaymentsError) as exc:
        db.rollback()
        msg = "مبلغ غير صالح." if isinstance(exc, InvalidOperation) else str(exc)
        return _err(f"/pos/treasury/custody/{advance_id}", msg)
    return RedirectResponse(f"/pos/treasury/custody/{advance_id}?ok=returned", 302)


@router.post("/custody/{advance_id}/close", response_class=HTMLResponse)
def treasury_custody_close(
    advance_id: int,
    db: DBSession,
    user: User = Depends(_act),
):
    try:
        close_purchase_advance(db, advance_id=advance_id, user_id=user.id)
        db.commit()
    except TreasuryDeskError as exc:
        db.rollback()
        return _err(f"/pos/treasury/custody/{advance_id}", str(exc))
    return RedirectResponse(f"/pos/treasury/custody/{advance_id}?ok=closed", 302)


def _fallback_pay_extras() -> dict:
    import json

    from modules.payments.pay_categories import KIND_OPERATING, KIND_UTILITY, builtin_categories

    return {
        "operating_categories": builtin_categories(KIND_OPERATING),
        "utility_categories": builtin_categories(KIND_UTILITY),
        "employees": [],
        "suppliers": [],
        "invoices": [],
        "invoices_json": "[]",
        "can_manage_pay_categories": True,
    }


def _employee_outstanding_map(db, table: str) -> dict[int, object]:
    from decimal import Decimal as D

    from sqlalchemy import text

    out: dict[int, D] = {}
    try:
        rows = db.execute(
            text(
                f"SELECT employee_id, COALESCE(SUM(amount - repaid_amount), 0) "
                f"FROM {table} "
                f"WHERE status IN ('OUTSTANDING', 'PARTIALLY_REPAID') "
                f"GROUP BY employee_id"
            )
        ).all()
    except Exception:
        return out
    for eid, rem in rows:
        if eid is None:
            continue
        val = D(str(rem or 0)).quantize(D("0.001"))
        if val > 0:
            out[int(eid)] = val
    return out


def _safe_pay_employees(db) -> list[dict]:
    from decimal import Decimal as D

    from sqlalchemy import text

    rows = db.execute(
        text(
            "SELECT id, full_name_ar, job_title, base_monthly_salary, status "
            "FROM hr_employees ORDER BY full_name_ar"
        )
    ).all()
    ded_map = _employee_outstanding_map(db, "hr_employee_deductions")
    adv_map = _employee_outstanding_map(db, "hr_advances")
    out = []
    for r in rows:
        status = str(r[4] or "").upper()
        eid = int(r[0])
        gross = D(str(r[3] or 0)).quantize(D("0.001"))
        if gross < 0:
            gross = D("0")
        adv = min(adv_map.get(eid, D("0")), gross)
        rem = (gross - adv).quantize(D("0.001"))
        ded = min(ded_map.get(eid, D("0")), rem)
        net = (gross - adv - ded).quantize(D("0.001"))
        if net < 0:
            net = D("0")
        out.append(
            {
                "id": eid,
                "name": str(r[1] or "").strip() or f"موظف #{r[0]}",
                "job": str(r[2] or "").strip(),
                "salary": str(gross),
                "deductions": str(ded),
                "advances": str(adv),
                "net": str(net),
                "active": status in ("", "ACTIVE"),
            }
        )
    out.sort(key=lambda x: (not x["active"], x["name"]))
    return out


def _safe_pay_suppliers_and_invoices(db, domain) -> tuple[list[str], list[dict]]:
    from decimal import Decimal as D

    from sqlalchemy import text

    rows = db.execute(
        text(
            "SELECT p.id, p.supplier, p.supplier_invoice_ref, p.amount, p.created_at, "
            "COALESCE((SELECT SUM(pp.amount) FROM purchase_payments pp "
            "WHERE pp.purchase_id = p.id), 0) AS paid "
            "FROM purchases p WHERE p.kind = 'INVENTORY' ORDER BY p.id DESC"
        )
    ).all()
    invoices: list[dict] = []
    names: set[str] = set()
    for r in rows:
        supplier = str(r[1] or "").strip()
        if supplier:
            names.add(supplier)
        total = D(str(r[3] or 0)).quantize(D("0.001"))
        paid = D(str(r[5] or 0)).quantize(D("0.001"))
        outstanding = (total - paid).quantize(D("0.001"))
        if outstanding <= 0:
            continue
        created = r[4]
        invoices.append(
            {
                "id": int(r[0]),
                "supplier": supplier or "مورد غير محدد",
                "date": created.strftime("%Y-%m-%d") if created is not None else "",
                "ref": str(r[2] or "").strip(),
                "total": str(total),
                "paid": str(paid),
                "outstanding": str(outstanding),
                "status": "غير مدفوعة" if paid <= 0 else "مدفوعة جزئياً",
            }
        )
    return sorted(names), invoices


def _pay_page_extras(db, user, domain):
    import json

    from modules.payments.pay_categories import (
        KIND_OPERATING,
        KIND_UTILITY,
        active_categories,
        builtin_categories,
        ensure_pay_category_gl_maps,
        seed_default_pay_categories,
    )

    employees: list[dict] = []
    suppliers: list[str] = []
    invoices: list[dict] = []
    try:
        employees = _safe_pay_employees(db)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    try:
        suppliers, invoices = _safe_pay_suppliers_and_invoices(db, domain)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    try:
        seed_default_pay_categories(db)
        ensure_pay_category_gl_maps(db)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    try:
        ops = active_categories(db, kind=KIND_OPERATING) or builtin_categories(KIND_OPERATING)
        utils = active_categories(db, kind=KIND_UTILITY) or builtin_categories(KIND_UTILITY)
    except Exception:
        ops = builtin_categories(KIND_OPERATING)
        utils = builtin_categories(KIND_UTILITY)
    return {
        "operating_categories": ops,
        "utility_categories": utils,
        "employees": employees,
        "suppliers": suppliers,
        "invoices": invoices,
        "invoices_json": json.dumps(invoices, ensure_ascii=False).replace("<", "\\u003c"),
        "can_manage_pay_categories": True,
    }


@router.get("/pay", response_class=HTMLResponse)
def treasury_pay_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_view),
):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from modules.payments.models import PurchaseKind, PurchasePayment
    from modules.payments.service import list_purchases

    domain, domain_label = _work_domain(request, user)
    end = datetime.now(timezone.utc) + timedelta(days=1)
    start = end - timedelta(days=31)
    recent = list_purchases(
        db,
        start,
        end,
        kind=PurchaseKind.EXPENSE,
        limit=40,
        exclude_loyalty=True,
        domain=domain,
    )
    recent_settlements = list(
        db.scalars(
            select(PurchasePayment)
            .options(
                selectinload(PurchasePayment.method),
                selectinload(PurchasePayment.purchase),
            )
            .where(PurchasePayment.created_at >= start)
            .order_by(PurchasePayment.id.desc())
            .limit(20)
        ).all()
    )
    try:
        extras = _pay_page_extras(db, user, domain)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        extras = _fallback_pay_extras()
    return templates.TemplateResponse(
        "treasury/pay.html",
        {
            "request": request,
            "user": user,
            "pay_methods": treasury_pay_methods(db, domain=domain),
            "pay_balances": treasury_pay_balances(db, domain=domain),
            "bals": load_treasury_balances(db, domain=domain),
            "recent_vouchers": recent,
            "recent_settlements": recent_settlements,
            "finance_domain": domain.value if domain else "restaurant",
            "domain_label": domain_label,
            "err": request.query_params.get("err"),
            "ok": request.query_params.get("ok"),
            **extras,
        },
    )


@router.get("/vouchers/in", response_class=HTMLResponse)
def treasury_vouchers_in_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_view),
):
    bals = load_treasury_balances(db)
    return templates.TemplateResponse(
        "treasury/vouchers_in.html",
        {
            "request": request,
            "user": user,
            "movement": today_movement(db),
            "recent_receipts": list_recent_receipt_vouchers(db),
            "bals": bals,
            "err": request.query_params.get("err"),
            "ok": request.query_params.get("ok"),
        },
    )


@router.get("/vouchers/out", response_class=HTMLResponse)
def treasury_vouchers_out_redirect(_: User = Depends(_view)):
    return RedirectResponse("/pos/treasury/pay", 302)


@router.get("/debts", response_class=HTMLResponse)
def treasury_debts_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_view),
):
    return templates.TemplateResponse(
        "treasury/debts.html",
        {
            "request": request,
            "user": user,
            "err": request.query_params.get("err"),
        },
    )


@router.get("/banks", response_class=HTMLResponse)
def treasury_banks_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_view),
    tab: str = Query("match"),
):
    domain, _label = _work_domain(request, user)
    bals = load_treasury_balances(db, domain=domain)
    key = domain.value if domain else None
    return templates.TemplateResponse(
        "treasury/banks.html",
        {
            "request": request,
            "user": user,
            "bals": bals,
            "tab": tab if tab in ("match", "move") else "match",
            "bank_rows": list_bank_receipt_rows(db, domain=key),
            "pay_methods": treasury_pay_methods(db, domain=domain),
            "err": request.query_params.get("err"),
            "ok": request.query_params.get("ok"),
        },
    )


@router.get("/reports", response_class=HTMLResponse)
def treasury_reports_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_view),
):
    return templates.TemplateResponse(
        "treasury/reports.html",
        {
            "request": request,
            "user": user,
            "movement": today_movement(db),
            "bals": load_treasury_balances(db),
        },
    )


def _require_pay_wallet(db, user, request, payment_method_id: str, business_domain: str):
    domain, _label = _work_domain(request, user)
    charge_domain = domain.value if domain else parse_charge_domain(business_domain)
    pm_id = int(payment_method_id)
    allowed = {int(m.id) for m in treasury_pay_methods(db, domain=domain)}
    if pm_id not in allowed:
        raise TreasuryDeskError(
            "هذه الخزينة لا تخص الوضع الحالي. بدّل وضع المطعم/الفندق من الشريط العلوي."
        )
    return pm_id, charge_domain


def _resolve_pay_category(db, *, kind: str, category_key: str, utility_key: str) -> str:
    from modules.payments.pay_categories import (
        KIND_OPERATING,
        KIND_UTILITY,
        category_by_key,
    )

    if kind == "other":
        return "أخرى"
    key = (utility_key if kind == "utility" else category_key) or ""
    row = category_by_key(db, key)
    if row is None or not row.get("active"):
        raise TreasuryDeskError(
            "اختر البند من القائمة. إن لم توجد بنود فالأدمن يضيفها من إدارة بنود الصرف."
        )
    want = KIND_UTILITY if kind == "utility" else KIND_OPERATING
    if row.get("kind") != want:
        raise TreasuryDeskError("البند المختار لا يخص نوع الصرف الحالي.")
    return str(row.get("label") or "").strip() or ("فاتورة خدمات" if kind == "utility" else "مصروف تشغيلي")


def _pay_salary(
    db,
    *,
    employee_id: str,
    salary_kind: str,
    pm_id: int,
    amount: Decimal,
    note: str,
    user_id: int,
    charge_domain: str,
    reference: str,
):
    from modules.hr.service import (
        HRError,
        apply_advance_deductions,
        apply_employee_deductions,
        get_employee,
        grant_advance,
        salary_payout_plan,
    )
    from modules.payments.pay_categories import (
        SALARY_CATEGORY_BONUS,
        SALARY_CATEGORY_FULL,
        SALARY_CATEGORY_OTHER,
        ensure_pay_category_gl_maps,
    )

    try:
        eid = int((employee_id or "").strip() or 0)
    except ValueError:
        eid = 0
    emp = get_employee(db, eid) if eid else None
    if emp is None:
        raise TreasuryDeskError("اختر الموظف الذي سيستلم المبلغ.")
    kind = (salary_kind or "").strip().lower()
    if kind == "full":
        plan = salary_payout_plan(db, emp.id)
        cash = plan["net"]
        parts = [f"راتب {emp.full_name_ar}"]
        if plan["deductions"] > 0:
            parts.append(f"خصم {plan['deductions']}")
        if plan["advances"] > 0:
            parts.append(f"سلفة {plan['advances']}")
        parts.append(f"صافي {cash}")
        extra = " — ".join(parts)
        if plan["deductions"] > 0:
            apply_employee_deductions(
                db,
                employee_id=emp.id,
                amount=plan["deductions"],
                payroll_entry_id=None,
            )
        if plan["advances"] > 0:
            apply_advance_deductions(
                db,
                employee_id=emp.id,
                amount=plan["advances"],
                payroll_entry_id=None,
            )
        if cash <= 0:
            if plan["deductions"] <= 0 and plan["advances"] <= 0:
                raise TreasuryDeskError("لا يوجد راتب أساسي لهذا الموظف.")
            return
        ensure_pay_category_gl_maps(db)
        record_expense(
            db,
            payment_method_id=pm_id,
            amount=cash,
            expense_category=SALARY_CATEGORY_FULL,
            supplier=emp.full_name_ar,
            note=(note.strip() + " — " if note.strip() else "") + extra,
            user_id=user_id,
            supplier_invoice_ref=(reference or "").strip() or None,
            business_domain=charge_domain,
        )
        return
    if kind == "advance":
        grant_advance(
            db,
            employee_id=emp.id,
            amount=amount,
            payment_method_id=pm_id,
            record_as_expense=True,
            notes=note or reference,
            given_by_id=user_id,
            business_domain=charge_domain,
        )
        return
    cats = {
        "full": SALARY_CATEGORY_FULL,
        "bonus": SALARY_CATEGORY_BONUS,
        "other": SALARY_CATEGORY_OTHER,
    }
    cat = cats.get(kind)
    if not cat:
        raise TreasuryDeskError("حدد نوع الصرف: راتب كامل أو سلفة أو مكافأة أو صرف آخر.")
    ensure_pay_category_gl_maps(db)
    extra = (note or "").strip() or f"{cat} — {emp.full_name_ar} — {amount} د.ل"
    if emp.full_name_ar not in extra:
        extra = f"{emp.full_name_ar} — {extra}"
    record_expense(
        db,
        payment_method_id=pm_id,
        amount=amount,
        expense_category=cat,
        supplier=emp.full_name_ar,
        note=extra,
        user_id=user_id,
        supplier_invoice_ref=(reference or "").strip() or None,
        business_domain=charge_domain,
    )


def _pay_supplier_invoice(
    db,
    *,
    purchase_id: str,
    pm_id: int,
    amount: Decimal,
    note: str,
    user_id: int,
    supplier_name: str,
):
    from modules.payments.models import Purchase, PurchaseKind

    try:
        pid = int((purchase_id or "").strip() or 0)
    except ValueError:
        pid = 0
    if pid <= 0:
        raise TreasuryDeskError("اختر الفاتورة المراد سدادها من قائمة فواتير المورد.")
    p = db.get(Purchase, pid)
    if p is None or p.kind != PurchaseKind.INVENTORY:
        raise TreasuryDeskError("فاتورة الشراء غير موجودة.")
    wanted = (supplier_name or "").strip()
    if wanted and (p.supplier or "").strip() != wanted:
        raise TreasuryDeskError("الفاتورة لا تخص المورد المختار.")
    record_purchase_payment(
        db,
        purchase_id=pid,
        payment_method_id=pm_id,
        amount=amount,
        user_id=user_id,
        note=note,
    )


@router.post("/pay", response_class=HTMLResponse)
def treasury_pay_submit(
    request: Request,
    db: DBSession,
    user: User = Depends(_act),
    pay_type: str = Form(...),
    payment_method_id: str = Form(...),
    amount: str = Form(...),
    beneficiary: str = Form(""),
    reference: str = Form(""),
    note: str = Form(""),
    business_domain: str = Form(""),
    category_key: str = Form(""),
    utility_key: str = Form(""),
    employee_id: str = Form(""),
    salary_kind: str = Form(""),
    supplier_name: str = Form(""),
    purchase_id: str = Form(""),
):
    kind = (pay_type or "").strip()
    if kind == "advance":
        return RedirectResponse("/pos/treasury/custody", 302)
    try:
        pm_id, charge_domain = _require_pay_wallet(
            db, user, request, payment_method_id, business_domain
        )
        amt = _money(amount)
        if kind == "salary":
            from modules.hr.service import HRError

            try:
                _pay_salary(
                    db,
                    employee_id=employee_id,
                    salary_kind=salary_kind,
                    pm_id=pm_id,
                    amount=amt,
                    note=note,
                    user_id=user.id,
                    charge_domain=charge_domain,
                    reference=reference,
                )
            except HRError as exc:
                raise TreasuryDeskError(str(exc)) from exc
        elif kind in ("purchase", "supplier"):
            if kind == "supplier" and not (supplier_name or "").strip():
                raise TreasuryDeskError("اختر المورد ثم الفاتورة المراد سدادها.")
            _pay_supplier_invoice(
                db,
                purchase_id=purchase_id,
                pm_id=pm_id,
                amount=amt,
                note=note,
                user_id=user.id,
                supplier_name=supplier_name if kind == "supplier" else "",
            )
        elif kind in ("operating", "utility", "other"):
            cat = _resolve_pay_category(
                db, kind=kind, category_key=category_key, utility_key=utility_key
            )
            ben = (beneficiary or "").strip()
            reason = (note or "").strip()
            if not ben:
                raise TreasuryDeskError("أدخل اسم المستلم للمبلغ قبل تنفيذ الصرف الخارجي.")
            if not reason:
                raise TreasuryDeskError("أدخل سبب الصرف في الملاحظات قبل تنفيذ الصرف الخارجي.")
            record_expense(
                db,
                payment_method_id=pm_id,
                amount=amt,
                expense_category=cat,
                supplier=ben,
                note=reason,
                user_id=user.id,
                supplier_invoice_ref=(reference or "").strip() or None,
                business_domain=charge_domain,
            )
        else:
            raise TreasuryDeskError("نوع الصرف غير معروف.")
        db.commit()
    except (ValueError, InvalidOperation, PaymentsError) as exc:
        db.rollback()
        msg = "بيانات غير صالحة." if isinstance(exc, (ValueError, InvalidOperation)) else str(exc)
        return _err("/pos/treasury/pay", msg)
    return RedirectResponse("/pos/treasury/pay?ok=1", 302)


@router.get("/session", response_class=HTMLResponse)
def treasury_session_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_view),
):
    session = get_open_treasury_session(db)
    bals = load_treasury_balances(db)
    mov = session_movements(db, session.opened_at) if session else today_movement(db)
    return templates.TemplateResponse(
        "treasury/session.html",
        {
            "request": request,
            "user": user,
            "session": session,
            "last_session": get_last_closed_treasury_session(db),
            "bals": bals,
            "movement": mov,
            "err": request.query_params.get("err"),
            "ok": request.query_params.get("ok"),
        },
    )


@router.post("/session/open", response_class=HTMLResponse)
def treasury_session_open(
    db: DBSession,
    user: User = Depends(_act),
):
    try:
        open_treasury_session(db, user_id=user.id)
        db.commit()
    except TreasuryDeskError as exc:
        db.rollback()
        return _err("/pos/treasury/session", str(exc))
    return RedirectResponse("/pos/treasury/desk?ok=opened", 302)


@router.post("/session/close", response_class=HTMLResponse)
def treasury_session_close(
    db: DBSession,
    user: User = Depends(_act),
    counted_cash: str = Form(...),
    counted_bank: str = Form(...),
    close_note: str = Form(""),
):
    if len((close_note or "").strip()) < 2:
        return _err("/pos/treasury/session", "أدخل بيان الإقفال — الوصف إلزامي.")
    try:
        close_treasury_session(
            db,
            user_id=user.id,
            counted_cash=_money(counted_cash),
            counted_bank=_money(counted_bank),
            note=close_note,
        )
        db.commit()
    except (InvalidOperation, TreasuryDeskError) as exc:
        db.rollback()
        msg = "مبلغ غير صالح." if isinstance(exc, InvalidOperation) else str(exc)
        return _err("/pos/treasury/session", msg)
    return RedirectResponse("/pos/treasury/desk?ok=closed", 302)
