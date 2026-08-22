"""Customers admin pages + loyalty management."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_, select
from starlette.requests import Request

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import CUSTOMERS_MANAGE, CUSTOMERS_VIEW
from modules.customers.service import (
    CustomersError,
    adjust_points,
    adjust_wallet,
    archive_customer,
    create_customer,
    delete_customer,
    get_customer,
    list_customers,
    list_transactions,
    list_wallet_transactions,
    loyalty_settings,
    purge_customer,
    require_valid_phone,
    restore_customer,
    total_points_grand,
    update_customer,
)
from modules.customers.stats import customer_stats, list_customer_sales
from modules.customers.import_handlers import handle_customers_import
router = APIRouter(prefix="/admin/customers", tags=["customers"])
_view = require_permission(CUSTOMERS_VIEW)
_manage = require_permission(CUSTOMERS_MANAGE)


@router.get("", response_class=HTMLResponse)
def customers_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_view),
):
    import logging

    from modules.customers.service import resolve_customer_list_domain
    from modules.platform.business_domain import is_system_admin

    log = logging.getLogger("pos.customers")
    search = (request.query_params.get("q") or "").strip()
    show_inactive = request.query_params.get("show_inactive") == "1"
    customer_type = (request.query_params.get("type") or "").strip() or None
    domain_raw = (request.query_params.get("domain") or "").strip() or None
    list_domain = resolve_customer_list_domain(
        user, request.session, explicit=domain_raw if domain_raw != "all" else None
    )
    # أدمن في الوضع العام: domain=all أو بدون فلتر = الكل
    if domain_raw == "all" and is_system_admin(user):
        list_domain = None
    try:
        # إصلاح ربط حجوزات الفندق ← قائمة العملاء (مجال/شركة)
        if list_domain is not None and getattr(list_domain, "value", None) == "hotel":
            from modules.customers.service import backfill_hotel_customers_from_bookings

            try:
                n = backfill_hotel_customers_from_bookings(db, limit=300)
                if n:
                    db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
                log.exception("hotel customers backfill failed")
        customers = list_customers(
            db,
            search=search or None,
            only_active=not show_inactive,
            customer_type=customer_type,
            business_domain=list_domain,
        )
        settings = loyalty_settings(db)
        grand = total_points_grand(
            db,
            business_domain=list_domain,
            only_active=not show_inactive,
        )
        customer_rows = [(c, customer_stats(db, c)) for c in customers]
    except Exception as exc:
        log.exception("customers page failed: %s", exc)
        return templates.TemplateResponse(
            "admin_customers.html",
            {
                "request": request,
                "customers": [],
                "customer_rows": [],
                "search": search,
                "customer_type": customer_type or "",
                "domain_filter": domain_raw or "",
                "list_domain": list_domain,
                "can_pick_domain": is_system_admin(user),
                "loyalty": {
                    "enabled": True,
                    "earn_per_dinar": Decimal("1"),
                    "redeem_value_per_point": Decimal("0.1"),
                    "min_points_to_redeem": Decimal("50"),
                },
                "grand_points": Decimal("0"),
                "saved": request.query_params.get("saved"),
                "error": (
                    "تعذّر تحميل العملاء. أعد تشغيل التطبيق لترقية قاعدة البيانات "
                    "أو راجع سجل السيرفر."
                ),
            },
        )
    return templates.TemplateResponse(
        "admin_customers.html",
        {
            "request": request,
            "customers": customers,
            "customer_rows": customer_rows,
            "search": search,
            "customer_type": customer_type or "",
            "domain_filter": domain_raw or (list_domain.value if list_domain else "all"),
            "list_domain": list_domain,
            "can_pick_domain": is_system_admin(user),
            "loyalty": settings,
            "grand_points": grand,
            "show_inactive": show_inactive,
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/add", response_class=HTMLResponse)
def customers_add(
    request: Request,
    db: DBSession,
    user: User = Depends(_manage),
    phone: str = Form(...),
    name: str = Form(""),
    email: str = Form(""),
    notes: str = Form(""),
    customer_type: str = Form("INDIVIDUAL"),
    company_name: str = Form(""),
    business_domain: str = Form(""),
):
    from modules.customers.service import resolve_customer_list_domain

    try:
        dom = (business_domain or "").strip() or None
        if not dom:
            inferred = resolve_customer_list_domain(user, request.session)
            dom = inferred.value if inferred else "restaurant"
        create_customer(
            db,
            phone=phone,
            name=name,
            email=email,
            notes=notes,
            customer_type=customer_type,
            company_name=company_name or None,
            business_domain=dom,
        )
        db.commit()
    except CustomersError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/customers?error={e}", status_code=302
        )
    return RedirectResponse("/admin/customers?saved=1", status_code=302)


@router.post("/import")
async def customers_import_csv(
    db: DBSession,
    user: User = Depends(_manage),
    file: UploadFile = File(...),
    update_existing: str = Form("on"),
    create_missing: str = Form("on"),
    restore_points: str = Form("on"),
    restore_wallet: str = Form("on"),
    restore_referral: str = Form("on"),
    return_to: str = Form("/admin/customers"),
):
    return await handle_customers_import(
        db,
        user,
        file=file,
        update_existing=update_existing,
        create_missing=create_missing,
        restore_points=restore_points,
        restore_wallet=restore_wallet,
        restore_referral=restore_referral,
        return_to=return_to,
    )


@router.post("/{cid}/edit", response_class=HTMLResponse)
def customers_edit(
    request: Request,
    cid: int,
    db: DBSession,
    user: User = Depends(_manage),
    phone: str = Form(...),
    name: str = Form(""),
    email: str = Form(""),
    notes: str = Form(""),
    is_active: str = Form(""),
    customer_type: str = Form("INDIVIDUAL"),
    company_name: str = Form(""),
    business_domain: str = Form(""),
    parent_company_id: str = Form(""),
    company_discount_percent: str = Form("0"),
    company_credit_limit: str = Form("0"),
    allow_company_credit: str = Form(""),
    company_notify_frequency: str = Form("DAILY"),
    company_default_notify_to: str = Form("COMPANY"),
):
    from modules.customers.service import (
        customer_visible_for_domain,
        get_customer,
        resolve_customer_list_domain,
    )
    from modules.platform.business_domain import is_system_admin

    c = get_customer(db, cid)
    if c is None:
        return RedirectResponse(
            "/admin/customers?error=العميل غير موجود.", status_code=302
        )
    list_domain = resolve_customer_list_domain(user, request.session)
    if list_domain and not customer_visible_for_domain(
        c.business_domain, filter_domain=list_domain
    ):
        return RedirectResponse(
            "/admin/customers?error=لا صلاحية لتعديل عميل خارج نطاقك.",
            status_code=302,
        )
    # موظف محدود المجال: لا يغيّر المجال يدوياً
    dom = (business_domain or "").strip() or None
    if not is_system_admin(user):
        dom = None
    parent_raw = (parent_company_id or "").strip()
    parent_id = int(parent_raw) if parent_raw.isdigit() else None
    try:
        update_customer(
            db,
            cid,
            phone=phone,
            name=name,
            email=email,
            notes=notes,
            is_active=(is_active == "on"),
            customer_type=customer_type,
            company_name=company_name or None,
            business_domain=dom,
            parent_company_id=parent_id,
            company_discount_percent=company_discount_percent or "0",
            company_credit_limit=company_credit_limit or "0",
            allow_company_credit=(allow_company_credit == "on"),
            company_notify_frequency=company_notify_frequency or "DAILY",
            company_default_notify_to=company_default_notify_to or "COMPANY",
        )
        db.commit()
    except CustomersError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/customers/{cid}?error={e}", status_code=302
        )
    return RedirectResponse(f"/admin/customers/{cid}?saved=1", status_code=302)


@router.post("/{cid}/archive", response_class=HTMLResponse)
def customers_archive(
    cid: int,
    db: DBSession,
    _: User = Depends(_manage),
):
    try:
        archive_customer(db, cid)
        db.commit()
    except CustomersError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/customers?error={e}", status_code=302
        )
    return RedirectResponse("/admin/customers?saved=archived", status_code=302)


@router.post("/{cid}/restore", response_class=HTMLResponse)
def customers_restore(
    cid: int,
    db: DBSession,
    _: User = Depends(_manage),
):
    try:
        restore_customer(db, cid)
        db.commit()
    except CustomersError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/customers?error={e}", status_code=302
        )
    return RedirectResponse("/admin/customers?saved=restored", status_code=302)


@router.post("/{cid}/purge", response_class=HTMLResponse)
def customers_purge(
    cid: int,
    db: DBSession,
    _: User = Depends(_manage),
    confirm_phone: str = Form(""),
):
    c = get_customer(db, cid)
    if c is None:
        return RedirectResponse(
            "/admin/customers?error=العميل غير موجود.", status_code=302
        )
    try:
        typed = require_valid_phone(confirm_phone)
        if typed != require_valid_phone(c.phone):
            raise CustomersError(
                "رقم التأكيد لا يطابق هاتف العميل. اكتب نفس الرقم للحذف النهائي."
            )
        purge_customer(db, cid)
        db.commit()
    except CustomersError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/customers/{cid}?error={e}", status_code=302
        )
    return RedirectResponse("/admin/customers?saved=purged", status_code=302)


@router.post("/{cid}/delete", response_class=HTMLResponse)
def customers_delete(
    cid: int,
    db: DBSession,
    _: User = Depends(_manage),
):
    try:
        delete_customer(db, cid)
        db.commit()
    except CustomersError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/customers?error={e}", status_code=302
        )
    return RedirectResponse("/admin/customers?saved=deleted", status_code=302)


@router.post("/{cid}/referral-code", response_class=HTMLResponse)
def customer_set_referral_code(
    cid: int,
    db: DBSession,
    _: User = Depends(_manage),
    referral_code: str = Form(...),
):
    from modules.customers.referral_service import ReferralError, set_customer_referral_code

    try:
        set_customer_referral_code(db, cid, referral_code)
        db.commit()
    except ReferralError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/customers/{cid}?error={quote(str(e))}", status_code=302
        )
    return RedirectResponse(
        f"/admin/customers/{cid}?saved=referral", status_code=302
    )


@router.get("/{cid:int}", response_class=HTMLResponse)
def customer_detail(
    request: Request,
    cid: int,
    db: DBSession,
    user: User = Depends(_view),
):
    from modules.customers.service import (
        customer_visible_for_domain,
        company_cash_movements,
        is_company_customer,
        resolve_customer_list_domain,
    )
    from modules.platform.business_domain import is_system_admin

    c = get_customer(db, cid)
    if c is None:
        return RedirectResponse(
            "/admin/customers?error=العميل غير موجود.", status_code=302
        )
    list_domain = resolve_customer_list_domain(user, request.session)
    if list_domain and not customer_visible_for_domain(
        c.business_domain, filter_domain=list_domain
    ):
        return RedirectResponse(
            "/admin/customers?error=هذا العميل خارج نطاق عرضك (مطعم/فندق).",
            status_code=302,
        )
    txns = list_transactions(db, cid, limit=200)
    sales = list_customer_sales(db, cid, limit=30)
    settings = loyalty_settings(db)
    from modules.customers.referral_service import (
        ensure_six_digit_referral_code,
        referral_code_is_locked,
        referral_code_usage_count,
    )

    ensure_six_digit_referral_code(db, c)
    db.commit()
    db.refresh(c)
    from modules.customers.account_balance import (
        customer_money_balance,
        list_booking_folio_credits,
    )
    from modules.messaging.consent import get_profile
    from modules.messaging.service import messaging_enabled

    msg_profile = get_profile(db, cid)
    money_balance = customer_money_balance(db, c)
    booking_credits = (
        list_booking_folio_credits(db, c) if is_company_customer(c) else []
    )
    stats = customer_stats(db, c)
    wallet_txns = list_wallet_transactions(db, cid, limit=100)
    company_moves = {"receipts": [], "expenses": []}
    if is_company_customer(c):
        company_moves = company_cash_movements(db, cid, limit=200)
    from modules.customers.models import Customer, CustomerType

    company_accounts = list(
        db.scalars(
            select(Customer)
            .where(
                Customer.customer_type == CustomerType.COMPANY,
                Customer.is_active.is_(True),
                Customer.id != int(cid),
            )
            .order_by(Customer.company_name, Customer.name)
            .limit(300)
        ).all()
    )
    company_agreement = None
    service_catalog = []
    agreement_change_pending = []
    agreement_change_history = []
    if c.customer_type == CustomerType.COMPANY:
        try:
            from modules.hotel.company_agreement_models import SERVICE_CATALOG
            from modules.hotel.company_agreement_service import (
                ensure_default_agreement,
                list_agreement_change_requests,
                request_items,
            )
            from modules.hotel.uploads import agreement_request_public_url

            company_agreement = ensure_default_agreement(db, int(c.id))
            db.commit()
            service_catalog = SERVICE_CATALOG

            def _pack_change_req(row):
                return {
                    "id": row.id,
                    "status": (row.status or "").upper(),
                    "note": row.note,
                    "attachment_url": agreement_request_public_url(row.attachment_path),
                    "attachment_name": row.attachment_name,
                    "items": request_items(row),
                    "created_at": row.created_at,
                    "review_note": row.review_note,
                    "booking_id": row.booking_id,
                }

            agreement_change_pending = [
                _pack_change_req(r)
                for r in list_agreement_change_requests(
                    db, int(c.id), status="PENDING", limit=30
                )
            ]
            agreement_change_history = [
                _pack_change_req(r)
                for r in list_agreement_change_requests(
                    db, int(c.id), status=None, limit=20
                )
                if (r.status or "").upper() != "PENDING"
            ]
        except Exception:  # noqa: BLE001
            db.rollback()
            company_agreement = None
    return templates.TemplateResponse(
        "admin_customer_detail.html",
        {
            "request": request,
            "customer": c,
            "stats": stats,
            "money_balance": money_balance,
            "transactions": txns,
            "wallet_transactions": wallet_txns,
            "company_receipts": company_moves.get("receipts") or [],
            "company_expenses": company_moves.get("expenses") or [],
            "company_wallet_ledger": company_moves.get("ledger") or [],
            "booking_credits": booking_credits,
            "sales": sales,
            "loyalty": settings,
            "referral_locked": referral_code_is_locked(db, c),
            "referral_uses": referral_code_usage_count(db, c.id),
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
            "msg_sent": request.query_params.get("msg_sent"),
            "messaging_enabled": messaging_enabled(db),
            "company_agreement": company_agreement,
            "service_catalog": service_catalog,
            "agreement_change_pending": agreement_change_pending,
            "agreement_change_history": agreement_change_history,
            "msg_opt_in": bool(msg_profile and msg_profile.opt_in),
            "msg_channel": msg_profile.preferred_channel if msg_profile else "whatsapp",
            "can_pick_domain": is_system_admin(user),
            "company_accounts": company_accounts,
        },
    )


@router.get("/{cid}/wallet-txn/{tid}/print", response_class=HTMLResponse)
def customer_wallet_txn_print(
    request: Request,
    cid: int,
    tid: int,
    db: DBSession,
    _: User = Depends(_view),
):
    from modules.customers.models import CustomerWalletTransaction

    c = get_customer(db, cid)
    txn = db.get(CustomerWalletTransaction, tid)
    if c is None or txn is None or int(txn.customer_id) != int(cid):
        return RedirectResponse(
            f"/admin/customers/{cid}?error=الحركة غير موجودة.",
            status_code=302,
        )
    amt = Decimal(str(txn.amount or 0))
    kind_ar = "إيداع" if amt > 0 else ("صرف" if amt < 0 else "تعديل")
    from modules.customers.service import _staff_label

    return templates.TemplateResponse(
        "admin_customer_wallet_print.html",
        {
            "request": request,
            "customer": c,
            "txn": txn,
            "kind_ar": kind_ar,
            "employee": _staff_label(db, user_id=txn.created_by_id),
            "amount_abs": abs(amt),
        },
    )


@router.post("/{cid}/company-agreement", response_class=HTMLResponse)
async def customer_company_agreement_save(
    request: Request,
    cid: int,
    db: DBSession,
    user: User = Depends(_manage),
):
    """حفظ عقد الشركة وجدول «من يتحمّل كل بند»."""
    from modules.customers.models import CustomerType
    from modules.hotel.company_agreement_models import SERVICE_CATALOG
    from modules.hotel.company_agreement_service import (
        AgreementError,
        ensure_default_agreement,
        refresh_open_booking_rules_for_company,
        save_agreement_services_bulk,
        update_agreement,
    )

    c = get_customer(db, cid)
    if c is None or c.customer_type != CustomerType.COMPANY:
        return RedirectResponse(
            f"/admin/customers/{cid}?error=العقد يخص حسابات الشركات فقط.",
            status_code=302,
        )
    form = await request.form()
    try:
        agreement = ensure_default_agreement(db, int(cid))
        update_agreement(
            db,
            agreement.id,
            name=str(form.get("agreement_name") or "العقد الافتراضي"),
            agreement_type=str(form.get("agreement_type") or "STANDARD"),
            payment_terms=str(form.get("payment_terms") or ""),
            commission_percent=str(form.get("commission_percent") or "0"),
            commission_fixed=str(form.get("commission_fixed") or "0"),
            notes=str(form.get("agreement_notes") or ""),
            is_active=True,
        )
        rows = []
        for code, default_name, _b in SERVICE_CATALOG:
            bearer = str(form.get(f"bearer_{code}") or "GUEST")
            lim = str(form.get(f"limit_{code}") or "").strip()
            active = str(form.get(f"active_{code}") or "") == "on"
            rows.append(
                {
                    "service_code": code,
                    "name_ar": default_name,
                    "bearer": bearer,
                    "limit_amount": lim if lim else None,
                    "limit_quantity": None,
                    "is_active": active if form.get(f"active_{code}") is not None else True,
                }
            )
            # إن لم يُرسل checkbox active — اعتبره مفعّلاً إن وُجد bearer
            if f"active_{code}" not in form:
                rows[-1]["is_active"] = True
        save_agreement_services_bulk(db, agreement.id, rows)
        refresh_open_booking_rules_for_company(db, int(cid))
        db.commit()
    except (AgreementError, Exception) as exc:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(
            f"/admin/customers/{cid}?error={quote(str(exc)[:180])}",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/customers/{cid}?saved=1#company-agreement",
        status_code=302,
    )


@router.post("/{cid}/company-agreement/requests/{rid}/apply", response_class=HTMLResponse)
def customer_company_agreement_request_apply(
    cid: int,
    rid: int,
    db: DBSession,
    user: User = Depends(_manage),
    review_note: str = Form(""),
):
    from modules.hotel.company_agreement_service import (
        AgreementError,
        apply_agreement_change_request,
    )

    try:
        from modules.hotel.company_agreement_models import CompanyAgreementChangeRequest

        row = db.get(CompanyAgreementChangeRequest, int(rid))
        if row is None or int(row.company_customer_id) != int(cid):
            raise AgreementError("طلب التعديل لا يخص هذه الشركة.")
        apply_agreement_change_request(
            db, int(rid), user_id=user.id, review_note=review_note
        )
        db.commit()
    except AgreementError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/customers/{cid}?error={quote(str(exc)[:180])}#company-agreement",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/customers/{cid}?saved=agreement_applied#company-agreement",
        status_code=302,
    )


@router.post("/{cid}/company-agreement/requests/{rid}/reject", response_class=HTMLResponse)
def customer_company_agreement_request_reject(
    cid: int,
    rid: int,
    db: DBSession,
    user: User = Depends(_manage),
    review_note: str = Form(""),
):
    from modules.hotel.company_agreement_service import (
        AgreementError,
        reject_agreement_change_request,
    )

    try:
        from modules.hotel.company_agreement_models import CompanyAgreementChangeRequest

        row = db.get(CompanyAgreementChangeRequest, int(rid))
        if row is None or int(row.company_customer_id) != int(cid):
            raise AgreementError("طلب التعديل لا يخص هذه الشركة.")
        reject_agreement_change_request(
            db, int(rid), user_id=user.id, review_note=review_note
        )
        db.commit()
    except AgreementError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/customers/{cid}?error={quote(str(exc)[:180])}#company-agreement",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/customers/{cid}?saved=agreement_rejected#company-agreement",
        status_code=302,
    )


@router.post("/{cid}/post-hotel-credits", response_class=HTMLResponse)
def customer_post_hotel_credits(
    cid: int,
    db: DBSession,
    user: User = Depends(_manage),
    booking_id: str = Form(""),
):
    """ترحيل يدوي لأرصدة الحجوزات المغلقة إلى محفظة هذا الحساب."""
    from modules.customers.account_balance import _customer_booking_match_clauses
    from modules.hotel.booking_models import BookingStatus, HotelBooking
    from modules.hotel.booking_service import (
        consolidate_checked_out_credits_to_wallet,
        transfer_booking_overpay_to_customer_wallet,
    )

    c = get_customer(db, cid)
    if c is None:
        return RedirectResponse(
            "/admin/customers?error=العميل غير موجود.", status_code=302
        )
    try:
        moved = Decimal("0")
        bid_raw = (booking_id or "").strip()
        if bid_raw.isdigit():
            booking = db.get(HotelBooking, int(bid_raw))
            ids = set(
                int(i)
                for i in db.scalars(
                    select(HotelBooking.id).where(
                        or_(*_customer_booking_match_clauses(c))
                    )
                ).all()
            )
            if booking is None or int(booking.id) not in ids:
                raise CustomersError("الحجز لا يخص هذا الحساب.")
            if booking.booking_status != BookingStatus.CHECKED_OUT:
                raise CustomersError(
                    "لا يُرحَّل رصيد حجز مفتوح — يبقى على كشف الحجز حتى المغادرة."
                )
            moved = transfer_booking_overpay_to_customer_wallet(
                db,
                booking,
                user_id=user.id,
                wallet_customer_id=int(cid),
                note=f"ترحيل يدوي لرصيد حجز {booking.reference} إلى محفظة #{cid}",
            )
        else:
            moved = consolidate_checked_out_credits_to_wallet(
                db, int(cid), user_id=user.id
            )
        db.commit()
    except CustomersError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/customers/{cid}?error={quote(str(exc)[:180])}#booking-credits",
            status_code=302,
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(
            f"/admin/customers/{cid}?error={quote(str(exc)[:180])}#booking-credits",
            status_code=302,
        )
    if moved <= 0:
        return RedirectResponse(
            f"/admin/customers/{cid}?error={quote('لا يوجد رصيد مغلق للترحيل. إن ظهر المبلغ فهو على حجز ما زال مفتوحاً.')}#booking-credits",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/customers/{cid}?saved=credits_posted#booking-credits",
        status_code=302,
    )


@router.post("/{cid}/adjust-points", response_class=HTMLResponse)
def customer_adjust_points(
    cid: int,
    db: DBSession,
    user: User = Depends(_manage),
    delta: str = Form(...),
    note: str = Form(""),
):
    try:
        d = Decimal(delta.strip())
    except (InvalidOperation, ValueError):
        return RedirectResponse(
            f"/admin/customers/{cid}?error=قيمة غير صحيحة.", status_code=302
        )
    try:
        adjust_points(db, customer_id=cid, delta_points=d, note=note, user_id=user.id)
        db.commit()
        from modules.customers.service import get_customer
        from modules.messaging.service import notify_loyalty_adjusted

        customer = get_customer(db, cid)
        if customer is not None:
            notify_loyalty_adjusted(
                db, customer=customer, delta=d, note=note or None
            )
    except CustomersError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/customers/{cid}?error={e}", status_code=302
        )
    return RedirectResponse(f"/admin/customers/{cid}?saved=1", status_code=302)


@router.post("/{cid}/adjust-wallet", response_class=HTMLResponse)
def customer_adjust_wallet(
    cid: int,
    db: DBSession,
    user: User = Depends(_manage),
    amount: str = Form(...),
    note: str = Form(""),
):
    try:
        amt = Decimal(amount.strip())
    except (InvalidOperation, ValueError):
        return RedirectResponse(
            f"/admin/customers/{cid}?error=قيمة غير صحيحة.", status_code=302
        )
    try:
        adjust_wallet(db, customer_id=cid, amount=amt, note=note, user_id=user.id)
        # بعد شحن المحفظة: خصم تلقائي لتغطية ديون الحجوزات النشطة
        if amt > 0:
            try:
                from modules.hotel.booking_service import apply_wallet_to_cover_booking_dues

                apply_wallet_to_cover_booking_dues(
                    db, customer_id=cid, user_id=user.id
                )
            except Exception:  # noqa: BLE001
                pass
        db.commit()
    except CustomersError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/customers/{cid}?error={e}", status_code=302
        )
    return RedirectResponse(f"/admin/customers/{cid}?saved=wallet", status_code=302)


@router.post("/{cid}/send-message", response_class=HTMLResponse)
def customer_send_message(
    cid: int,
    db: DBSession,
    user: User = Depends(_manage),
    message: str = Form(...),
    image_url: str = Form(""),
    force_send: str = Form(""),
):
    from modules.messaging.service import MessagingError, send_message_to_customer

    try:
        send_message_to_customer(
            db,
            customer_id=cid,
            message=message,
            image_url=image_url,
            force=force_send == "on",
            user_id=user.id,
        )
        db.commit()
        return RedirectResponse(f"/admin/customers/{cid}?msg_sent=1", status_code=302)
    except MessagingError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/customers/{cid}?error={quote(str(e))}",
            status_code=302,
        )
    except Exception as e:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(
            f"/admin/customers/{cid}?error={quote(str(e))}",
            status_code=302,
        )


# ============================================================
# Loyalty settings page
# ============================================================
loyalty_router = APIRouter(prefix="/admin/loyalty", tags=["loyalty"])
_loyalty_admin = require_permission(CUSTOMERS_MANAGE)


@loyalty_router.get("", response_class=HTMLResponse)
def loyalty_settings_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_loyalty_admin),
):
    from modules.customers.referral_service import referral_settings
    from modules.platform.business_domain import BusinessDomain

    from modules.messaging.hermes_loyalty import get_loyalty_intro_template

    s = referral_settings(db, BusinessDomain.RESTAURANT)
    return templates.TemplateResponse(
        "admin_loyalty_settings.html",
        {
            "request": request,
            "loyalty": s,
            "domain_label": "المطعم",
            "show_loyalty_intro_editor": True,
            "loyalty_intro_text": get_loyalty_intro_template(db),
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@loyalty_router.post("/save", response_class=HTMLResponse)
def loyalty_settings_save(
    db: DBSession,
    _: User = Depends(_loyalty_admin),
    enabled: str = Form(""),
    earn_per_dinar: str = Form("1"),
    redeem_value_per_point: str = Form("0.1"),
    min_points_to_redeem: str = Form("50"),
    loyalty_max_redeem_percent: str = Form(""),
    referral_enabled: str = Form(""),
    referral_referrer_points: str = Form("50"),
    referral_buyer_points: str = Form("25"),
    referral_min_sale_total: str = Form("0"),
    referral_max_uses_per_code: str = Form("1"),
    loyalty_intro_text: str = Form(""),
):
    from modules.messaging.hermes_loyalty import save_loyalty_intro_template
    from modules.platform.business_domain import BusinessDomain
    from modules.platform.domain_loyalty import (
        save_loyalty_settings_for_domain,
        save_referral_settings_for_domain,
    )
    from modules.settings.service import invalidate_settings_cache

    save_loyalty_settings_for_domain(
        db,
        BusinessDomain.RESTAURANT,
        enabled=enabled == "on",
        earn_per_dinar=earn_per_dinar,
        redeem_value_per_point=redeem_value_per_point,
        min_points_to_redeem=min_points_to_redeem,
        max_redeem_percent=loyalty_max_redeem_percent,
    )
    save_referral_settings_for_domain(
        db,
        BusinessDomain.RESTAURANT,
        enabled=referral_enabled == "on",
        referrer_points=referral_referrer_points,
        buyer_points=referral_buyer_points,
        min_sale_total=referral_min_sale_total,
        max_uses_per_code=referral_max_uses_per_code,
    )
    save_loyalty_intro_template(db, loyalty_intro_text)
    invalidate_settings_cache()
    db.commit()
    return RedirectResponse("/admin/loyalty?saved=1", status_code=302)
