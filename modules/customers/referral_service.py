"""كود الإحالة ومنح النقاط للمحيل والمشتري."""
from __future__ import annotations

import secrets

from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from modules.customers.models import Customer, LoyaltyTransaction, LoyaltyTxnKind, ReferralEvent
from modules.customers.service import CustomersError, loyalty_settings
from modules.settings.service import get_setting, set_setting


class ReferralError(Exception):
    pass


def referral_settings(db: Session, domain=None) -> dict:
    from modules.platform.domain_loyalty import referral_settings_for_domain

    return referral_settings_for_domain(db, domain)


def _max_uses_per_code_per_buyer(db: Session) -> int:
    """0 = بلا حد — كل زبون يستطيع استخدام نفس الكود بلا قيد."""
    raw = (get_setting(db, "referral_max_uses_per_code", "") or "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            return 1
    # ترحيل من الإعداد القديم (إحالة واحدة لكل زبون)
    legacy = (get_setting(db, "referral_first_sale_only", "") or "").strip()
    if legacy == "0":
        return 0
    return 1


def _max_uses_per_code_display(db: Session) -> str:
    return str(_max_uses_per_code_per_buyer(db))


def save_referral_settings(
    db: Session,
    *,
    enabled: bool,
    referrer_points: str,
    buyer_points: str,
    min_sale_total: str,
    max_uses_per_code_per_buyer: str,
    domain=None,
) -> None:
    from modules.platform.domain_loyalty import save_referral_settings_for_domain

    save_referral_settings_for_domain(
        db,
        domain or "restaurant",
        enabled=enabled,
        referrer_points=referrer_points,
        buyer_points=buyer_points,
        min_sale_total=min_sale_total,
        max_uses_per_code=max_uses_per_code_per_buyer,
    )


def _generate_referral_code() -> str:
    """كود إحالة رقمي فقط (6 أرقام)."""
    return f"{secrets.randbelow(900_000) + 100_000:06d}"


def normalize_referral_code_input(code: str) -> str:
    """يُطبّع إدخال الكود: أرقام فقط للأكواد الجديدة، أو الحروف للأكواد القديمة."""
    raw = (code or "").strip()
    if not raw:
        return ""
    if raw.upper().startswith("RF-"):
        return raw.upper()
    compact = raw.replace(" ", "").replace("-", "")
    if compact.isdigit():
        return compact
    return raw.upper()


def ensure_referral_code(db: Session, customer: Customer, *, notify: bool = True) -> str:
    if customer.referral_code:
        return customer.referral_code
    for _ in range(30):
        code = _generate_referral_code()
        exists = db.scalar(
            select(func.count(Customer.id)).where(Customer.referral_code == code)
        )
        if not int(exists or 0):
            customer.referral_code = code
            db.flush()
            if notify:
                try:
                    from modules.notifications.marketing_hooks import emit_referral_link_created

                    emit_referral_link_created(db, customer, code=code)
                except Exception:  # noqa: BLE001
                    pass
            return code
    raise ReferralError("تعذّر إنشاء كود إحالة.")


def validate_manual_referral_code(raw: str) -> str:
    """كود يدوي للمشاهير — 6 أرقام بالضبط."""
    code = (raw or "").strip().replace(" ", "").replace("-", "")
    if len(code) != 6 or not code.isdigit():
        raise ReferralError("كود الإحالة يجب أن يكون 6 أرقام فقط (مثل 123456).")
    return code


def set_customer_referral_code(db: Session, customer_id: int, code: str) -> str:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise ReferralError("العميل غير موجود.")
    if referral_code_is_locked(db, customer):
        raise ReferralError(
            "لا يمكن تغيير الكود — تم استخدامه في إحالة سابقة."
        )
    new_code = validate_manual_referral_code(code)
    if (customer.referral_code or "").strip() == new_code:
        return new_code
    taken = db.scalar(
        select(func.count(Customer.id)).where(
            Customer.referral_code == new_code,
            Customer.id != customer_id,
        )
    )
    if int(taken or 0):
        raise ReferralError("هذا الكود مستخدم لعميل آخر — اختر رقماً مختلفاً.")
    customer.referral_code = new_code
    db.flush()
    return new_code


def referral_code_usage_count(db: Session, customer_id: int) -> int:
    """عدد الإحالات الناجحة التي استخدمت كود هذا العميل."""
    return int(
        db.scalar(
            select(func.count(ReferralEvent.id)).where(
                ReferralEvent.referrer_customer_id == customer_id
            )
        )
        or 0
    )


def referral_code_is_locked(db: Session, customer: Customer) -> bool:
    return referral_code_usage_count(db, customer.id) > 0


def ensure_six_digit_referral_code(db: Session, customer: Customer) -> str:
    """يضمن كوداً رقمياً من 6 خانات — يُولَّد تلقائياً إن لم يوجد أو كان قديماً."""
    if referral_code_is_locked(db, customer):
        return (customer.referral_code or "").strip()
    code = (customer.referral_code or "").strip()
    if len(code) == 6 and code.isdigit():
        return code
    if not code:
        return ensure_referral_code(db, customer)
    for _ in range(30):
        new_code = _generate_referral_code()
        taken = db.scalar(
            select(func.count(Customer.id)).where(
                Customer.referral_code == new_code,
                Customer.id != customer.id,
            )
        )
        if not int(taken or 0):
            customer.referral_code = new_code
            db.flush()
            return new_code
    raise ReferralError("تعذّر إنشاء كود إحالة.")


def find_customer_by_referral_code(db: Session, code: str) -> Customer | None:
    raw = (code or "").strip()
    if not raw:
        return None
    norm = normalize_referral_code_input(raw)
    clauses = []
    if norm:
        clauses.append(Customer.referral_code == norm)
    legacy = raw.upper()
    if legacy and legacy != norm:
        clauses.append(Customer.referral_code == legacy)
    if not clauses:
        return None
    return db.scalars(
        select(Customer).where(
            or_(*clauses),
            Customer.is_active.is_(True),
        )
    ).first()


def _customer_ids_for_buyer_phone(db: Session, buyer: Customer) -> list[int]:
    """كل سجلات العملاء المرتبطة بنفس رقم الهاتف (صيغ مخزّنة قديمة وجديدة)."""
    from modules.customers.service import _phone_storage_variants, normalize_phone

    if not (buyer.phone or "").strip():
        return [buyer.id]
    variants = _phone_storage_variants(normalize_phone(buyer.phone))
    ids = list(
        db.scalars(select(Customer.id).where(Customer.phone.in_(variants))).all()
    )
    return ids if ids else [buyer.id]


def buyer_can_use_referral(
    db: Session, buyer: Customer, *, exclude_sale_id: int | None = None
) -> bool:
    """هل يحق لهذا الهاتف/العميل استخدام كود إحالة (مرة واحدة في المجمل)؟"""
    buyer_ids = _customer_ids_for_buyer_phone(db, buyer)
    used_before = db.scalars(
        select(ReferralEvent.id)
        .where(ReferralEvent.buyer_customer_id.in_(buyer_ids))
        .limit(1)
    ).first()
    if used_before is not None:
        return False
    from modules.sales.models import Sale

    sale_q = select(Sale.id).where(
        Sale.customer_id.in_(buyer_ids),
        Sale.referrer_customer_id.isnot(None),
    )
    if exclude_sale_id is not None:
        sale_q = sale_q.where(Sale.id != exclude_sale_id)
    pending = db.scalars(sale_q.limit(1)).first()
    return pending is None


def buyer_referral_code_uses(
    db: Session, buyer_id: int, referral_code: str
) -> int:
    """عدد مرات استخدام زبون لكود إحالة محدد (فواتير مكتملة)."""
    code = normalize_referral_code_input(referral_code)
    if not code:
        return 0
    n = db.scalar(
        select(func.count(ReferralEvent.id)).where(
            ReferralEvent.buyer_customer_id == buyer_id,
            ReferralEvent.referral_code == code,
        )
    )
    return int(n or 0)


def apply_referral_to_sale(
    db: Session,
    sale,
    *,
    code: str,
    buyer: Customer,
) -> Customer:
    """يربط المحيل بالفاتورة قبل الإتمام — المنح بعد الدفع."""
    cfg = referral_settings(db)
    if not cfg["referral_enabled"]:
        raise ReferralError("نظام الإحالة غير مفعّل.")
    referrer = find_customer_by_referral_code(db, code)
    if referrer is None:
        raise ReferralError("كود الإحالة غير صالح.")
    if referrer.id == buyer.id:
        raise ReferralError("لا يمكن استخدام كودك الخاص.")
    if not buyer_can_use_referral(db, buyer, exclude_sale_id=sale.id):
        raise ReferralError(
            "رقم الهاتف هذا استخدم كود إحالة سابقاً — "
            "مرة واحدة فقط لكل زبون."
        )
    ref_code = (referrer.referral_code or "").strip()
    max_uses = cfg["max_uses_per_code_per_buyer"]
    if max_uses > 0:
        uses = buyer_referral_code_uses(db, buyer.id, ref_code)
        if uses >= max_uses:
            raise ReferralError(
                f"تجاوزت عدد مرات استخدام هذا الكود (الحد: {max_uses})."
            )
    sale.referral_code_used = ref_code
    sale.referrer_customer_id = referrer.id
    db.flush()
    return referrer


def grant_referral_after_sale(
    db: Session,
    *,
    sale_id: int,
    paid_total: Decimal,
    user_id: int | None,
) -> ReferralEvent | None:
    from modules.sales.models import Sale

    sale = db.get(Sale, sale_id)
    if sale is None or not sale.referrer_customer_id or not sale.customer_id:
        return None
    existing = db.scalars(
        select(ReferralEvent).where(ReferralEvent.sale_id == sale_id)
    ).first()
    if existing is not None:
        return existing

    cfg = referral_settings(db)
    if not cfg["referral_enabled"]:
        return None
    if paid_total < cfg["min_sale_total"]:
        return None

    referrer = db.get(Customer, sale.referrer_customer_id)
    buyer = db.get(Customer, sale.customer_id)
    if referrer is None or buyer is None:
        return None

    ref_pts = cfg["referrer_points"].quantize(Decimal("0.001"))
    buy_pts = cfg["buyer_points"].quantize(Decimal("0.001"))

    if ref_pts > 0:
        referrer.points_balance = Decimal(referrer.points_balance or 0) + ref_pts
        db.add(
            LoyaltyTransaction(
                customer_id=referrer.id,
                sale_id=sale_id,
                kind=LoyaltyTxnKind.EARN,
                points=ref_pts,
                note=f"إحالة — فاتورة #{sale_id}",
                created_by_id=user_id,
            )
        )
    if buy_pts > 0:
        buyer.points_balance = Decimal(buyer.points_balance or 0) + buy_pts
        db.add(
            LoyaltyTransaction(
                customer_id=buyer.id,
                sale_id=sale_id,
                kind=LoyaltyTxnKind.EARN,
                points=buy_pts,
                note=f"مكافأة إحالة — فاتورة #{sale_id}",
                created_by_id=user_id,
            )
        )

    ev = ReferralEvent(
        referrer_customer_id=referrer.id,
        buyer_customer_id=buyer.id,
        sale_id=sale_id,
        referral_code=sale.referral_code_used or referrer.referral_code or "",
        referrer_points=ref_pts,
        buyer_points=buy_pts,
    )
    db.add(ev)
    db.flush()
    try:
        from modules.notifications.marketing_hooks import (
            emit_referral_first_order_completed,
            emit_referral_referrer_rewarded,
        )

        ensure_referral_code(db, buyer, notify=False)

        emit_referral_first_order_completed(
            db,
            referrer=referrer,
            buyer=buyer,
            sale_id=sale_id,
            referral_code=ev.referral_code or "",
            referrer_points=ref_pts,
            buyer_points=buy_pts,
        )
        if ref_pts > 0:
            emit_referral_referrer_rewarded(
                db,
                referrer=referrer,
                buyer=buyer,
                sale_id=sale_id,
                referral_code=ev.referral_code or "",
                referrer_points=ref_pts,
            )
    except Exception:  # noqa: BLE001
        pass
    return ev
