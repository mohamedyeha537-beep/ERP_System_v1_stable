from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from pathlib import Path

from starlette.datastructures import UploadFile
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from sqlalchemy import func, or_, select, update
from modules.inventory.models import StockMovementType
from modules.inventory.service import apply_movement
from modules.payments.models import (
    HOTEL_TREASURY_BANK_PM_NAME,
    HOTEL_TREASURY_CASH_PM_NAME,
    HOTEL_RECEPTION_BANK_PM_NAME,
    HOTEL_RECEPTION_CASH_PM_NAME,
    HOTEL_PURCHASE_CUSTODY_CASH_PM_NAME,
    HOTEL_PURCHASE_CUSTODY_BANK_PM_NAME,
    RESTAURANT_PURCHASE_CUSTODY_CASH_PM_NAME,
    RESTAURANT_PURCHASE_CUSTODY_BANK_PM_NAME,
    ROOM_SETTLE_CLEARING_PM_NAME,
    PURCHASE_CUSTODY_PM_NAMES,
    LEGACY_OWNER_CAPITAL_PM_NAME,
    LEGACY_OWNER_DRAW_PM_NAME,
    OWNER_EQUITY_PM_NAME,
    SUPPLIER_CREDIT_PM_NAME,
    PaymentMethod,
    PaymentMethodDomain,
    PaymentMethodKind,
    PaymentTransfer,
    PaymentTransferType,
    Purchase,
    PurchaseKind,
    PurchaseLine,
    PurchasePayment,
    RefundPayment,
    SalePayment,
)
from modules.sales.models import Sale, SaleStatus


class PaymentsError(Exception):
    pass


def _domain_value(value) -> str:
    return getattr(value, "value", value) or PaymentMethodDomain.SHARED.value


def payment_method_is_strict_domain(pm: PaymentMethod, domain: PaymentMethodDomain) -> bool:
    return str(_domain_value(getattr(pm, "business_domain", None))).strip().lower() == domain.value


@dataclass(frozen=True)
class InventoryPurchaseLineIn:
    product_id: int
    quantity: Decimal
    unit_cost: Decimal
    production_date: date | None = None
    expiry_date: date | None = None


@dataclass(frozen=True)
class MixedPurchaseLineIn:
    """بند في فاتورة شراء موحّدة: منتج مخزون أو أصل ثابت أو استهلاك."""

    kind: str  # PRODUCT | FIXED_ASSET | CONSUMABLE
    quantity: Decimal
    unit_cost: Decimal
    product_id: int | None = None
    item_name: str | None = None
    unit: str | None = None
    useful_life_months: int = 0
    salvage_value: Decimal = Decimal("0")
    production_date: date | None = None
    expiry_date: date | None = None


def _normalize_inventory_lines(
    lines: list[InventoryPurchaseLineIn]
    | list[tuple[int, Decimal, Decimal]],
) -> list[InventoryPurchaseLineIn]:
    out: list[InventoryPurchaseLineIn] = []
    for item in lines:
        if isinstance(item, InventoryPurchaseLineIn):
            out.append(item)
        else:
            pid, qty, cost = item
            out.append(InventoryPurchaseLineIn(pid, qty, cost))
    return out


def _normalize_mixed_lines(
    lines: list[MixedPurchaseLineIn] | list[InventoryPurchaseLineIn] | list[tuple],
) -> list[MixedPurchaseLineIn]:
    """يقبل خطوطاً مختلطة أو خطوط مخزون قديمة ويوحّدها إلى MixedPurchaseLineIn."""
    from modules.payments.models import PurchaseLineKind

    out: list[MixedPurchaseLineIn] = []
    for item in lines:
        if isinstance(item, MixedPurchaseLineIn):
            out.append(item)
        elif isinstance(item, InventoryPurchaseLineIn):
            out.append(
                MixedPurchaseLineIn(
                    kind=PurchaseLineKind.PRODUCT.value,
                    product_id=item.product_id,
                    quantity=item.quantity,
                    unit_cost=item.unit_cost,
                    production_date=item.production_date,
                    expiry_date=item.expiry_date,
                )
            )
        else:
            pid, qty, cost = item
            out.append(
                MixedPurchaseLineIn(
                    kind=PurchaseLineKind.PRODUCT.value,
                    product_id=int(pid),
                    quantity=qty,
                    unit_cost=cost,
                )
            )
    return out


def ensure_default_payment_methods(db: Session) -> None:
    """ينشئ أسلوب «كاش» إن لم يوجد أي أسلوب دفع."""
    existing = db.execute(select(PaymentMethod).limit(1)).scalar_one_or_none()
    if existing is not None:
        ensure_supplier_credit_payment_method(db)
        return
    db.add(
        PaymentMethod(
            name_ar="كاش",
            kind=PaymentMethodKind.CASH,
            is_active=True,
            sort_order=0,
            can_receive=True,
            can_pay=True,
            can_fund=False,
        )
    )
    db.commit()
    ensure_supplier_credit_payment_method(db)
    ensure_owner_equity_payment_method(db)


def ensure_hotel_treasury_payment_methods(db: Session) -> dict[str, PaymentMethod]:
    """خزينة الفندق النهائية (كاش/مصرف) — لا تقبل تحصيل مباشرة؛ فقط تحويل اعتماد الجلسة.

    التحصيل اليومي للاستقبال عبر ensure_hotel_reception_payment_methods.
    """
    ensure_hotel_reception_payment_methods(db)

    def _ensure(name: str, kind: PaymentMethodKind, sort_order: int) -> PaymentMethod:
        row = db.scalar(select(PaymentMethod).where(PaymentMethod.name_ar == name))
        if row is None:
            row = PaymentMethod(
                name_ar=name,
                kind=kind,
                is_active=True,
                sort_order=sort_order,
                # مثل الخزينة الرئيسية: لا قبض مباشرة من نقطة الاستقبال
                can_receive=False,
                can_pay=True,
                can_fund=True,
                is_system=True,
                show_on_dashboard=True,
                business_domain=PaymentMethodDomain.HOTEL,
            )
            db.add(row)
            db.flush()
        else:
            row.kind = kind
            row.is_active = True
            row.can_receive = False
            row.can_pay = True
            row.can_fund = True
            row.is_system = True
            row.show_on_dashboard = True
            row.business_domain = PaymentMethodDomain.HOTEL
            db.flush()
        return row

    out = {
        "CASH": _ensure(HOTEL_TREASURY_CASH_PM_NAME, PaymentMethodKind.CASH, 60),
        "BANK": _ensure(HOTEL_TREASURY_BANK_PM_NAME, PaymentMethodKind.BANK, 61),
    }
    try:
        from modules.hotel.shift_handoff import mark_legacy_hotel_shifts_handed_off

        mark_legacy_hotel_shifts_handed_off(db)
    except Exception:  # noqa: BLE001
        pass
    try:
        from modules.gl.seed import ensure_hotel_wallet_gl_maps

        ensure_hotel_wallet_gl_maps(db)
    except Exception:  # noqa: BLE001
        pass
    return out


def ensure_hotel_reception_payment_methods(db: Session) -> dict[str, PaymentMethod]:
    """محافظ تحصيل الاستقبال — تقبض من النزيل أثناء الجلسة (قبل اعتماد الخزينة)."""

    def _ensure(name: str, kind: PaymentMethodKind, sort_order: int) -> PaymentMethod:
        row = db.scalar(select(PaymentMethod).where(PaymentMethod.name_ar == name))
        if row is None:
            row = PaymentMethod(
                name_ar=name,
                kind=kind,
                is_active=True,
                sort_order=sort_order,
                can_receive=True,
                can_pay=True,
                can_fund=True,
                is_system=True,
                show_on_dashboard=True,
                business_domain=PaymentMethodDomain.HOTEL,
            )
            db.add(row)
            db.flush()
        else:
            row.kind = kind
            row.is_active = True
            row.can_receive = True
            row.can_pay = True
            row.can_fund = True
            row.is_system = True
            row.show_on_dashboard = True
            row.business_domain = PaymentMethodDomain.HOTEL
            db.flush()
        return row

    return {
        "CASH": _ensure(HOTEL_RECEPTION_CASH_PM_NAME, PaymentMethodKind.CASH, 58),
        "BANK": _ensure(HOTEL_RECEPTION_BANK_PM_NAME, PaymentMethodKind.BANK, 59),
    }


def ensure_supplier_credit_payment_method(db: Session) -> PaymentMethod:
    """محفظة افتراضية لفواتير الشراء الآجلة (ذمم دائن للمورد)."""
    row = db.execute(
        select(PaymentMethod).where(PaymentMethod.name_ar == SUPPLIER_CREDIT_PM_NAME)
    ).scalar_one_or_none()
    if row is not None:
        row.can_receive = False
        row.can_pay = False
        row.can_fund = False
        row.is_system = True
        row.show_on_dashboard = False
        db.flush()
        return row
    pm = PaymentMethod(
        name_ar=SUPPLIER_CREDIT_PM_NAME,
        kind=PaymentMethodKind.OTHER,
        is_active=True,
        sort_order=900,
        can_receive=False,
        can_pay=False,
        can_fund=False,
        is_system=True,
        show_on_dashboard=False,
    )
    db.add(pm)
    db.flush()
    return pm


def _legacy_owner_equity_names() -> tuple[str, ...]:
    return (LEGACY_OWNER_DRAW_PM_NAME, LEGACY_OWNER_CAPITAL_PM_NAME)


def ensure_owner_equity_payment_method(db: Session) -> PaymentMethod:
    """حساب نظامي واحد لحركات المالك (إيداعات وسحوبات) — حقوق ملكية وليست إيراداً أو مصروفاً."""
    unified = db.execute(
        select(PaymentMethod).where(PaymentMethod.name_ar == OWNER_EQUITY_PM_NAME)
    ).scalar_one_or_none()
    legacy_rows = list(
        db.scalars(
            select(PaymentMethod).where(
                PaymentMethod.name_ar.in_(_legacy_owner_equity_names())
            )
        ).all()
    )
    if unified is None:
        if legacy_rows:
            unified = legacy_rows[0]
            unified.name_ar = OWNER_EQUITY_PM_NAME
        else:
            unified = PaymentMethod(
                name_ar=OWNER_EQUITY_PM_NAME,
                kind=PaymentMethodKind.OTHER,
                is_active=True,
                sort_order=15,
                can_receive=False,
                can_pay=True,
                can_fund=True,
                is_system=True,
            )
            db.add(unified)
            db.flush()
    unified.can_receive = False
    unified.can_pay = True
    unified.can_fund = True
    unified.is_system = True
    unified.is_active = True
    unified.show_on_dashboard = False
    unified.sort_order = 15
    for leg in legacy_rows:
        if leg.id == unified.id:
            continue
        db.execute(
            update(PaymentTransfer)
            .where(PaymentTransfer.to_payment_method_id == leg.id)
            .values(to_payment_method_id=unified.id)
        )
        db.execute(
            update(PaymentTransfer)
            .where(PaymentTransfer.from_payment_method_id == leg.id)
            .values(from_payment_method_id=unified.id)
        )
        try:
            delete_payment_method(db, leg.id, force_legacy=True)
        except PaymentsError:
            leg.is_active = False
    db.flush()
    return unified


def is_supplier_credit_payment_method(pm: PaymentMethod | None) -> bool:
    return pm is not None and pm.name_ar == SUPPLIER_CREDIT_PM_NAME


def is_owner_equity_payment_method(pm: PaymentMethod | None) -> bool:
    if pm is None:
        return False
    if pm.name_ar == OWNER_EQUITY_PM_NAME:
        return True
    return pm.name_ar in _legacy_owner_equity_names()


def _is_equity_system_payment_method(pm: PaymentMethod | None) -> bool:
    return is_owner_equity_payment_method(pm)


def payment_method_kind_matches(pm: PaymentMethod, kind: PaymentMethodKind) -> bool:
    """مقارنة نوع المحفظة (enum أو نص من SQLite)."""
    k = pm.kind
    if isinstance(k, PaymentMethodKind):
        return k == kind
    return str(k).strip().upper() == kind.value


_TAWA_NAME_MARKERS = ("توا", "تداول", "تداو", "tawa", "tadawul", "tadao")


def payment_method_requires_operation_ref(pm: PaymentMethod | None) -> bool:
    """مصرف أو حساب توا/تداول — يلزم رقم العملية قبل التحويل."""
    if pm is None:
        return False
    if payment_method_kind_matches(pm, PaymentMethodKind.BANK):
        return True
    name = f"{getattr(pm, 'name_ar', '') or ''} {getattr(pm, 'name', '') or ''}"
    name_l = name.lower()
    return any(marker in name or marker in name_l for marker in _TAWA_NAME_MARKERS)


def compose_transfer_note_with_bank_ref(note: str | None, bank_ref: str | None) -> str | None:
    ref = (bank_ref or "").strip()
    extra = (note or "").strip()
    parts: list[str] = []
    if ref:
        parts.append(f"رقم العملية: {ref}")
    if extra:
        parts.append(extra)
    return " — ".join(parts) if parts else None


def assert_bank_operation_ref(
    from_pm: PaymentMethod | None,
    to_pm: PaymentMethod | None,
    bank_ref: str | None,
) -> str:
    """يفرض رقم العملية إن كان أحد الطرفين مصرفاً أو توا."""
    needs = payment_method_requires_operation_ref(
        from_pm
    ) or payment_method_requires_operation_ref(to_pm)
    ref = (bank_ref or "").strip()
    if needs and not ref:
        raise PaymentsError(
            "أدخل رقم العملية التي أُجريت في حساب توا أو المصرف قبل تسجيل التحويل."
        )
    return ref


def is_treasury_wallet_method(pm: PaymentMethod) -> bool:
    return payment_method_kind_matches(
        pm, PaymentMethodKind.CASH
    ) or payment_method_kind_matches(pm, PaymentMethodKind.BANK)


def list_wallet_methods_for_shortage_forgive(
    db: Session, *, kind: PaymentMethodKind | None = None, only_active: bool = True
) -> list[PaymentMethod]:
    """محافظ كاش/مصرف المسموح الصرف منها عند عفو عجز الجلسة."""
    out: list[PaymentMethod] = []
    for m in list_payment_methods_for_pay(db, only_active=only_active):
        if not is_treasury_wallet_method(m):
            continue
        if kind is not None and not payment_method_kind_matches(m, kind):
            continue
        out.append(m)
    return sorted(out, key=lambda x: (x.sort_order, x.id))


def payment_method_can_receive_transfer(pm: PaymentMethod) -> bool:
    """استلام تحويلات بين الحسابات — منفصل عن قبض نقطة البيع (can_receive)."""
    if (
        is_supplier_credit_payment_method(pm)
        or is_owner_equity_payment_method(pm)
    ):
        return False
    return pm.can_fund


def list_payment_methods_for_receive(
    db: Session, *, only_active: bool = True, domain=None
) -> list[PaymentMethod]:
    return [
        m
        for m in list_payment_methods(db, only_active=only_active, domain=domain)
        if m.can_receive
        and not is_supplier_credit_payment_method(m)
        and not is_owner_equity_payment_method(m)
    ]


def list_pos_sale_payment_methods(
    db: Session, *, only_active: bool = True, domain=None, user=None
) -> list[PaymentMethod]:
    """وسائل الدفع في تحصيل نقطة البيع وتسوية الشقق — كاش ومصرف فقط."""
    from modules.authz.pos_wallet_access import filter_payment_methods_for_user
    from modules.platform.business_domain import BusinessDomain

    ctx = domain if domain is not None else BusinessDomain.RESTAURANT
    methods = [
        m
        for m in list_payment_methods_for_receive(db, only_active=only_active, domain=ctx)
        if is_treasury_wallet_method(m)
    ]
    return filter_payment_methods_for_user(methods, user)


def user_may_receive_hotel_to_restaurant_treasury(user) -> bool:
    """أدمن النظام وأمين الخزينة: قبض من حساب الفندق مباشرة على خزنة المطعم."""
    from modules.authz.permissions import TREASURY_CLERK_ROLE_NAME_AR
    from modules.authz.service import user_has_permission
    from modules.platform.business_domain import is_system_admin, user_role_names

    if user is None:
        return False
    if is_system_admin(user):
        return True
    if TREASURY_CLERK_ROLE_NAME_AR in user_role_names(user):
        return True
    # احتياطي إن وُجدت صلاحية الخزينة دون اسم الدور القديم
    from modules.authz.capability import can_hotel_settle_transfer

    return user_has_permission(user, "hotel:settle") and can_hotel_settle_transfer(user)


def _is_restaurant_main_treasury_receive_target(pm: PaymentMethod) -> bool:
    """الخزينة الرئيسية للمطعم — قبض مسموح عند تسوية حساب فندق فقط."""
    from modules.payments.shift_handoff_service import is_main_treasury_payment_method

    return is_main_treasury_payment_method(pm) and payment_method_is_strict_domain(
        pm, PaymentMethodDomain.RESTAURANT
    )


def _hotel_settle_wallet_allowed(
    pm: PaymentMethod, *, include_restaurant: bool
) -> bool:
    if not pm.is_active or not is_treasury_wallet_method(pm):
        return False
    if (
        is_purchase_custody_payment_method(pm)
        or is_supplier_credit_payment_method(pm)
        or is_owner_equity_payment_method(pm)
    ):
        return False
    if payment_method_is_strict_domain(pm, PaymentMethodDomain.HOTEL):
        # خزينة الفندق: can_receive=False (لا قبض مباشر) لكن can_pay=True للتسوية
        return bool(pm.can_pay or pm.can_receive)
    if not include_restaurant:
        return False
    # خزينة المطعم الرئيسية (can_receive=False عمداً لنقطة البيع)
    if _is_restaurant_main_treasury_receive_target(pm):
        return True
    # أي محفظة قبض مطعم/مشتركة (كاش نقطة البيع…)
    if pm.can_receive and str(
        _domain_value(getattr(pm, "business_domain", None))
    ).strip().lower() in (
        PaymentMethodDomain.RESTAURANT.value,
        PaymentMethodDomain.SHARED.value,
    ):
        return True
    return False


def list_hotel_settle_payment_methods(
    db: Session,
    *,
    only_active: bool = True,
    include_restaurant: bool | None = None,
    user=None,
) -> list[PaymentMethod]:
    """وسائل الدفع في الفندق — محافظ الاستقبال أولاً؛ وللأدمن/الخزينة أيضاً خزائن المطعم."""

    ensure_hotel_treasury_payment_methods(db)
    if include_restaurant is None:
        include_restaurant = bool(
            user and user_may_receive_hotel_to_restaurant_treasury(user)
        )
    if include_restaurant:
        from modules.payments.shift_handoff_service import (
            ensure_main_treasury_payment_methods,
        )

        ensure_main_treasury_payment_methods(db)

    from modules.authz.pos_wallet_access import filter_payment_methods_for_user

    rows = [
        m
        for m in list_payment_methods(db, only_active=only_active)
        if _hotel_settle_wallet_allowed(m, include_restaurant=include_restaurant)
    ]
    rows = filter_payment_methods_for_user(rows, user)
    # فندق أولاً ثم مطعم/مشترك، والكاش قبل المصرف داخل كل مجال
    domain_rank = {
        PaymentMethodDomain.HOTEL.value: 0,
        PaymentMethodDomain.RESTAURANT.value: 1,
        PaymentMethodDomain.SHARED.value: 2,
    }
    kind_rank = {"cash": 0, "bank": 1}

    def _sort_key(m: PaymentMethod):
        dom = str(_domain_value(getattr(m, "business_domain", None))).strip().lower()
        kind = str(getattr(m.kind, "value", m.kind) or "").strip().lower()
        return (
            domain_rank.get(dom, 9),
            kind_rank.get(kind, 9),
            m.sort_order,
            m.id,
        )

    rows.sort(key=_sort_key)
    return rows


def assert_hotel_payment_method(
    db: Session,
    payment_method_id: int | None,
    *,
    allow_restaurant: bool | None = None,
    user=None,
) -> PaymentMethod:
    if allow_restaurant is None:
        allow_restaurant = bool(
            user and user_may_receive_hotel_to_restaurant_treasury(user)
        )
    ensure_hotel_treasury_payment_methods(db)
    pm = db.get(PaymentMethod, payment_method_id) if payment_method_id else None
    if pm is None or not _hotel_settle_wallet_allowed(
        pm, include_restaurant=bool(allow_restaurant)
    ):
        raise PaymentsError(
            "اختر محفظة استقبال الفندق (كاش/مصرف)، أو خزينة مطعم (متاحة للأدمن وأمين الخزينة)."
            if allow_restaurant
            else "اختر محفظة استقبال الفندق كاش أو مصرف."
        )
    return pm


def is_pos_sale_payment_method(pm: PaymentMethod | None) -> bool:
    if pm is None or not pm.is_active:
        return False
    if is_supplier_credit_payment_method(pm) or is_owner_equity_payment_method(pm):
        return False
    if not pm.can_receive or not is_treasury_wallet_method(pm):
        return False
    return True


def list_payment_methods_for_pay(
    db: Session, *, only_active: bool = True, domain=None
) -> list[PaymentMethod]:
    return [
        m
        for m in list_payment_methods(db, only_active=only_active, domain=domain)
        if m.can_pay
        and not is_supplier_credit_payment_method(m)
        and not _is_equity_system_payment_method(m)
    ]


def list_payment_methods_main_treasury_for_pay(
    db: Session, *, only_active: bool = True, domain=None
) -> list[PaymentMethod]:
    """الخزينة الرئيسية — كاش/مصرف فقط (فواتير الشراء والأصول والاستهلاكات)."""
    from modules.payments.models import PaymentMethodKind
    from modules.payments.shift_handoff_service import (
        ensure_main_treasury_payment_method,
        is_main_treasury_payment_method,
    )

    ensure_main_treasury_payment_method(db, PaymentMethodKind.CASH)
    ensure_main_treasury_payment_method(db, PaymentMethodKind.BANK)
    rows = [
        m
        for m in list_payment_methods_for_pay(db, only_active=only_active, domain=domain)
        if is_main_treasury_payment_method(m)
    ]
    rows.sort(key=lambda m: (0 if m.kind == PaymentMethodKind.CASH else 1, m.sort_order))
    return rows


def is_hotel_treasury_payment_method(pm: PaymentMethod) -> bool:
    return pm.name_ar in (HOTEL_TREASURY_CASH_PM_NAME, HOTEL_TREASURY_BANK_PM_NAME)


def is_hotel_reception_payment_method(pm: PaymentMethod | None) -> bool:
    if pm is None:
        return False
    return pm.name_ar in (HOTEL_RECEPTION_CASH_PM_NAME, HOTEL_RECEPTION_BANK_PM_NAME)


def is_hotel_domain_cash_wallet(pm: PaymentMethod | None) -> bool:
    """محفظة فندق: استقبال (تحصيل) أو خزينة نهائية."""
    if pm is None:
        return False
    return is_hotel_treasury_payment_method(pm) or is_hotel_reception_payment_method(pm)


def is_purchase_source_wallet(pm: PaymentMethod) -> bool:
    """خزينة مطعم أو فندق أو عهدة مشتريات — مصدر دفع لفاتورة شراء."""
    from modules.payments.shift_handoff_service import is_main_treasury_payment_method

    return (
        is_main_treasury_payment_method(pm)
        or is_hotel_domain_cash_wallet(pm)
        or is_purchase_custody_payment_method(pm)
    )


def list_payment_methods_hotel_treasury_for_pay(
    db: Session, *, only_active: bool = True, domain=None
) -> list[PaymentMethod]:
    """خزينة الفندق — كاش/مصرف."""
    ensure_hotel_treasury_payment_methods(db)
    rows = [
        m
        for m in list_payment_methods_for_pay(db, only_active=only_active, domain=domain)
        if is_hotel_treasury_payment_method(m)
    ]
    rows.sort(key=lambda m: (0 if m.kind == PaymentMethodKind.CASH else 1, m.sort_order))
    return rows


def is_purchase_custody_payment_method(pm: PaymentMethod) -> bool:
    return pm.name_ar in PURCHASE_CUSTODY_PM_NAMES and pm.kind in (
        PaymentMethodKind.CASH,
        PaymentMethodKind.BANK,
    )


def ensure_purchase_custody_payment_method(
    db: Session, *, domain: PaymentMethodDomain | None = None
) -> PaymentMethod:
    from modules.gl.purchase_custody_wallets import ensure_purchase_custody_wallets

    ensure_purchase_custody_wallets(db)
    name = (
        HOTEL_PURCHASE_CUSTODY_CASH_PM_NAME
        if domain == PaymentMethodDomain.HOTEL
        else RESTAURANT_PURCHASE_CUSTODY_CASH_PM_NAME
    )
    pm = db.scalar(select(PaymentMethod).where(PaymentMethod.name_ar == name))
    if pm is None:
        raise PaymentsError("حساب عهدة المشتريات غير مهيّأ.")
    return pm


def list_payment_methods_purchase_custody_for_pay(
    db: Session, *, only_active: bool = True, domain=None
) -> list[PaymentMethod]:
    """عهدة المشتريات (كاش + مصرف) — لموظف إدخال الفواتير."""
    from modules.gl.purchase_custody_wallets import ensure_purchase_custody_wallets

    ensure_purchase_custody_wallets(db)
    dom = domain
    rows = [
        m
        for m in list_payment_methods_for_pay(db, only_active=only_active, domain=dom)
        if is_purchase_custody_payment_method(m)
    ]
    if not rows and dom is not None:
        rows = [
            m
            for m in list_payment_methods_for_pay(db, only_active=only_active, domain=None)
            if is_purchase_custody_payment_method(m)
        ]
    rows.sort(key=lambda m: (0 if m.kind == PaymentMethodKind.CASH else 1, m.sort_order))
    return rows


def list_payment_methods_for_purchase_term_custody(
    db: Session, *, only_active: bool = True, domain=None
) -> list[PaymentMethod]:
    """فواتير شراء لموظف المشتريات — عهدة كاش/مصرف فقط."""
    return list_payment_methods_purchase_custody_for_pay(
        db, only_active=only_active, domain=domain
    )


def assert_purchase_custody_or_supplier_credit(
    db: Session, payment_method_id: int
) -> PaymentMethod:
    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active:
        raise PaymentsError("أسلوب الدفع غير صالح.")
    if is_supplier_credit_payment_method(pm) or is_purchase_custody_payment_method(pm):
        return pm
    raise PaymentsError(
        "فواتير الشراء تُسجَّل على عهدة المشتريات أو آجل للمورد — "
        "لا يمكن استخدام الخزينة الرئيسية."
    )


def assert_purchase_custody_payment_method(
    db: Session, payment_method_id: int
) -> PaymentMethod:
    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active or not pm.can_pay:
        raise PaymentsError("أسلوب الدفع غير صالح.")
    if not is_purchase_custody_payment_method(pm):
        raise PaymentsError("اختر حساب عهدة المشتريات فقط.")
    return pm


def purchase_user_limited_to_custody(user) -> bool:
    from modules.authz.permissions import PURCHASES_MANAGE, PURCHASE_INVOICES_MANAGE
    from modules.authz.service import user_has_permission

    return user_has_permission(
        user, PURCHASE_INVOICES_MANAGE
    ) and not user_has_permission(user, PURCHASES_MANAGE)


def assert_purchase_term_payment_method(
    db: Session, payment_method_id: int
) -> PaymentMethod:
    """فاتورة شراء: خزينة مطعم/فندق، عهدة مشتريات، أو آجل."""
    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active:
        raise PaymentsError("أسلوب الدفع غير صالح.")
    if is_supplier_credit_payment_method(pm) or is_purchase_source_wallet(pm):
        return pm
    raise PaymentsError(
        "فواتير الشراء تُسجَّل على خزينة المطعم أو الفندق أو عهدة المشتريات أو آجل للمورد."
    )


def assert_purchase_pay_wallet(
    db: Session, payment_method_id: int, *, custody_only: bool = False
) -> PaymentMethod:
    """سداد فاتورة شراء — من خزينة المطعم/الفندق أو العهدة."""
    if custody_only:
        return assert_purchase_custody_payment_method(db, payment_method_id)

    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active or not pm.can_pay:
        raise PaymentsError("أسلوب الدفع غير صالح.")
    if is_purchase_source_wallet(pm):
        return pm
    raise PaymentsError("اختر خزينة المطعم أو الفندق أو عهدة المشتريات.")


def list_payment_methods_for_purchase_pay(
    db: Session, *, only_active: bool = True, custody_only: bool = False
) -> list[PaymentMethod]:
    """محافظ سداد فواتير الشراء — مطعم + فندق معاً (بدون تصفية مجال)."""
    if custody_only:
        return list_payment_methods_purchase_custody_for_pay(
            db, only_active=only_active, domain=None
        )
    out: list[PaymentMethod] = []
    seen: set[int] = set()
    for group in (
        list_payment_methods_main_treasury_for_pay(db, only_active=only_active, domain=None),
        list_payment_methods_hotel_treasury_for_pay(db, only_active=only_active, domain=None),
        list_payment_methods_purchase_custody_for_pay(
            db, only_active=only_active, domain=None
        ),
    ):
        for m in group:
            if m.id not in seen:
                out.append(m)
                seen.add(m.id)
    return out


def list_payment_methods_for_purchase_term(
    db: Session, *, only_active: bool = True, domain=None
) -> list[PaymentMethod]:
    """حسابات يمكن اختيارها عند إنشاء فاتورة شراء (آجل أو دفع فوري)."""
    credit = ensure_supplier_credit_payment_method(db)
    out: list[PaymentMethod] = []
    seen: set[int] = set()
    # domain=None: يظهر خزين المطعم والفندق معاً لموظف المشتريات
    pay_domain = domain  # يُمرَّر None عادةً من الراوتر
    for m in list_payment_methods_main_treasury_for_pay(
        db, only_active=only_active, domain=pay_domain
    ):
        if m.id not in seen:
            out.append(m)
            seen.add(m.id)
    for m in list_payment_methods_hotel_treasury_for_pay(
        db, only_active=only_active, domain=pay_domain
    ):
        if m.id not in seen:
            out.append(m)
            seen.add(m.id)
    for m in list_payment_methods_purchase_custody_for_pay(
        db, only_active=only_active, domain=pay_domain
    ):
        if m.id not in seen:
            out.append(m)
            seen.add(m.id)
    if credit.id not in seen and (not only_active or credit.is_active):
        out.insert(0, credit)
    return out


def assert_main_treasury_or_supplier_credit(db: Session, payment_method_id: int) -> PaymentMethod:
    """يتحقق أن الدفع من الخزينة الرئيسية أو ذمم المورد (آجل)."""
    from modules.payments.shift_handoff_service import is_main_treasury_payment_method

    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active:
        raise PaymentsError("أسلوب الدفع غير صالح.")
    if is_supplier_credit_payment_method(pm) or is_main_treasury_payment_method(pm):
        return pm
    raise PaymentsError("فواتير الشراء تُسدَّد من الخزينة الرئيسية (كاش أو مصرف) أو آجل للمورد.")


def assert_main_treasury_payment_method(db: Session, payment_method_id: int) -> PaymentMethod:
    from modules.payments.shift_handoff_service import is_main_treasury_payment_method

    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active or not pm.can_pay:
        raise PaymentsError("أسلوب الدفع غير صالح.")
    if not is_main_treasury_payment_method(pm):
        raise PaymentsError("اختر الخزينة الرئيسية — كاش أو الخزينة الرئيسية — مصرف.")
    return pm


def list_payment_methods_transfer_sources(
    db: Session, *, only_active: bool = True, domain=None
) -> list[PaymentMethod]:
    ensure_owner_equity_payment_method(db)
    rows = [
        m
        for m in list_payment_methods(db, only_active=only_active, domain=domain)
        if m.can_pay and not is_supplier_credit_payment_method(m)
    ]
    rows.sort(key=lambda m: (0 if is_owner_equity_payment_method(m) else 1, m.sort_order, m.id))
    return rows


def list_clerk_transfer_source_methods(
    db: Session,
    user,
    session: dict | None = None,
    *,
    only_active: bool = True,
) -> list[PaymentMethod]:
    """أمين الخزينة يحوّل فقط من خزينة مجاله: مطعم كاش/مصرف أو فندق كاش/مصرف."""
    from modules.authz.capability import is_treasury_clerk_user
    from modules.payments.shift_handoff_service import is_main_treasury_payment_method
    from modules.platform.business_domain import (
        BusinessDomain,
        is_system_admin,
        resolve_finance_domain,
    )

    rows = list_payment_methods_transfer_sources(db, only_active=only_active)
    if is_system_admin(user):
        return rows
    from modules.authz.pos_wallet_access import (
        apply_custom_transfer_filter,
        user_custom_transfer_ids,
    )

    custom_send = user_custom_transfer_ids(db, user, direction="send")
    if custom_send is not None:
        return apply_custom_transfer_filter(rows, custom_send)
    if not is_treasury_clerk_user(user):
        return rows
    domain = resolve_finance_domain(user, session)
    if domain == BusinessDomain.HOTEL:
        return [m for m in rows if is_hotel_treasury_payment_method(m)]
    return [m for m in rows if is_main_treasury_payment_method(m)]


def list_clerk_transfer_target_methods(
    db: Session,
    user,
    *,
    only_active: bool = True,
) -> list[PaymentMethod]:
    """وجهة التحويل: كل حسابات الاستلام، أو ما خصّصه الأدمن للموظف."""
    from modules.platform.business_domain import is_system_admin

    rows = list_payment_methods_transfer_targets(db, only_active=only_active)
    if is_system_admin(user):
        return rows
    from modules.authz.pos_wallet_access import (
        apply_custom_transfer_filter,
        user_custom_transfer_ids,
    )

    custom_recv = user_custom_transfer_ids(db, user, direction="receive")
    return apply_custom_transfer_filter(rows, custom_recv)


def list_payment_methods_for_dashboard(
    db: Session, *, only_active: bool = True, domain=None
) -> list[PaymentMethod]:
    """حسابات كاش/مصرف المعروضة في لوحة الخزينة."""
    return [
        m
        for m in list_payment_methods(db, only_active=only_active, domain=domain)
        if m.show_on_dashboard
        and m.kind in (PaymentMethodKind.CASH, PaymentMethodKind.BANK)
    ]


def list_payment_methods_transfer_targets(
    db: Session, *, only_active: bool = True, domain=None
) -> list[PaymentMethod]:
    ensure_owner_equity_payment_method(db)
    rows = []
    for m in list_payment_methods(db, only_active=only_active, domain=domain):
        if payment_method_can_receive_transfer(m) or is_owner_equity_payment_method(m):
            rows.append(m)
    rows.sort(key=lambda m: (0 if is_owner_equity_payment_method(m) else 1, m.sort_order, m.id))
    return rows


def list_payment_methods_owner_capital_targets(
    db: Session, *, only_active: bool = True, domain=None
) -> list[PaymentMethod]:
    """حسابات كاش/مصرف يمكن إيداع رأس المال فيها."""
    return [
        m
        for m in list_payment_methods_transfer_targets(
            db, only_active=only_active, domain=domain
        )
        if not is_owner_equity_payment_method(m)
    ]


def payment_method_balances_map(db: Session) -> dict[int, Decimal]:
    """أرصدة كل الحسابات — استعلام واحد بدل تكرار wallet_breakdown."""
    return {row.method.id: row.net for row in wallet_breakdown(db, None, None)}


def method_current_balance(db: Session, payment_method_id: int) -> Decimal:
    return payment_method_balances_map(db).get(payment_method_id, Decimal("0"))


def sum_purchase_payments(db: Session, purchase_id: int) -> Decimal:
    total = db.execute(
        select(func.coalesce(func.sum(PurchasePayment.amount), 0)).where(
            PurchasePayment.purchase_id == purchase_id
        )
    ).scalar_one()
    return Decimal(str(total or 0)).quantize(Decimal("0.001"))


def purchase_outstanding(db: Session, purchase: Purchase) -> Decimal:
    from modules.payments.cost_reference import is_cost_reference_purchase

    if is_cost_reference_purchase(purchase):
        return Decimal("0")
    paid = sum_purchase_payments(db, purchase.id)
    outstanding = Decimal(str(purchase.amount or 0)) - paid
    if outstanding < 0:
        return Decimal("0")
    return outstanding.quantize(Decimal("0.001"))


def record_purchase_payment(
    db: Session,
    *,
    purchase_id: int,
    payment_method_id: int,
    amount: Decimal,
    user_id: int | None = None,
    note: str | None = None,
    payment_proof_image_filename: str | None = None,
    allow_any_pay_wallet: bool = False,
    purchase_custody_only: bool = False,
) -> PurchasePayment:
    p = db.get(Purchase, purchase_id)
    if p is None:
        raise PaymentsError("فاتورة الشراء غير موجودة.")
    if allow_any_pay_wallet:
        pm = db.get(PaymentMethod, payment_method_id)
        if pm is None or not pm.is_active or not pm.can_pay:
            raise PaymentsError("محفظة السداد غير صالحة.")
    elif purchase_custody_only:
        pm = assert_purchase_custody_payment_method(db, payment_method_id)
    else:
        pm = assert_purchase_pay_wallet(db, payment_method_id)
    if is_supplier_credit_payment_method(pm):
        raise PaymentsError("لا يمكن السداد عبر محفظة «ذمم دائن» — اختر كاش أو مصرف.")
    if amount <= 0:
        raise PaymentsError("مبلغ الدفع يجب أن يكون أكبر من صفر.")
    outstanding = purchase_outstanding(db, p)
    if amount > outstanding:
        raise PaymentsError(
            f"المبلغ يتجاوز المتبقي ({outstanding} د.ل)."
        )
    pp = PurchasePayment(
        purchase_id=purchase_id,
        payment_method_id=payment_method_id,
        amount=amount.quantize(Decimal("0.001")),
        note=(note or "").strip() or None,
        payment_proof_image_filename=(payment_proof_image_filename or "").strip()
        or None,
        created_by_id=user_id,
    )
    db.add(pp)
    db.flush()
    if purchase_outstanding(db, p) <= 0:
        from modules.dashboard_notify.constants import PURCHASES
        from modules.dashboard_notify.service import resolve_activity

        resolve_activity(db, PURCHASES, ref_id=purchase_id)
    from modules.gl.posting import post_purchase_payment_shadow_safe

    post_purchase_payment_shadow_safe(db, pp)
    try:
        from modules.notifications.treasury_hooks import emit_treasury_movement

        emit_treasury_movement(
            db,
            source_type="purchase_payment",
            source_id=pp.id,
            movement_label=f"سداد فاتورة شراء #{purchase_id}",
            amount=pp.amount,
            from_method=pm.name_ar,
            to_method="مورد",
            user_id=user_id,
        )
    except Exception:  # noqa: BLE001
        pass
    return pp


def list_payment_methods(
    db: Session,
    *,
    only_active: bool = False,
    domain=None,
) -> list[PaymentMethod]:
    from modules.platform.business_domain import (
        BusinessDomain,
        payment_method_visible_for_domain,
    )

    stmt = select(PaymentMethod).order_by(PaymentMethod.sort_order, PaymentMethod.id)
    if only_active:
        stmt = stmt.where(PaymentMethod.is_active.is_(True))
    rows = list(db.scalars(stmt).all())
    if domain is None:
        return rows
    filter_domain = domain if isinstance(domain, BusinessDomain) else BusinessDomain(domain)
    return [
        m
        for m in rows
        if payment_method_visible_for_domain(
            getattr(m, "business_domain", PaymentMethodDomain.SHARED),
            filter_domain=filter_domain,
        )
    ]


def create_payment_method(
    db: Session,
    name_ar: str,
    kind: PaymentMethodKind,
    sort_order: int = 0,
    *,
    can_receive: bool | None = None,
    can_pay: bool | None = None,
    can_fund: bool | None = None,
    show_on_dashboard: bool | None = None,
    business_domain: PaymentMethodDomain = PaymentMethodDomain.SHARED,
) -> PaymentMethod:
    name = name_ar.strip()
    if not name:
        raise PaymentsError("الاسم مطلوب.")
    exists = db.execute(
        select(PaymentMethod).where(PaymentMethod.name_ar == name)
    ).scalar_one_or_none()
    if exists is not None:
        raise PaymentsError("اسم أسلوب الدفع موجود مسبقاً.")
    recv = can_receive if can_receive is not None else kind != PaymentMethodKind.OTHER
    pay = can_pay if can_pay is not None else kind != PaymentMethodKind.OTHER
    fund = (
        can_fund
        if can_fund is not None
        else kind in (PaymentMethodKind.CASH, PaymentMethodKind.BANK)
    )
    on_dash = (
        show_on_dashboard
        if show_on_dashboard is not None
        else kind in (PaymentMethodKind.CASH, PaymentMethodKind.BANK)
    )
    pm = PaymentMethod(
        name_ar=name,
        kind=kind,
        is_active=True,
        sort_order=sort_order,
        can_receive=recv,
        can_pay=pay,
        can_fund=fund,
        is_system=False,
        show_on_dashboard=on_dash,
        business_domain=business_domain,
    )
    db.add(pm)
    db.flush()
    return pm


def apply_payment_method_icon(
    db: Session,
    pm: PaymentMethod,
    static_root: Path,
    *,
    upload: UploadFile | None = None,
    clear: bool = False,
) -> None:
    """رفع أو حذف أيقونة وسيلة الدفع في نقطة البيع."""
    from modules.catalog.uploads import delete_stored_relative_file, save_payment_method_icon

    if clear:
        delete_stored_relative_file(static_root, pm.icon_path)
        pm.icon_path = None
        db.flush()
        return
    if upload is not None and upload.filename:
        delete_stored_relative_file(static_root, pm.icon_path)
        pm.icon_path = save_payment_method_icon(upload, static_root)
        db.flush()


def update_payment_method(
    db: Session,
    pm_id: int,
    *,
    name_ar: str | None = None,
    kind: PaymentMethodKind | None = None,
    is_active: bool | None = None,
    sort_order: int | None = None,
    can_receive: bool | None = None,
    can_pay: bool | None = None,
    can_fund: bool | None = None,
    show_on_dashboard: bool | None = None,
    business_domain: PaymentMethodDomain | None = None,
) -> PaymentMethod:
    pm = db.get(PaymentMethod, pm_id)
    if pm is None:
        raise PaymentsError("أسلوب الدفع غير موجود.")
    if pm.is_system and (name_ar is not None or kind is not None):
        raise PaymentsError("لا يمكن تعديل اسم أو نوع الحساب النظامي.")
    if name_ar is not None:
        n = name_ar.strip()
        if not n:
            raise PaymentsError("الاسم مطلوب.")
        clash = db.execute(
            select(PaymentMethod).where(
                PaymentMethod.name_ar == n, PaymentMethod.id != pm_id
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise PaymentsError("اسم أسلوب الدفع موجود مسبقاً.")
        pm.name_ar = n
    if kind is not None:
        pm.kind = kind
    if is_active is not None:
        pm.is_active = is_active
    if sort_order is not None:
        pm.sort_order = sort_order
    if is_owner_equity_payment_method(pm):
        pm.can_receive = False
        if can_receive:
            raise PaymentsError("حساب المالك لا يُستخدم في نقطة البيع.")
        if can_pay is not None:
            pm.can_pay = can_pay
        if can_fund is not None:
            pm.can_fund = can_fund
    else:
        if can_receive is not None:
            pm.can_receive = can_receive
        if can_pay is not None:
            pm.can_pay = can_pay
        if can_fund is not None:
            pm.can_fund = can_fund
    if show_on_dashboard is not None:
        if pm.is_system and show_on_dashboard:
            raise PaymentsError("لا يمكن إظهار الحسابات النظامية في لوحة الخزينة.")
        if pm.kind == PaymentMethodKind.OTHER and show_on_dashboard:
            raise PaymentsError("حسابات «أخرى» لا تُعرض في لوحة الخزينة.")
        pm.show_on_dashboard = show_on_dashboard
    if business_domain is not None and not pm.is_system:
        pm.business_domain = business_domain
    db.flush()
    return pm


def payment_method_allow_delete(pm: PaymentMethod) -> bool:
    if is_supplier_credit_payment_method(pm):
        return False
    if pm.name_ar in _legacy_owner_equity_names():
        return True
    if is_owner_equity_payment_method(pm):
        return True
    if pm.is_system:
        return False
    return True


def delete_payment_method(
    db: Session, pm_id: int, *, force_legacy: bool = False
) -> None:
    from modules.delivery.models import DeliveryCashSettlement
    from modules.refunds.models import SaleReturn

    pm = db.get(PaymentMethod, pm_id)
    if pm is None:
        return
    if force_legacy:
        if pm.name_ar not in _legacy_owner_equity_names():
            raise PaymentsError("لا يمكن الحذف القسري إلا للحسابات القديمة المدمجة.")
    elif not payment_method_allow_delete(pm):
        raise PaymentsError(
            "لا يمكن حذف هذا الحساب (نظامي أو أساسي). يمكنك تعطيله من «مفعّل»."
        )
    used_in_sale = db.execute(
        select(SalePayment.id).where(SalePayment.payment_method_id == pm_id).limit(1)
    ).scalar_one_or_none()
    used_in_purchase = db.execute(
        select(Purchase.id).where(Purchase.payment_method_id == pm_id).limit(1)
    ).scalar_one_or_none()
    used_in_pp = db.execute(
        select(PurchasePayment.id)
        .where(PurchasePayment.payment_method_id == pm_id)
        .limit(1)
    ).scalar_one_or_none()
    used_in_xfer = db.execute(
        select(PaymentTransfer.id)
        .where(
            (PaymentTransfer.from_payment_method_id == pm_id)
            | (PaymentTransfer.to_payment_method_id == pm_id)
        )
        .limit(1)
    ).scalar_one_or_none()
    used_in_refund = db.execute(
        select(SaleReturn.id)
        .where(SaleReturn.refund_payment_method_id == pm_id)
        .limit(1)
    ).scalar_one_or_none()
    used_in_delivery = db.execute(
        select(DeliveryCashSettlement.id)
        .where(DeliveryCashSettlement.cash_method_id == pm_id)
        .limit(1)
    ).scalar_one_or_none()
    if (
        used_in_sale
        or used_in_purchase
        or used_in_pp
        or used_in_xfer
        or used_in_refund
        or used_in_delivery
    ):
        raise PaymentsError(
            "لا يمكن حذف الحساب لأنه مستخدم في عمليات سابقة. عطّله بدلاً من الحذف."
        )
    db.delete(pm)
    db.flush()


def record_sale_payment(
    db: Session,
    sale_id: int,
    payment_method_id: int,
    amount: Decimal,
    *,
    payment_proof_image_filename: str | None = None,
    for_hotel_settle: bool = False,
) -> SalePayment:
    """يسجّل دفعة لبيع (تستدعى مع complete_sale)."""
    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active:
        raise PaymentsError("أسلوب الدفع غير صالح.")
    if for_hotel_settle:
        # تسوية غرفة: خزينة فندق/مطعم، أو حساب المقاصة الداخلي بعد التحويل
        allowed = _hotel_settle_wallet_allowed(
            pm, include_restaurant=True
        ) or (pm.name_ar == ROOM_SETTLE_CLEARING_PM_NAME and pm.is_active)
        if not allowed:
            raise PaymentsError(f"الحساب «{pm.name_ar}» غير مسموح لهذه التسوية.")
    else:
        if not pm.can_receive:
            raise PaymentsError(f"الحساب «{pm.name_ar}» غير مسموح بالقبض عند البيع.")
        from modules.platform.business_domain import (
            BusinessDomain,
            assert_payment_method_for_domain,
        )

        assert_payment_method_for_domain(pm, filter_domain=BusinessDomain.RESTAURANT)
    sp = SalePayment(
        sale_id=sale_id,
        payment_method_id=payment_method_id,
        amount=amount,
        payment_proof_image_filename=(payment_proof_image_filename or "").strip()
        or None,
    )
    db.add(sp)
    db.flush()
    # ترحيل الخزينة/GL لمبيعات الجلسة يتم عند إقفال الجلسة — ليس بعد كل فاتورة
    from modules.sales.models import Sale

    sale_for_gl = db.get(Sale, int(sale_id))
    defer_shift_gl = bool(
        sale_for_gl is not None and getattr(sale_for_gl, "pos_shift_id", None)
    )
    if not defer_shift_gl:
        from modules.gl.posting import post_sale_payment_shadow_safe

        post_sale_payment_shadow_safe(db, sp)
        try:
            from modules.notifications.treasury_hooks import emit_treasury_movement

            emit_treasury_movement(
                db,
                source_type="sale_payment",
                source_id=sp.id,
                movement_label=f"تحصيل بيع #{sale_id}",
                amount=sp.amount,
                from_method="عميل",
                to_method=pm.name_ar,
                user_id=None,
            )
        except Exception:  # noqa: BLE001
            pass
    return sp


def list_sale_payments(db: Session, sale_id: int) -> list[SalePayment]:
    return list(
        db.scalars(
            select(SalePayment)
            .where(SalePayment.sale_id == sale_id)
            .order_by(SalePayment.created_at, SalePayment.id)
        ).all()
    )


def sum_sale_payments(db: Session, sale_id: int) -> Decimal:
    total = db.execute(
        select(func.coalesce(func.sum(SalePayment.amount), 0)).where(
            SalePayment.sale_id == sale_id
        )
    ).scalar_one()
    return Decimal(str(total or 0)).quantize(Decimal("0.001"))


def correct_sale_payment_method(
    db: Session,
    sale_id: int,
    new_payment_method_id: int,
    *,
    user_id: int | None,
) -> SalePayment:
    """تصحيح وسيلة دفع فاتورة مغلقة — نقل المبلغ بين الخزائن."""
    from modules.sales.models import Sale, SaleStatus

    sale = db.get(Sale, sale_id)
    if sale is None or sale.status != SaleStatus.COMPLETED:
        raise PaymentsError("يمكن تصحيح الدفع للفواتير المغلقة فقط.")
    from modules.refunds.service import get_open_room_charge

    if get_open_room_charge(db, sale_id) is not None:
        raise PaymentsError("لا يمكن تغيير وسيلة الدفع لحساب الشقة من هنا.")
    pays = list_sale_payments(db, sale_id)
    if len(pays) != 1:
        raise PaymentsError(
            "تصحيح وسيلة الدفع متاح حالياً للفواتير بدفعة واحدة فقط."
        )
    sp = pays[0]
    old_id = int(sp.payment_method_id)
    if old_id == int(new_payment_method_id):
        raise PaymentsError("وسيلة الدفع الجديدة مطابقة للحالية.")
    new_pm = db.get(PaymentMethod, new_payment_method_id)
    old_pm = db.get(PaymentMethod, old_id)
    if new_pm is None or not new_pm.is_active or not new_pm.can_receive:
        raise PaymentsError("وسيلة الدفع الجديدة غير صالحة للقبض.")
    if old_pm is None:
        raise PaymentsError("وسيلة الدفع الحالية غير موجودة.")
    amount = Decimal(str(sp.amount or 0)).quantize(Decimal("0.001"))
    if amount <= 0:
        raise PaymentsError("مبلغ الدفع غير صالح.")
    record_manual_transfer(
        db,
        from_payment_method_id=old_id,
        to_payment_method_id=new_payment_method_id,
        amount=amount,
        user_id=user_id,
        note=f"تصحيح دفع فاتورة #{sale_id}",
        transfer_type=PaymentTransferType.SALE_PAYMENT_CORRECTION,
    )
    sp.payment_method_id = int(new_payment_method_id)
    db.flush()
    return sp


def record_refund_payment(
    db: Session,
    *,
    sale_return_id: int,
    payment_method_id: int,
    amount: Decimal,
    user_id: int | None,
    note: str | None = None,
) -> RefundPayment:
    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active:
        raise PaymentsError("أسلوب الدفع غير صالح.")
    if not pm.can_pay:
        raise PaymentsError(f"الحساب «{pm.name_ar}» غير مسموح برد المبالغ.")
    if amount <= 0:
        raise PaymentsError("مبلغ المرتجع يجب أن يكون أكبر من صفر.")
    rp = RefundPayment(
        sale_return_id=sale_return_id,
        payment_method_id=payment_method_id,
        amount=amount.quantize(Decimal("0.001")),
        note=(note or "").strip() or None,
        created_by_id=user_id,
    )
    db.add(rp)
    db.flush()
    from modules.gl.posting import post_refund_payment_shadow_safe

    post_refund_payment_shadow_safe(db, rp)
    try:
        from modules.notifications.treasury_hooks import emit_treasury_movement

        emit_treasury_movement(
            db,
            source_type="refund_payment",
            source_id=rp.id,
            movement_label=f"رد مرتجع #{sale_return_id}",
            amount=rp.amount,
            from_method=pm.name_ar,
            to_method="عميل",
            user_id=user_id,
        )
    except Exception:  # noqa: BLE001
        pass
    return rp


def record_payment_transfer(
    db: Session,
    *,
    sale_return_id: int,
    from_payment_method_id: int,
    to_payment_method_id: int,
    amount: Decimal,
    user_id: int | None,
    note: str | None = None,
) -> PaymentTransfer:
    return record_manual_transfer(
        db,
        from_payment_method_id=from_payment_method_id,
        to_payment_method_id=to_payment_method_id,
        amount=amount,
        user_id=user_id,
        note=note,
        transfer_type=PaymentTransferType.REFUND_SETTLEMENT,
        sale_return_id=sale_return_id,
    )


def record_manual_transfer(
    db: Session,
    *,
    from_payment_method_id: int,
    to_payment_method_id: int,
    amount: Decimal,
    user_id: int | None,
    note: str | None = None,
    transfer_type: PaymentTransferType = PaymentTransferType.MANUAL,
    sale_return_id: int | None = None,
    bank_ref: str | None = None,
    require_operation_ref: bool | None = None,
) -> PaymentTransfer:
    from_pm = db.get(PaymentMethod, from_payment_method_id)
    to_pm = db.get(PaymentMethod, to_payment_method_id)
    if from_pm is None or to_pm is None or not from_pm.is_active or not to_pm.is_active:
        raise PaymentsError("الحساب المصدر أو الوجهة غير صالح.")
    if from_payment_method_id == to_payment_method_id:
        raise PaymentsError("لا يمكن التحويل إلى نفس الحساب.")
    if is_supplier_credit_payment_method(from_pm) or is_supplier_credit_payment_method(to_pm):
        raise PaymentsError("لا يمكن التحويل من/إلى حساب ذمم المورد الآجل.")
    if not from_pm.can_pay:
        raise PaymentsError(f"الحساب «{from_pm.name_ar}» غير مسموح بالصرف أو التحويل الصادر.")
    if not (
        payment_method_can_receive_transfer(to_pm)
        or is_owner_equity_payment_method(to_pm)
    ):
        raise PaymentsError(
            f"الحساب «{to_pm.name_ar}» غير مسموح باستلام التحويلات. "
            "فعّل «تحويل وارد» من إدارة الحسابات."
        )
    if amount <= 0:
        raise PaymentsError("مبلغ التحويل يجب أن يكون أكبر من صفر.")
    if transfer_type == PaymentTransferType.MANUAL:
        if is_owner_equity_payment_method(to_pm) and not is_owner_equity_payment_method(
            from_pm
        ):
            transfer_type = PaymentTransferType.OWNER_DRAW
        elif is_owner_equity_payment_method(from_pm) and not is_owner_equity_payment_method(
            to_pm
        ):
            transfer_type = PaymentTransferType.OWNER_CAPITAL
    skip_ref_types = (
        PaymentTransferType.SHIFT_HANDOFF,
        PaymentTransferType.REFUND_SETTLEMENT,
        PaymentTransferType.SALE_PAYMENT_CORRECTION,
    )
    must_check = require_operation_ref
    if must_check is None:
        must_check = transfer_type not in skip_ref_types
    if must_check:
        assert_bank_operation_ref(from_pm, to_pm, bank_ref)
    if transfer_type not in (
        PaymentTransferType.OWNER_CAPITAL,
        PaymentTransferType.SHIFT_HANDOFF,
    ):
        bal = method_current_balance(db, from_payment_method_id)
        if amount > bal:
            raise PaymentsError(
                f"الرصيد غير كافٍ في «{from_pm.name_ar}» (المتاح: {bal} د.ل)."
            )
    tf = PaymentTransfer(
        transfer_type=transfer_type,
        sale_return_id=sale_return_id,
        from_payment_method_id=from_payment_method_id,
        to_payment_method_id=to_payment_method_id,
        amount=amount.quantize(Decimal("0.001")),
        note=compose_transfer_note_with_bank_ref(note, bank_ref),
        created_by_id=user_id,
    )
    db.add(tf)
    db.flush()
    from modules.gl.posting import post_payment_transfer_shadow_safe

    post_payment_transfer_shadow_safe(db, tf)
    try:
        from modules.notifications.treasury_hooks import emit_treasury_movement

        emit_treasury_movement(
            db,
            source_type="payment_transfer",
            source_id=tf.id,
            movement_label=transfer_type.value,
            amount=tf.amount,
            from_method=from_pm.name_ar,
            to_method=to_pm.name_ar,
            user_id=user_id,
        )
    except Exception:  # noqa: BLE001
        pass
    return tf


def record_owner_draw(
    db: Session,
    *,
    from_payment_method_id: int,
    amount: Decimal,
    user_id: int | None,
    note: str | None = None,
) -> PaymentTransfer:
    """سحب صاحب المشروع — محاسبياً: تخفيض حقوق الملكية وليس مصروفاً تشغيلياً."""
    owner = ensure_owner_equity_payment_method(db)
    return record_manual_transfer(
        db,
        from_payment_method_id=from_payment_method_id,
        to_payment_method_id=owner.id,
        amount=amount,
        user_id=user_id,
        note=(note or "").strip() or "سحب مالك",
        transfer_type=PaymentTransferType.OWNER_DRAW,
    )


def record_owner_capital(
    db: Session,
    *,
    to_payment_method_id: int,
    amount: Decimal,
    user_id: int | None,
    note: str | None = None,
) -> PaymentTransfer:
    """إيداع رأس مال من المالك — محاسبياً: زيادة حقوق الملكية وليس إيراداً."""
    capital = ensure_owner_equity_payment_method(db)
    to_pm = db.get(PaymentMethod, to_payment_method_id)
    if to_pm is None or not to_pm.is_active:
        raise PaymentsError("حساب الوجهة غير صالح.")
    if _is_equity_system_payment_method(to_pm) or is_supplier_credit_payment_method(to_pm):
        raise PaymentsError("لا يمكن الإيداع في هذا الحساب.")
    if not payment_method_can_receive_transfer(to_pm):
        raise PaymentsError(
            f"الحساب «{to_pm.name_ar}» غير مؤهل لاستلام الإيداع. "
            "استخدم حساب كاش أو مصرف مفعّل للقبض."
        )
    return record_manual_transfer(
        db,
        from_payment_method_id=capital.id,
        to_payment_method_id=to_payment_method_id,
        amount=amount,
        user_id=user_id,
        note=(note or "").strip() or "إيداع رأس مال من المالك",
        transfer_type=PaymentTransferType.OWNER_CAPITAL,
    )


def _apply_purchase_payment_at_create(
    db: Session,
    purchase: Purchase,
    pm: PaymentMethod,
    *,
    user_id: int | None,
    pay_amount: Decimal | None = None,
    payment_proof_image_filename: str | None = None,
    allow_any_pay_wallet: bool = False,
    purchase_custody_only: bool = False,
) -> None:
    """يسجّل دفعة عند إنشاء الفاتورة (كامل أو جزئي) ما لم تكن آجلة بالكامل."""
    if is_supplier_credit_payment_method(pm):
        return
    total = Decimal(str(purchase.amount or 0))
    amt = pay_amount if pay_amount is not None else total
    if amt <= 0:
        return
    if amt > total:
        amt = total
    record_purchase_payment(
        db,
        purchase_id=purchase.id,
        payment_method_id=pm.id,
        amount=amt,
        user_id=user_id,
        payment_proof_image_filename=payment_proof_image_filename,
        allow_any_pay_wallet=allow_any_pay_wallet,
        purchase_custody_only=purchase_custody_only,
    )


def _placeholder_payment_method_for_accrual(db: Session) -> PaymentMethod:
    """حساب مرجعي لمصروفات الاستحقاق (بدون حركة خزينة)."""
    for pm in list_payment_methods(db, only_active=True):
        if pm.can_pay:
            return pm
    pm = db.scalar(select(PaymentMethod).limit(1))
    if pm is None:
        raise PaymentsError("لا توجد محفظة دفع لتسجيل المصروف.")
    return pm


def record_accrual_expense(
    db: Session,
    *,
    amount: Decimal,
    expense_category: str | None,
    supplier: str | None,
    note: str | None,
    user_id: int | None,
    created_at: datetime | None = None,
    supplier_invoice_ref: str | None = None,
    business_domain: str | PaymentMethodDomain | None = None,
) -> Purchase:
    """مصروف تشغيلي بدون خصم من الخزينة (استحقاق محاسبي — مثل نقاط الولاء)."""
    if amount <= 0:
        raise PaymentsError("المبلغ يجب أن يكون أكبر من صفر.")
    pm = _placeholder_payment_method_for_accrual(db)
    ref = (supplier_invoice_ref or "").strip() or None
    from modules.platform.business_domain import resolve_record_business_domain

    dom_raw = resolve_record_business_domain(
        None, business_domain if isinstance(business_domain, str) else None,
        inherit_from=pm,
    )
    try:
        dom = PaymentMethodDomain(dom_raw)
    except ValueError:
        dom = PaymentMethodDomain.RESTAURANT
    p = Purchase(
        payment_method_id=pm.id,
        kind=PurchaseKind.EXPENSE,
        amount=amount.quantize(Decimal("0.001")),
        expense_category=(expense_category or "").strip() or None,
        supplier=(supplier or "").strip() or None,
        supplier_invoice_ref=ref,
        note=(note or "").strip() or None,
        created_by_id=user_id,
        business_domain=dom,
    )
    if created_at is not None:
        p.created_at = created_at
    db.add(p)
    db.flush()
    return p


def record_expense(
    db: Session,
    *,
    payment_method_id: int,
    amount: Decimal,
    expense_category: str | None,
    supplier: str | None,
    note: str | None,
    user_id: int | None,
    created_at: datetime | None = None,
    supplier_invoice_ref: str | None = None,
    invoice_image_filename: str | None = None,
    pos_shift_id: int | None = None,
    allow_any_pay_wallet: bool = False,
    business_domain: str | PaymentMethodDomain | None = None,
    filter_domain=None,
) -> Purchase:
    """مصروف عام (إيجار/راتب/فاتورة) — لا يضاف للمخزون."""
    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active:
        raise PaymentsError("أسلوب الدفع غير صالح.")
    if not pm.can_pay:
        raise PaymentsError(f"الحساب «{pm.name_ar}» غير مسموح بالصرف.")
    if amount <= 0:
        raise PaymentsError("المبلغ يجب أن يكون أكبر من صفر.")
    ref = (supplier_invoice_ref or "").strip() or None
    from modules.platform.business_domain import resolve_record_business_domain

    explicit = business_domain.value if hasattr(business_domain, "value") else business_domain
    dom_raw = resolve_record_business_domain(
        filter_domain, explicit, inherit_from=pm
    )
    try:
        dom = PaymentMethodDomain(dom_raw)
    except ValueError:
        dom = PaymentMethodDomain.RESTAURANT
    p = Purchase(
        payment_method_id=payment_method_id,
        kind=PurchaseKind.EXPENSE,
        amount=amount,
        expense_category=(expense_category or "").strip() or None,
        supplier=(supplier or "").strip() or None,
        supplier_invoice_ref=ref,
        invoice_image_filename=(invoice_image_filename or "").strip() or None,
        note=(note or "").strip() or None,
        created_by_id=user_id,
        pos_shift_id=pos_shift_id,
        business_domain=dom,
    )
    if created_at is not None:
        p.created_at = created_at
    db.add(p)
    db.flush()
    _apply_purchase_payment_at_create(
        db,
        p,
        pm,
        user_id=user_id,
        allow_any_pay_wallet=allow_any_pay_wallet,
    )
    from modules.gl.posting import post_expense_shadow_safe

    post_expense_shadow_safe(db, p)
    return p


def record_asset_purchase(
    db: Session,
    *,
    payment_method_id: int,
    supplier: str | None,
    note: str | None,
    lines: list[tuple[str, str | None, Decimal, Decimal, int, Decimal]],
    user_id: int | None,
    created_at: datetime | None = None,
    supplier_invoice_ref: str | None = None,
    invoice_image_filename: str | None = None,
    business_domain: str | PaymentMethodDomain | None = None,
) -> Purchase:
    """فاتورة أصول/أدوات للشركة (لا تباع، لا تأثير على المخزون).
    lines = [(item_name, unit, quantity, unit_cost, useful_life_months, salvage_value), ...]
    - useful_life_months = 0 → بند استهلاكي (مصروف فوري في شهر الشراء).
    - useful_life_months > 0 → أصل ثابت يُهلَك على فترة العمر الإنتاجي (القسط الثابت).
    """
    pm = assert_main_treasury_payment_method(db, payment_method_id)
    if not lines:
        raise PaymentsError("أضف بنداً واحداً على الأقل.")

    total = Decimal("0")
    cleaned: list[tuple[str, str | None, Decimal, Decimal, int, Decimal]] = []
    for name, unit, qty, cost, life, salvage in lines:
        nm = (name or "").strip()
        if not nm:
            raise PaymentsError("اكتب اسم الأداة/الأصل لكل بند.")
        if qty <= 0:
            raise PaymentsError("الكمية يجب أن تكون أكبر من صفر.")
        if cost < 0:
            raise PaymentsError("سعر الوحدة لا يمكن أن يكون سالباً.")
        if life < 0:
            raise PaymentsError("العمر الإنتاجي لا يمكن أن يكون سالباً.")
        if salvage < 0:
            raise PaymentsError("قيمة الخردة لا يمكن أن تكون سالبة.")
        line_total = (qty * cost).quantize(Decimal("0.001"))
        if salvage > line_total:
            raise PaymentsError(
                f"قيمة الخردة لـ «{nm}» ({salvage}) لا يمكن أن تتجاوز إجمالي البند ({line_total})."
            )
        u = (unit or "").strip() or None
        total += line_total
        cleaned.append((nm, u, qty, cost, int(life), salvage))
    total = total.quantize(Decimal("0.001"))
    if total <= 0:
        raise PaymentsError("إجمالي الفاتورة يجب أن يكون أكبر من صفر.")

    ref = (supplier_invoice_ref or "").strip() or None
    dom = business_domain
    if dom is None:
        dom = PaymentMethodDomain.RESTAURANT
    elif isinstance(dom, str):
        try:
            dom = PaymentMethodDomain(dom.strip().lower())
        except ValueError:
            dom = PaymentMethodDomain.RESTAURANT
    p = Purchase(
        payment_method_id=payment_method_id,
        kind=PurchaseKind.ASSET,
        amount=total,
        supplier=(supplier or "").strip() or None,
        supplier_invoice_ref=ref,
        invoice_image_filename=(invoice_image_filename or "").strip() or None,
        note=(note or "").strip() or None,
        created_by_id=user_id,
        business_domain=dom,
    )
    if created_at is not None:
        p.created_at = created_at
    db.add(p)
    db.flush()

    for name, unit, qty, cost, life, salvage in cleaned:
        pl = PurchaseLine(
            purchase_id=p.id,
            product_id=None,
            line_kind=(
                "FIXED_ASSET" if int(life) > 0 else "CONSUMABLE"
            ),
            item_name=name,
            unit=unit,
            quantity=qty.quantize(Decimal("0.0001")),
            unit_cost=cost.quantize(Decimal("0.001")),
            line_total=(qty * cost).quantize(Decimal("0.001")),
            useful_life_months=life,
            salvage_value=salvage.quantize(Decimal("0.001")),
        )
        db.add(pl)
    db.flush()
    _apply_purchase_payment_at_create(db, p, pm, user_id=user_id)
    from modules.dashboard_notify.constants import ASSETS
    from modules.dashboard_notify.service import record_activity

    record_activity(
        db,
        ASSETS,
        event_type="asset_purchase",
        ref_id=p.id,
        note=(supplier or note or f"أصول #{p.id}")[:255],
    )
    from modules.gl.posting import post_asset_purchase_shadow_safe

    post_asset_purchase_shadow_safe(db, p)
    return p


def record_consumable_purchase(
    db: Session,
    *,
    payment_method_id: int,
    supplier: str | None,
    note: str | None,
    consumable_category: str,
    lines: list[tuple[str, str | None, Decimal, Decimal]],
    user_id: int | None,
    created_at: datetime | None = None,
    supplier_invoice_ref: str | None = None,
    invoice_image_filename: str | None = None,
    business_domain: str | PaymentMethodDomain | None = None,
) -> Purchase:
    """فاتورة أدوات/استهلاكات — بنود بعمر 0 وتصنيف من إعدادات الأدمن."""
    cat = (consumable_category or "").strip()
    if not cat:
        raise PaymentsError("اختر تصنيف الاستهلاك.")
    if not lines:
        raise PaymentsError("أضف بنداً واحداً على الأقل.")
    asset_lines: list[tuple[str, str | None, Decimal, Decimal, int, Decimal]] = []
    for name, unit, qty, cost in lines:
        asset_lines.append((name, unit, qty, cost, 0, Decimal("0")))
    p = record_asset_purchase(
        db,
        payment_method_id=payment_method_id,
        supplier=supplier,
        note=note,
        lines=asset_lines,
        user_id=user_id,
        created_at=created_at,
        supplier_invoice_ref=supplier_invoice_ref,
        invoice_image_filename=invoice_image_filename,
        business_domain=business_domain,
    )
    p.expense_category = cat
    return p


def record_inventory_purchase(
    db: Session,
    *,
    payment_method_id: int,
    supplier: str | None,
    note: str | None,
    lines: list[MixedPurchaseLineIn]
    | list[InventoryPurchaseLineIn]
    | list[tuple[int, Decimal, Decimal]],
    user_id: int | None,
    warehouse_id: int | None,
    created_at: datetime | None = None,
    supplier_phone: str | None = None,
    supplier_invoice_ref: str | None = None,
    invoice_image_filename: str | None = None,
    payment_proof_image_filename: str | None = None,
    pay_now_amount: Decimal | None = None,
    pay_now_method_id: int | None = None,
    business_domain: str | PaymentMethodDomain | None = None,
    filter_domain=None,
    purchase_custody_only: bool = False,
) -> Purchase:
    """فاتورة شراء موحّدة — بنود مخزون و/أو أصول ثابتة و/أو استهلاكات في فاتورة واحدة."""
    from modules.catalog.models import Product, ProductKind
    from modules.inventory.lots import (
        create_lot_for_purchase_line,
        receipt_batch_no_for_purchase,
        validate_lot_expiry,
    )
    from modules.inventory.service import WarehouseError, resolve_warehouse_id
    from modules.payments.models import PurchaseLineKind

    mixed = _normalize_mixed_lines(lines)
    if not mixed:
        raise PaymentsError("أضف بنداً واحداً على الأقل لفاتورة الشراء.")

    product_lines = [
        ln for ln in mixed if (ln.kind or "").strip().upper() == PurchaseLineKind.PRODUCT.value
    ]
    asset_lines = [
        ln
        for ln in mixed
        if (ln.kind or "").strip().upper()
        in (PurchaseLineKind.FIXED_ASSET.value, PurchaseLineKind.CONSUMABLE.value)
    ]

    wid: int | None = None
    if product_lines:
        if warehouse_id is None:
            raise PaymentsError("اختر المخزن الذي تُضاف إليه بنود البضاعة.")
        try:
            wid = resolve_warehouse_id(db, warehouse_id)
        except WarehouseError as e:
            raise PaymentsError(str(e)) from e

    if purchase_custody_only:
        pm = assert_purchase_custody_payment_method(db, payment_method_id)
    else:
        pm = assert_purchase_term_payment_method(db, payment_method_id)

    pids = [ln.product_id for ln in product_lines if ln.product_id]
    products_by_id = {
        p.id: p
        for p in db.scalars(select(Product).where(Product.id.in_(pids))).all()
    } if pids else {}
    bad_products = [
        products_by_id[pid]
        for pid in pids
        if pid in products_by_id
        and products_by_id[pid].kind == ProductKind.FINAL_SELLABLE
        and not products_by_id[pid].direct_purchase_enabled
    ]
    if bad_products:
        names = [bp.name_ar for bp in bad_products]
        raise PaymentsError(
            "لا يمكن شراء منتجات نهائية غير مفعّل لها خيار الشراء المباشر: "
            + "، ".join(names)
            + ". فعّل «قابل للشراء والتوريد» من كرت الصنف إذا كان يُشترى جاهزاً مثل الكولا."
        )

    from modules.catalog.service import CatalogError

    total = Decimal("0")
    cleaned: list[MixedPurchaseLineIn] = []
    for ln in mixed:
        kind = (ln.kind or "").strip().upper() or PurchaseLineKind.PRODUCT.value
        if ln.quantity <= 0:
            raise PaymentsError("الكمية يجب أن تكون أكبر من صفر.")
        if ln.unit_cost < 0:
            raise PaymentsError("سعر الوحدة لا يمكن أن يكون سالباً.")
        line_total = (ln.quantity * ln.unit_cost).quantize(Decimal("0.001"))

        if kind == PurchaseLineKind.PRODUCT.value:
            if not ln.product_id:
                raise PaymentsError("اختر صنفاً لكل بند من نوع «منتج / مخزون».")
            product = products_by_id.get(ln.product_id)
            if product is None:
                raise PaymentsError("صنف غير موجود في أحد بنود الفاتورة.")
            try:
                dates = validate_lot_expiry(
                    product,
                    production_date=ln.production_date,
                    expiry_date=ln.expiry_date,
                )
            except CatalogError as exc:
                raise PaymentsError(str(exc)) from exc
            if product.expiry_tracked and dates.expiry_date is None:
                raise PaymentsError(
                    f"تاريخ الانتهاء مطلوب للصنف «{product.name_ar}» — فعّل الصلاحية في كرت الصنف."
                )
            cleaned.append(
                MixedPurchaseLineIn(
                    kind=PurchaseLineKind.PRODUCT.value,
                    product_id=ln.product_id,
                    quantity=ln.quantity,
                    unit_cost=ln.unit_cost,
                    production_date=dates.production_date,
                    expiry_date=dates.expiry_date,
                )
            )
        elif kind == PurchaseLineKind.FIXED_ASSET.value:
            nm = (ln.item_name or "").strip()
            if not nm:
                raise PaymentsError("اكتب اسم الأصل لكل بند من نوع «أصل ثابت».")
            life = int(ln.useful_life_months or 0)
            if life <= 0:
                raise PaymentsError(
                    f"العمر الإنتاجي مطلوب لبند الأصل «{nm}» (بالأشهر، أكبر من صفر)."
                )
            salvage = Decimal(str(ln.salvage_value or 0))
            if salvage < 0:
                raise PaymentsError("قيمة الخردة لا يمكن أن تكون سالبة.")
            if salvage > line_total:
                raise PaymentsError(
                    f"قيمة الخردة لـ «{nm}» لا يمكن أن تتجاوز إجمالي البند."
                )
            cleaned.append(
                MixedPurchaseLineIn(
                    kind=PurchaseLineKind.FIXED_ASSET.value,
                    item_name=nm,
                    unit=(ln.unit or "").strip() or None,
                    quantity=ln.quantity,
                    unit_cost=ln.unit_cost,
                    useful_life_months=life,
                    salvage_value=salvage,
                )
            )
        elif kind == PurchaseLineKind.CONSUMABLE.value:
            nm = (ln.item_name or "").strip()
            if not nm:
                raise PaymentsError("اكتب اسم البند لكل بند من نوع «استهلاك / تغليف».")
            cleaned.append(
                MixedPurchaseLineIn(
                    kind=PurchaseLineKind.CONSUMABLE.value,
                    item_name=nm,
                    unit=(ln.unit or "").strip() or None,
                    quantity=ln.quantity,
                    unit_cost=ln.unit_cost,
                    useful_life_months=0,
                    salvage_value=Decimal("0"),
                )
            )
        else:
            raise PaymentsError(f"نوع بند غير معروف: {kind}")
        total += line_total

    total = total.quantize(Decimal("0.001"))
    if total <= 0:
        raise PaymentsError("إجمالي الفاتورة يجب أن يكون أكبر من صفر.")

    ref = (supplier_invoice_ref or "").strip() or None
    from modules.platform.business_domain import resolve_record_business_domain

    explicit = business_domain.value if hasattr(business_domain, "value") else business_domain
    dom_raw = resolve_record_business_domain(filter_domain, explicit, inherit_from=pm)
    try:
        dom = PaymentMethodDomain(dom_raw)
    except ValueError:
        dom = PaymentMethodDomain.RESTAURANT
    p = Purchase(
        payment_method_id=payment_method_id,
        kind=PurchaseKind.INVENTORY,
        amount=total,
        supplier=(supplier or "").strip() or None,
        supplier_phone=(supplier_phone or "").strip() or None,
        supplier_invoice_ref=ref,
        invoice_image_filename=(invoice_image_filename or "").strip() or None,
        payment_proof_image_filename=(payment_proof_image_filename or "").strip()
        or None,
        note=(note or "").strip() or None,
        created_by_id=user_id,
        warehouse_id=wid,
        business_domain=dom,
    )
    if created_at is not None:
        p.created_at = created_at
    db.add(p)
    db.flush()
    if product_lines:
        p.receipt_batch_no = receipt_batch_no_for_purchase(p.id)
        db.flush()

    line_no = 0
    for ln in cleaned:
        qty = ln.quantity.quantize(Decimal("0.0001"))
        cost = ln.unit_cost.quantize(Decimal("0.001"))
        line_total = (qty * cost).quantize(Decimal("0.001"))
        if ln.kind == PurchaseLineKind.PRODUCT.value:
            line_no += 1
            product = products_by_id[ln.product_id]  # type: ignore[index]
            pl = PurchaseLine(
                purchase_id=p.id,
                product_id=ln.product_id,
                line_kind=PurchaseLineKind.PRODUCT.value,
                quantity=qty,
                unit_cost=cost,
                line_total=line_total,
            )
            db.add(pl)
            db.flush()
            dates = validate_lot_expiry(
                product,
                production_date=ln.production_date,
                expiry_date=ln.expiry_date,
            )
            create_lot_for_purchase_line(
                db,
                purchase=p,
                line=pl,
                product=product,
                warehouse_id=wid,  # type: ignore[arg-type]
                line_index=line_no,
                production_date=dates.production_date,
                expiry_date=dates.expiry_date,
            )
            apply_movement(
                db,
                product_id=ln.product_id,  # type: ignore[arg-type]
                quantity_delta=qty,
                movement_type=StockMovementType.PURCHASE,
                user_id=user_id,
                warehouse_id=wid,
                purchase_id=p.id,
                note=f"شراء {p.receipt_batch_no} — {pl.lot_code or f'#{p.id}'}",
            )
        else:
            pl = PurchaseLine(
                purchase_id=p.id,
                product_id=None,
                line_kind=ln.kind,
                item_name=ln.item_name,
                unit=ln.unit,
                quantity=qty,
                unit_cost=cost,
                line_total=line_total,
                useful_life_months=int(ln.useful_life_months or 0),
                salvage_value=Decimal(str(ln.salvage_value or 0)).quantize(
                    Decimal("0.001")
                ),
            )
            db.add(pl)
    db.flush()
    if is_supplier_credit_payment_method(pm):
        if pay_now_method_id and pay_now_amount and pay_now_amount > 0:
            pay_pm = assert_purchase_pay_wallet(
                db, pay_now_method_id, custody_only=purchase_custody_only
            )
            record_purchase_payment(
                db,
                purchase_id=p.id,
                payment_method_id=pay_pm.id,
                amount=pay_now_amount,
                user_id=user_id,
                payment_proof_image_filename=payment_proof_image_filename,
                purchase_custody_only=purchase_custody_only,
            )
    else:
        _apply_purchase_payment_at_create(
            db,
            p,
            pm,
            user_id=user_id,
            pay_amount=pay_now_amount,
            payment_proof_image_filename=payment_proof_image_filename,
            purchase_custody_only=purchase_custody_only,
        )
    from modules.dashboard_notify.constants import PURCHASES
    from modules.dashboard_notify.service import record_activity

    note_hint = supplier or note or f"شراء #{p.id}"
    if asset_lines and product_lines:
        note_hint = f"مختلطة — {note_hint}"
    elif asset_lines and not product_lines:
        note_hint = f"أصول/استهلاك — {note_hint}"
    record_activity(
        db,
        PURCHASES,
        event_type="inventory_purchase",
        ref_id=p.id,
        note=note_hint[:255],
    )
    from modules.gl.posting import post_inventory_purchase_shadow_safe

    post_inventory_purchase_shadow_safe(db, p)
    try:
        from modules.notifications.inventory_hooks import emit_purchase_received

        emit_purchase_received(db, p)
    except Exception:  # noqa: BLE001
        pass
    return p


def delete_purchase(db: Session, purchase_id: int) -> None:
    """يحذف عملية صرف. لو كانت INVENTORY: نخصم الكميات من المخزون مرة أخرى."""
    from modules.catalog.uploads import delete_stored_relative_file

    p = db.get(Purchase, purchase_id)
    if p is None:
        return
    static_root = Path(__file__).resolve().parents[2] / "app" / "static"
    delete_stored_relative_file(static_root, p.invoice_image_filename)
    delete_stored_relative_file(static_root, p.payment_proof_image_filename)
    if p.kind == PurchaseKind.INVENTORY:
        wid = p.warehouse_id
        from modules.inventory.lots import void_lots_for_purchase
        from modules.inventory.service import get_main_warehouse
        from modules.payments.cost_reference import is_cost_reference_purchase

        if wid is None:
            wid = get_main_warehouse(db).id
        void_lots_for_purchase(db, p.id)
        if not is_cost_reference_purchase(p):
            for ln in p.lines:
                if ln.product_id is None:
                    continue
                apply_movement(
                    db,
                    product_id=ln.product_id,
                    quantity_delta=-ln.quantity,
                    movement_type=StockMovementType.ADJUSTMENT,
                    user_id=None,
                    warehouse_id=wid,
                    note=f"إلغاء شراء فاتورة #{p.id}",
                )
    kind = p.kind
    pid = p.id
    db.delete(p)
    db.flush()
    from modules.dashboard_notify.constants import ASSETS, EXPENSES, PURCHASES
    from modules.dashboard_notify.service import resolve_activity

    if kind == PurchaseKind.EXPENSE:
        resolve_activity(db, EXPENSES, ref_id=pid)
    elif kind == PurchaseKind.INVENTORY:
        resolve_activity(db, PURCHASES, ref_id=pid)
    else:
        resolve_activity(db, ASSETS, ref_id=pid)


def get_sale_payment(db: Session, sale_id: int) -> SalePayment | None:
    from sqlalchemy.orm import selectinload

    return db.execute(
        select(SalePayment)
        .where(SalePayment.sale_id == sale_id)
        .options(selectinload(SalePayment.method))
        .order_by(SalePayment.created_at, SalePayment.id)
    ).scalars().first()


# --- Wallet calculations ---


@dataclass
class WalletRow:
    method: PaymentMethod
    sales_in: Decimal
    refunds_out: Decimal
    transfers_in: Decimal
    transfers_out: Decimal
    delivery_fees_out: Decimal
    purchases_out: Decimal
    net: Decimal


def wallet_breakdown(
    db: Session, start: datetime | None, end: datetime | None
) -> list[WalletRow]:
    """يحسب لكل أسلوب دفع: مدخل المبيعات، المرتجعات، التسويات، خرج المشتريات، الصافي خلال الفترة.
    إن كانت start/end None: حسبة كل الوقت."""
    from modules.delivery.models import DeliveryCashSettlement

    methods = list_payment_methods(db, only_active=False)

    sales_q = (
        select(SalePayment.payment_method_id, func.coalesce(func.sum(SalePayment.amount), 0))
        .group_by(SalePayment.payment_method_id)
    )
    if start is not None:
        sales_q = sales_q.where(SalePayment.created_at >= start)
    if end is not None:
        sales_q = sales_q.where(SalePayment.created_at < end)
    sales_map: dict[int, Decimal] = {
        int(mid): Decimal(str(s or 0)) for mid, s in db.execute(sales_q).all()
    }

    refunds_q = (
        select(
            RefundPayment.payment_method_id,
            func.coalesce(func.sum(RefundPayment.amount), 0),
        )
        .group_by(RefundPayment.payment_method_id)
    )
    if start is not None:
        refunds_q = refunds_q.where(RefundPayment.created_at >= start)
    if end is not None:
        refunds_q = refunds_q.where(RefundPayment.created_at < end)
    refunds_map: dict[int, Decimal] = {
        int(mid): Decimal(str(s or 0)) for mid, s in db.execute(refunds_q).all()
    }

    transfers_in_q = (
        select(
            PaymentTransfer.to_payment_method_id,
            func.coalesce(func.sum(PaymentTransfer.amount), 0),
        )
        .group_by(PaymentTransfer.to_payment_method_id)
    )
    if start is not None:
        transfers_in_q = transfers_in_q.where(PaymentTransfer.created_at >= start)
    if end is not None:
        transfers_in_q = transfers_in_q.where(PaymentTransfer.created_at < end)
    transfers_in_map: dict[int, Decimal] = {
        int(mid): Decimal(str(s or 0))
        for mid, s in db.execute(transfers_in_q).all()
    }

    transfers_out_q = (
        select(
            PaymentTransfer.from_payment_method_id,
            func.coalesce(func.sum(PaymentTransfer.amount), 0),
        )
        .group_by(PaymentTransfer.from_payment_method_id)
    )
    if start is not None:
        transfers_out_q = transfers_out_q.where(PaymentTransfer.created_at >= start)
    if end is not None:
        transfers_out_q = transfers_out_q.where(PaymentTransfer.created_at < end)
    transfers_out_map: dict[int, Decimal] = {
        int(mid): Decimal(str(s or 0))
        for mid, s in db.execute(transfers_out_q).all()
    }

    delivery_fees_q = (
        select(
            DeliveryCashSettlement.cash_method_id,
            func.coalesce(func.sum(DeliveryCashSettlement.amount), 0),
        )
        .group_by(DeliveryCashSettlement.cash_method_id)
    )
    if start is not None:
        delivery_fees_q = delivery_fees_q.where(DeliveryCashSettlement.created_at >= start)
    if end is not None:
        delivery_fees_q = delivery_fees_q.where(DeliveryCashSettlement.created_at < end)
    delivery_fees_map: dict[int, Decimal] = {
        int(mid): Decimal(str(s or 0)) for mid, s in db.execute(delivery_fees_q).all()
    }

    purchases_q = select(
        PurchasePayment.payment_method_id,
        func.coalesce(func.sum(PurchasePayment.amount), 0),
    ).group_by(PurchasePayment.payment_method_id)
    if start is not None:
        purchases_q = purchases_q.where(PurchasePayment.created_at >= start)
    if end is not None:
        purchases_q = purchases_q.where(PurchasePayment.created_at < end)
    purchases_map: dict[int, Decimal] = {
        int(mid): Decimal(str(s or 0)) for mid, s in db.execute(purchases_q).all()
    }

    from modules.hotel.booking_models import (
        HotelBookingPayment,
        HotelBookingPaymentRefund,
    )

    hotel_payments_q = (
        select(
            HotelBookingPayment.payment_method_id,
            func.coalesce(func.sum(HotelBookingPayment.amount), 0),
        )
        .where(HotelBookingPayment.payment_method_id.isnot(None))
        .group_by(HotelBookingPayment.payment_method_id)
    )
    if start is not None:
        hotel_payments_q = hotel_payments_q.where(HotelBookingPayment.created_at >= start)
    if end is not None:
        hotel_payments_q = hotel_payments_q.where(HotelBookingPayment.created_at < end)
    hotel_payments_map: dict[int, Decimal] = {
        int(mid): Decimal(str(s or 0)) for mid, s in db.execute(hotel_payments_q).all()
    }

    hotel_refund_pm = func.coalesce(
        HotelBookingPaymentRefund.payment_method_id,
        HotelBookingPayment.payment_method_id,
    )
    hotel_refunds_q = (
        select(
            hotel_refund_pm,
            func.coalesce(func.sum(HotelBookingPaymentRefund.amount), 0),
        )
        .join(
            HotelBookingPayment,
            HotelBookingPayment.id == HotelBookingPaymentRefund.payment_id,
        )
        .where(hotel_refund_pm.isnot(None))
        .group_by(hotel_refund_pm)
    )
    if start is not None:
        hotel_refunds_q = hotel_refunds_q.where(HotelBookingPaymentRefund.created_at >= start)
    if end is not None:
        hotel_refunds_q = hotel_refunds_q.where(HotelBookingPaymentRefund.created_at < end)
    hotel_refunds_map: dict[int, Decimal] = {
        int(mid): Decimal(str(s or 0)) for mid, s in db.execute(hotel_refunds_q).all()
    }

    rows: list[WalletRow] = []
    for m in methods:
        sin = sales_map.get(m.id, Decimal("0")) + hotel_payments_map.get(m.id, Decimal("0"))
        rout = refunds_map.get(m.id, Decimal("0")) + hotel_refunds_map.get(m.id, Decimal("0"))
        tin = transfers_in_map.get(m.id, Decimal("0"))
        tout = transfers_out_map.get(m.id, Decimal("0"))
        dfout = delivery_fees_map.get(m.id, Decimal("0"))
        pout = purchases_map.get(m.id, Decimal("0"))
        rows.append(
            WalletRow(
                method=m,
                sales_in=sin,
                refunds_out=rout,
                transfers_in=tin,
                transfers_out=tout,
                delivery_fees_out=dfout,
                purchases_out=pout,
                net=(sin + tin - rout - tout - dfout - pout).quantize(Decimal("0.001")),
            )
        )
    return rows


@dataclass
class PurchasesSummary:
    count: int
    total: Decimal


def _purchases_summary_q(
    db: Session, start: datetime, end: datetime, kind: PurchaseKind | None
) -> PurchasesSummary:
    stmt = select(
        func.count(Purchase.id), func.coalesce(func.sum(Purchase.amount), 0)
    ).where(Purchase.created_at >= start, Purchase.created_at < end)
    if kind is not None:
        stmt = stmt.where(Purchase.kind == kind)
    cnt, total = db.execute(stmt).one()
    return PurchasesSummary(count=int(cnt or 0), total=Decimal(str(total or 0)))


def purchases_summary(
    db: Session, start: datetime, end: datetime
) -> PurchasesSummary:
    """ملخص جميع عمليات الصرف (شراء + مصروف)."""
    return _purchases_summary_q(db, start, end, None)


def inventory_purchases_summary(
    db: Session, start: datetime, end: datetime
) -> PurchasesSummary:
    return _purchases_summary_q(db, start, end, PurchaseKind.INVENTORY)


def expenses_summary(
    db: Session, start: datetime, end: datetime
) -> PurchasesSummary:
    return _purchases_summary_q(db, start, end, PurchaseKind.EXPENSE)


def list_purchases(
    db: Session,
    start: datetime,
    end: datetime,
    kind: PurchaseKind | None = None,
    limit: int = 500,
    domain=None,
    *,
    exclude_loyalty: bool = False,
) -> list[Purchase]:
    from modules.platform.business_domain import purchase_domain_db_values

    stmt = (
        select(Purchase)
        .where(Purchase.created_at >= start, Purchase.created_at < end)
        .order_by(Purchase.id.desc())
        .limit(limit)
    )
    if kind is not None:
        stmt = stmt.where(Purchase.kind == kind)
    if exclude_loyalty:
        from modules.pos_shifts.loyalty_settlement import (
            LOYALTY_OPERATING_EXPENSE_CATEGORY,
            LOYALTY_OPERATING_EXPENSE_SUPPLIER,
            LOYALTY_SHIFT_EXPENSE_REF_PREFIX,
        )

        stmt = stmt.where(
            or_(
                Purchase.expense_category.is_(None),
                Purchase.expense_category != LOYALTY_OPERATING_EXPENSE_CATEGORY,
            ),
            or_(
                Purchase.supplier.is_(None),
                Purchase.supplier != LOYALTY_OPERATING_EXPENSE_SUPPLIER,
            ),
            or_(
                Purchase.supplier_invoice_ref.is_(None),
                ~Purchase.supplier_invoice_ref.like(f"{LOYALTY_SHIFT_EXPENSE_REF_PREFIX}%"),
            ),
        )
    domain_vals = purchase_domain_db_values(domain)
    if domain_vals is not None:
        stmt = stmt.where(Purchase.business_domain.in_(domain_vals))
    return list(db.scalars(stmt).all())
