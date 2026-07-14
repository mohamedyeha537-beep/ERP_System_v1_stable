"""Customers & loyalty business logic."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from modules.customers.models import (
    Customer,
    LoyaltyTransaction,
    LoyaltyTxnKind,
)
from modules.settings.service import get_setting


class CustomersError(Exception):
    """خطأ في إدارة العملاء/الولاء."""


# ============================================================
# Phone normalization
# ============================================================

_DIGITS_RE = re.compile(r"\D")


def normalize_phone(raw: str | None) -> str:
    """يُوحّد رقم الجوال الليبي إلى 10 أرقام محلية (09xxxxxxxx).

    يقبل إدخالاً بمفتاح +218 أو 218 ثم يحوّله للصيغة المحلية حتى لا يُنشأ عميل مكرر.
    """
    if raw is None:
        return ""
    digits = _DIGITS_RE.sub("", str(raw).strip())
    if not digits:
        return ""
    if digits.startswith("00218"):
        digits = digits[5:]
    elif digits.startswith("218"):
        digits = digits[3:]
    if len(digits) == 9 and digits.startswith("9"):
        digits = "0" + digits
    return digits


def require_valid_phone(raw: str | None) -> str:
    """رقم هاتف صالح للتخزين — 10 أرقام محلية بدون مفتاح دولة."""
    p = normalize_phone(raw)
    if not p:
        raise CustomersError("رقم الهاتف مطلوب.")
    if len(p) != 10:
        raise CustomersError(
            "رقم الهاتف يجب أن يكون 10 أرقام فقط (مثل 0917122552) "
            "بدون إدخال +218 أو 218."
        )
    if not p.startswith("09"):
        raise CustomersError("رقم الجوال الليبي يبدأ عادةً بـ 09 (10 أرقام).")
    return p


def _phone_storage_variants(canonical: str) -> list[str]:
    """صيغ قديمة مخزّنة في قاعدة البيانات قبل توحيد الأرقام."""
    if len(canonical) != 10:
        return [canonical]
    body = canonical[1:]
    return [canonical, f"+218{body}", f"218{body}", body]


def _migrate_phone_to_canonical(db: Session, customer: Customer, canonical: str) -> None:
    if customer.phone == canonical:
        return
    conflict = db.scalar(
        select(Customer.id).where(
            Customer.phone == canonical, Customer.id != customer.id
        )
    )
    if conflict is None:
        customer.phone = canonical
        db.flush()


# ============================================================
# CRUD
# ============================================================

def list_customers(
    db: Session,
    *,
    only_active: bool = False,
    search: str | None = None,
    customer_type: str | None = None,
    limit: int = 500,
) -> list[Customer]:
    from modules.customers.models import CustomerType

    stmt = select(Customer)
    if only_active:
        stmt = stmt.where(Customer.is_active.is_(True))
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(
            (Customer.phone.like(like))
            | (Customer.name.like(like))
            | (Customer.company_name.like(like))
        )
    if customer_type:
        try:
            ct = CustomerType(customer_type.strip().upper())
            stmt = stmt.where(Customer.customer_type == ct)
        except ValueError:
            pass
    stmt = stmt.order_by(Customer.last_visit_at.is_(None), Customer.last_visit_at.desc(), Customer.id.desc()).limit(limit)
    return list(db.scalars(stmt))


def get_customer(db: Session, customer_id: int) -> Customer | None:
    return db.get(Customer, customer_id)


def _customer_by_phone_row(
    db: Session, phone_value: str, *, active_only: bool
) -> Customer | None:
    stmt = select(Customer).where(Customer.phone == phone_value)
    if active_only:
        stmt = stmt.where(Customer.is_active.is_(True))
    return db.scalar(stmt.limit(1))


def get_by_phone(
    db: Session, phone: str, *, active_only: bool = True
) -> Customer | None:
    """يبحث عن عميل بالهاتف (النشطون فقط في نقطة البيع افتراضياً)."""
    p = normalize_phone(phone)
    if not p:
        return None
    found = _customer_by_phone_row(db, p, active_only=active_only)
    if found is not None:
        return found
    if len(p) == 10:
        for alt in _phone_storage_variants(p):
            if alt == p:
                continue
            legacy = _customer_by_phone_row(db, alt, active_only=active_only)
            if legacy is not None:
                _migrate_phone_to_canonical(db, legacy, p)
                return _customer_by_phone_row(db, p, active_only=active_only)
    return None


def get_by_phone_any(db: Session, phone: str) -> Customer | None:
    return get_by_phone(db, phone, active_only=False)


def create_customer(
    db: Session,
    *,
    phone: str,
    name: str | None = None,
    email: str | None = None,
    notes: str | None = None,
    customer_type: str | None = None,
    company_name: str | None = None,
) -> Customer:
    from modules.customers.models import CustomerType

    p = require_valid_phone(phone)
    existing = get_by_phone_any(db, p)
    if existing is not None:
        if existing.is_active:
            raise CustomersError(f"رقم الهاتف {p} مسجَّل بالفعل.")
        raise CustomersError(
            f"رقم الهاتف {p} لعميل مؤرشف (#{existing.id}). "
            "فعّله من «عرض المؤرشفين» بدلاً من إنشاء سجل جديد."
        )
    ct = CustomerType.INDIVIDUAL
    if customer_type:
        try:
            ct = CustomerType(customer_type.strip().upper())
        except ValueError:
            ct = CustomerType.INDIVIDUAL
    c = Customer(
        phone=p,
        name=(name or "").strip() or None,
        email=(email or "").strip() or None,
        notes=(notes or "").strip() or None,
        customer_type=ct,
        company_name=(company_name or "").strip() or None,
        is_active=True,
    )
    db.add(c)
    db.flush()
    from modules.customers.referral_service import ensure_referral_code

    ensure_referral_code(db, c, notify=False)
    return c


def get_or_create_by_phone(
    db: Session,
    *,
    phone: str,
    name: str | None = None,
) -> Customer:
    """يبحث عن عميل برقم الهاتف، فإن لم يوجد ينشئه."""
    p = require_valid_phone(phone)
    existing = get_by_phone_any(db, p)
    if existing is not None:
        if not existing.is_active:
            existing.is_active = True
        if not existing.name and (name or "").strip():
            existing.name = name.strip()
        db.flush()
        return existing
    return create_customer(db, phone=p, name=name)


def update_customer(
    db: Session,
    customer_id: int,
    *,
    phone: str | None = None,
    name: str | None = None,
    email: str | None = None,
    notes: str | None = None,
    is_active: bool | None = None,
    customer_type: str | None = None,
    company_name: str | None = None,
) -> Customer:
    from modules.customers.models import CustomerType

    c = db.get(Customer, customer_id)
    if c is None:
        raise CustomersError("العميل غير موجود.")
    if phone is not None:
        p = require_valid_phone(phone)
        existing = get_by_phone_any(db, p)
        if existing is not None and existing.id != customer_id:
            raise CustomersError(f"رقم الهاتف {p} مسجَّل لعميل آخر.")
        c.phone = p
    if name is not None:
        c.name = name.strip() or None
    if email is not None:
        c.email = email.strip() or None
    if notes is not None:
        c.notes = notes.strip() or None
    if is_active is not None:
        c.is_active = is_active
    if customer_type is not None:
        try:
            c.customer_type = CustomerType(customer_type.strip().upper())
        except ValueError:
            pass
    if company_name is not None:
        c.company_name = company_name.strip() or None
    db.flush()
    return c


def adjust_wallet(
    db: Session,
    customer_id: int,
    *,
    amount: Decimal,
    note: str = "",
    user_id: int | None = None,
) -> Customer:
    from modules.customers.models import CustomerWalletTransaction, WalletTxnKind

    c = db.get(Customer, customer_id)
    if c is None:
        raise CustomersError("العميل غير موجود.")
    delta = Decimal(str(amount)).quantize(Decimal("0.001"))
    if delta == 0:
        raise CustomersError("المبلغ صفر.")
    new_bal = Decimal(str(c.wallet_balance or 0)) + delta
    if new_bal < 0:
        raise CustomersError("رصيد المحفظة لا يمكن أن يكون سالباً.")
    c.wallet_balance = new_bal.quantize(Decimal("0.001"))
    kind = WalletTxnKind.TOPUP if delta > 0 else WalletTxnKind.SPEND
    if (note or "").strip().lower().startswith("تعديل"):
        kind = WalletTxnKind.ADJUST
    db.add(
        CustomerWalletTransaction(
            customer_id=customer_id,
            kind=kind,
            amount=delta,
            note=(note or "").strip() or None,
            created_by_id=user_id,
        )
    )
    db.flush()
    return c


def list_wallet_transactions(db: Session, customer_id: int, *, limit: int = 100):
    from modules.customers.models import CustomerWalletTransaction

    return list(
        db.scalars(
            select(CustomerWalletTransaction)
            .where(CustomerWalletTransaction.customer_id == customer_id)
            .order_by(CustomerWalletTransaction.id.desc())
            .limit(limit)
        ).all()
    )


def archive_customer(db: Session, customer_id: int) -> Customer:
    """أرشفة: يختفي من القائمة اليومية لكن تبقى الفواتير والنقاط في السجل."""
    c = db.get(Customer, customer_id)
    if c is None:
        raise CustomersError("العميل غير موجود.")
    c.is_active = False
    db.flush()
    return c


def restore_customer(db: Session, customer_id: int) -> Customer:
    c = db.get(Customer, customer_id)
    if c is None:
        raise CustomersError("العميل غير موجود.")
    conflict = get_by_phone(db, c.phone)
    if conflict is not None and conflict.id != c.id:
        raise CustomersError(
            f"لا يمكن التفعيل: الرقم {c.phone} مستخدم لعميل نشط آخر (#{conflict.id})."
        )
    c.is_active = True
    db.flush()
    return c


def purge_customer(db: Session, customer_id: int) -> None:
    """حذف نهائي: يفك ارتباط الفواتير ويحذف سجل النقاط ثم يزيل العميل."""
    from modules.sales.models import Sale

    c = db.get(Customer, customer_id)
    if c is None:
        return
    db.execute(
        update(Sale).where(Sale.customer_id == customer_id).values(customer_id=None)
    )
    db.execute(
        delete(LoyaltyTransaction).where(
            LoyaltyTransaction.customer_id == customer_id
        )
    )
    db.delete(c)
    db.flush()


def delete_customer(db: Session, customer_id: int) -> None:
    """حذف بسيط إن لم يكن للعميل أي سجل نقاط؛ وإلا يُطلب الأرشفة أو الحذف النهائي."""
    c = db.get(Customer, customer_id)
    if c is None:
        return
    txn_count = db.scalar(
        select(func.count(LoyaltyTransaction.id)).where(
            LoyaltyTransaction.customer_id == customer_id
        )
    )
    if (txn_count or 0) > 0:
        raise CustomersError(
            "لا يمكن الحذف البسيط: للعميل سجل نقاط أو فواتير. "
            "استخدم «أرشفة» لإخفائه من القائمة، أو «حذف نهائي» بعد التأكيد."
        )
    purge_customer(db, customer_id)


# ============================================================
# Loyalty engine
# ============================================================

def _setting_decimal(db: Session, key: str, default: str) -> Decimal:
    val = get_setting(db, key, default)
    try:
        return Decimal(str(val).strip() or default)
    except Exception:  # noqa: BLE001
        return Decimal(default)


def _setting_bool(db: Session, key: str, default: bool) -> bool:
    val = get_setting(db, key, "1" if default else "0")
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def loyalty_settings(
    db: Session, domain=None
) -> dict:
    """يقرأ إعدادات الولاء — افتراضياً المطعم/نقطة البيع."""
    from modules.platform.domain_loyalty import loyalty_settings_for_domain

    return loyalty_settings_for_domain(db, domain)


def _max_redeem_percent_of_sale(db: Session) -> Decimal:
    """نسبة قيمة الفاتورة القصوى التي يُسمح خصمها بالنقاط (0 = معطّل، 100 = كامل الفاتورة)."""
    raw = (get_setting(db, "loyalty_max_redeem_percent", "") or "").strip()
    if not raw:
        return Decimal("0")
    try:
        pct = Decimal(raw)
    except Exception:
        return Decimal("0")
    if pct < 0:
        return Decimal("0")
    if pct > 100:
        return Decimal("100")
    return pct


def points_to_dinars(db: Session, points: Decimal, domain=None) -> Decimal:
    """يحوّل النقاط إلى قيمة دينار حسب إعدادات المجال."""
    from modules.platform.domain_loyalty import points_to_dinars_for_domain

    return points_to_dinars_for_domain(db, points, domain)


def grant_points_for_sale(
    db: Session,
    *,
    customer: Customer,
    sale_id: int | None,
    sale_total: Decimal,
    user_id: int | None = None,
    skip_notification: bool = False,
    domain=None,
) -> Decimal:
    """يضيف نقاط ولاء للعميل بناءً على إجمالي الفاتورة + يحدّث إحصاءاته.

    يعيد عدد النقاط الممنوحة (قد يكون صفر لو الولاء معطّل).
    """
    s = loyalty_settings(db, domain)
    if not s["enabled"]:
        # نحدّث الإحصاءات حتى مع تعطيل الولاء
        _bump_visit_stats(customer, sale_total)
        db.flush()
        return Decimal("0")

    earn_rate = s["earn_per_dinar"]
    points = (Decimal(sale_total) * earn_rate).quantize(Decimal("0.001"))
    if points <= 0:
        _bump_visit_stats(customer, sale_total)
        db.flush()
        return Decimal("0")

    customer.points_balance = (
        Decimal(customer.points_balance or 0) + points
    )
    _bump_visit_stats(customer, sale_total)

    txn = LoyaltyTransaction(
        customer_id=customer.id,
        sale_id=sale_id if sale_id else None,
        kind=LoyaltyTxnKind.EARN,
        points=points,
        note=f"كسب من فاتورة #{sale_id}" if sale_id else "كسب من فاتورة",
        created_by_id=user_id,
    )
    db.add(txn)
    db.flush()
    if not skip_notification:
        try:
            from modules.notifications.hooks import emit_loyalty_points_earned

            emit_loyalty_points_earned(
                db, customer=customer, sale_id=sale_id, points=points
            )
        except Exception:  # noqa: BLE001
            pass
    return points


def attach_customer_to_sale(
    db: Session,
    sale,
    *,
    phone: str | None = None,
    name: str | None = None,
) -> Customer | None:
    """يربط عميلاً بالفاتورة (إنشاء أو بحث بالهاتف). يعيد None إن لم يُدخل هاتف."""
    p = normalize_phone(phone or "")
    nm = (name or "").strip()
    if p:
        cust = get_or_create_by_phone(db, phone=p, name=nm or None)
        sale.customer_id = cust.id
        if nm and not (cust.name or "").strip():
            cust.name = nm
            db.flush()
        return cust
    if sale.customer_id:
        return get_customer(db, int(sale.customer_id))
    return None


def loyalty_redeem_quote(
    db: Session, *, customer: Customer, sale_total: Decimal, domain=None
) -> dict:
    """معاينة أقصى خصم ممكن بالنقاط على فاتورة."""
    from modules.platform.domain_loyalty import loyalty_redeem_quote_for_domain

    return loyalty_redeem_quote_for_domain(
        db, customer=customer, sale_total=sale_total, domain=domain
    )


def redeem_points_for_sale(
    db: Session,
    *,
    customer: Customer,
    sale_id: int,
    sale_total: Decimal,
    points_to_use: Decimal | None = None,
    user_id: int | None = None,
    domain=None,
) -> tuple[Decimal, Decimal]:
    """يخصم نقاط العميل ويعيد (النقاط المستخدمة، قيمة الخصم بالدينار)."""
    quote = loyalty_redeem_quote(
        db, customer=customer, sale_total=sale_total, domain=domain
    )
    if not quote["can_redeem"]:
        raise CustomersError(
            "لا يمكن استخدام النقاط — الولاء معطّل، أو النسبة غير مفعّلة، "
            "أو الرصيد أقل من الحد الأدنى."
        )
    pts = (
        Decimal(str(points_to_use))
        if points_to_use is not None
        else quote["max_points"]
    ).quantize(Decimal("0.001"), rounding=ROUND_DOWN)
    if pts <= 0:
        raise CustomersError("حدد عدد نقاط للاستخدام.")
    if pts > quote["max_points"]:
        pts = quote["max_points"]
    if pts < quote["min_points"]:
        raise CustomersError(
            f"الحد الأدنى لاستخدام النقاط هو {quote['min_points']} نقطة."
        )
    discount = points_to_dinars(db, pts, domain)
    new_balance = Decimal(str(customer.points_balance or 0)) - pts
    if new_balance < 0:
        raise CustomersError("رصيد النقاط غير كافٍ.")
    customer.points_balance = new_balance
    txn = LoyaltyTransaction(
        customer_id=customer.id,
        sale_id=sale_id,
        kind=LoyaltyTxnKind.REDEEM,
        points=-pts,
        note=f"خصم نقاط ولاء — فاتورة #{sale_id}",
        created_by_id=user_id,
    )
    db.add(txn)
    db.flush()
    return pts, discount


def loyalty_earn_exists_for_sale(db: Session, sale_id: int) -> bool:
    """هل سبق منح نقاط كسب لهذه الفاتورة؟"""
    n = db.scalar(
        select(func.count(LoyaltyTransaction.id)).where(
            LoyaltyTransaction.sale_id == sale_id,
            LoyaltyTransaction.kind == LoyaltyTxnKind.EARN,
        )
    )
    return int(n or 0) > 0


def points_earned_on_sale(
    db: Session, sale_id: int, customer_id: int | None
) -> Decimal:
    """إجمالي نقاط الكسب المرتبطة بفاتورة لعميل محدد."""
    if not customer_id:
        return Decimal("0")
    total = db.execute(
        select(func.coalesce(func.sum(LoyaltyTransaction.points), 0)).where(
            LoyaltyTransaction.customer_id == customer_id,
            LoyaltyTransaction.sale_id == sale_id,
            LoyaltyTransaction.kind == LoyaltyTxnKind.EARN,
        )
    ).scalar_one()
    return Decimal(str(total or 0)).quantize(Decimal("0.001"))


def points_redeemed_on_sale(
    db: Session, sale_id: int, customer_id: int | None
) -> Decimal:
    """نقاط الاستخدام (موجبة) المرتبطة بفاتورة."""
    if not customer_id:
        return Decimal("0")
    total = db.execute(
        select(func.coalesce(func.sum(LoyaltyTransaction.points), 0)).where(
            LoyaltyTransaction.customer_id == customer_id,
            LoyaltyTransaction.sale_id == sale_id,
            LoyaltyTransaction.kind == LoyaltyTxnKind.REDEEM,
        )
    ).scalar_one()
    pts = abs(Decimal(str(total or 0))).quantize(Decimal("0.001"))
    return pts


def format_loyalty_points_display(pts: Decimal) -> str:
    dec = Decimal(str(pts or 0)).quantize(Decimal("0.001"))
    text = format(dec.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def loyalty_discount_for_sale(
    db: Session, sale_id: int, customer_id: int | None
) -> Decimal:
    """قيمة خصم نقاط الولاء على فاتورة (د.ل)."""
    redeemed = points_redeemed_on_sale(db, sale_id, customer_id)
    if redeemed <= 0:
        return Decimal("0")
    return points_to_dinars(db, redeemed).quantize(Decimal("0.001"))


def sale_amount_paid(
    db: Session,
    sale_id: int,
    *,
    invoice_total: Decimal,
    loyalty_discount: Decimal,
) -> Decimal:
    """المبلغ المحصّل نقداً/مصرفاً على الطلب (بعد خصم النقاط)."""
    from modules.payments.service import sum_sale_payments

    paid_total = sum_sale_payments(db, sale_id)
    if paid_total > 0:
        return paid_total
    paid = (invoice_total - loyalty_discount).quantize(Decimal("0.001"))
    return paid if paid > 0 else Decimal("0")


def build_receipt_loyalty_context(
    db: Session, sale, sale_payment=None
) -> dict:
    """سياق نقاط الولاء والمبالغ المدفوعة للفاتورة المطبوعة."""
    from modules.customers.models import Customer
    from modules.sales.models import ExternalOrderType, SaleContext

    empty_payment = {
        "receipt_show_payment_breakdown": False,
        "receipt_invoice_total_display": format_money_plain(sale.total or 0),
        "receipt_loyalty_discount_display": "0",
        "receipt_amount_paid_display": format_money_plain(sale.total or 0),
        "receipt_amount_customer_total_display": format_money_plain(
            sale.total or 0
        ),
    }
    customer = sale.customer
    if customer is None and sale.customer_id:
        customer = db.get(Customer, sale.customer_id)

    invoice_total = Decimal(str(sale.total or 0)).quantize(Decimal("0.001"))
    is_delivery = (
        sale.context_type == SaleContext.EXTERNAL
        and sale.external_order_type == ExternalOrderType.DELIVERY
    )
    delivery_fee = (
        Decimal(str(sale.delivery_fee or 0)).quantize(Decimal("0.001"))
        if is_delivery
        else Decimal("0")
    )

    if not sale.customer_id or customer is None:
        amount_paid = sale_amount_paid(
            db,
            sale.id,
            invoice_total=invoice_total,
            loyalty_discount=Decimal("0"),
        )
        customer_total = (amount_paid + delivery_fee).quantize(Decimal("0.001"))
        return {
            "loyalty_show_footer": False,
            "loyalty_show_earned": False,
            "loyalty_points_earned_display": "0",
            "loyalty_points_earned_dinar_display": "0",
            "loyalty_points_redeemed_display": "0",
            "loyalty_redeem_dinar_display": "0",
            "loyalty_balance_display": "0",
            "loyalty_points_display": "0",
            **empty_payment,
            "receipt_amount_paid_display": format_money_plain(amount_paid),
            "receipt_amount_customer_total_display": format_money_plain(
                customer_total
            ),
        }

    earned = points_earned_on_sale(db, sale.id, sale.customer_id)
    redeemed = points_redeemed_on_sale(db, sale.id, sale.customer_id)
    balance = Decimal(str(customer.points_balance or 0)).quantize(Decimal("0.001"))
    redeem_dinar = loyalty_discount_for_sale(db, sale.id, sale.customer_id)

    amount_paid = sale_amount_paid(
        db,
        sale.id,
        invoice_total=invoice_total,
        loyalty_discount=redeem_dinar,
    )
    customer_total = (amount_paid + delivery_fee).quantize(Decimal("0.001"))

    earned_dinar = points_to_dinars(db, earned).quantize(Decimal("0.001"))

    return {
        "loyalty_show_footer": True,
        "loyalty_show_earned": earned > 0,
        "loyalty_points_earned_display": format_loyalty_points_display(earned),
        "loyalty_points_earned_dinar_display": format_money_plain(earned_dinar),
        "loyalty_points_redeemed_display": format_loyalty_points_display(redeemed),
        "loyalty_redeem_dinar_display": format_money_plain(redeem_dinar),
        "loyalty_balance_display": format_loyalty_points_display(balance),
        "loyalty_points_display": format_loyalty_points_display(earned),
        "receipt_show_payment_breakdown": redeem_dinar > 0,
        "receipt_invoice_total_display": format_money_plain(invoice_total),
        "receipt_loyalty_discount_display": format_money_plain(redeem_dinar),
        "receipt_amount_paid_display": format_money_plain(amount_paid),
        "receipt_amount_customer_total_display": format_money_plain(customer_total),
    }


def format_money_plain(value) -> str:
    dec = Decimal(str(value)).quantize(Decimal("0.001"))
    sign = "-" if dec < 0 else ""
    raw = format(abs(dec), "f")
    whole, frac = raw.split(".")
    frac = frac.rstrip("0")
    if frac:
        return f"{sign}{whole}.{frac}"
    return f"{sign}{whole}"


def grant_loyalty_after_sale(
    db: Session,
    *,
    customer: Customer,
    sale_id: int,
    paid_total: Decimal,
    user_id: int | None = None,
    skip_notification: bool = False,
    domain=None,
) -> Decimal:
    """منح نقاط على المبلغ المدفوع فعلياً (بعد خصم النقاط إن وُجد)."""
    if loyalty_earn_exists_for_sale(db, sale_id):
        return Decimal("0")
    paid = Decimal(str(paid_total or 0)).quantize(Decimal("0.001"))
    if paid <= 0:
        return Decimal("0")
    return grant_points_for_sale(
        db,
        customer=customer,
        sale_id=sale_id,
        sale_total=paid,
        user_id=user_id,
        skip_notification=skip_notification,
        domain=domain,
    )


def _bump_visit_stats(customer: Customer, sale_total: Decimal) -> None:
    customer.total_spent = Decimal(customer.total_spent or 0) + Decimal(sale_total)
    customer.visits_count = int(customer.visits_count or 0) + 1
    customer.last_visit_at = datetime.now(timezone.utc)


def adjust_points(
    db: Session,
    *,
    customer_id: int,
    delta_points: Decimal,
    note: str | None,
    user_id: int | None = None,
) -> LoyaltyTransaction:
    """تعديل يدوي من الأدمن (إضافة أو خصم — استخدم سالب للخصم)."""
    c = db.get(Customer, customer_id)
    if c is None:
        raise CustomersError("العميل غير موجود.")
    delta = Decimal(delta_points).quantize(Decimal("0.001"))
    new_balance = Decimal(c.points_balance or 0) + delta
    if new_balance < 0:
        raise CustomersError(
            f"التعديل سيجعل الرصيد سالباً ({new_balance}). "
            "أَدخل قيمة أصغر."
        )
    c.points_balance = new_balance
    txn = LoyaltyTransaction(
        customer_id=customer_id,
        sale_id=None,
        kind=LoyaltyTxnKind.ADJUST,
        points=delta,
        note=(note or "تعديل يدوي").strip(),
        created_by_id=user_id,
    )
    db.add(txn)
    db.flush()
    return txn


def list_transactions(
    db: Session, customer_id: int, limit: int = 50
) -> list[LoyaltyTransaction]:
    stmt = (
        select(LoyaltyTransaction)
        .where(LoyaltyTransaction.customer_id == customer_id)
        .order_by(LoyaltyTransaction.id.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt))


def total_points_grand(db: Session) -> Decimal:
    val = db.scalar(
        select(func.coalesce(func.sum(Customer.points_balance), 0))
    )
    return Decimal(val or 0)
