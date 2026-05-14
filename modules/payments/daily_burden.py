"""تحليل التعادل اليومي (Cost-Volume-Profit / Break-Even Analysis).

المرجع: Cost Accounting (Horngren, Datar, Rajan) — الفصل 3 (CVP Analysis)
وأيضاً Management Accounting (Kaplan & Atkinson). المعايير الدولية لا تفرض
صيغة محدّدة لتحليل التعادل (لأنه أداة إدارية وليست محاسبة مالية)، لكنه أداة
معتمدة عالمياً في إدارة الأعمال الصغيرة والمطاعم.

الخطوات:
1) جمع التكاليف الثابتة الشهرية (رواتب، إيجار، اشتراكات...) من جدول RecurringCost.
2) إضافة قسط الإهلاك الشهري (من الأصول الثابتة).
3) قسمة الإجمالي على عدد أيام التشغيل المتوقعة في الشهر (افتراضياً 30) → العبء اليومي.
4) حساب «هامش المساهمة» (Contribution Margin Ratio) من تاريخ المبيعات الأخير:
       CM Ratio = (Revenue − COGS) / Revenue
5) عتبة التعادل اليومية للإيراد:
       Break-Even Daily Revenue = Daily Fixed Cost / CM Ratio

نتيجة: «إذا بِعتَ يومياً ≥ X د.ل (وحافظتَ على نفس الهامش) فأنت تغطّي تكاليفك،
وأي زيادة فوقها هي ربحٌ صافٍ حقيقي».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.payments.depreciation import fixed_assets_summary
from modules.payments.models import RecurringCost, RecurringCostCategory
from modules.sales.models import Sale, SaleStatus


DEFAULT_OPERATING_DAYS = Decimal("30")


@dataclass
class CategoryRow:
    category: str
    label_ar: str
    monthly: Decimal
    daily: Decimal


@dataclass
class DailyBurden:
    operating_days_per_month: Decimal
    rows: list[CategoryRow] = field(default_factory=list)
    fixed_monthly_total: Decimal = Decimal("0")
    fixed_daily_total: Decimal = Decimal("0")
    depreciation_monthly: Decimal = Decimal("0")
    depreciation_daily: Decimal = Decimal("0")
    grand_monthly: Decimal = Decimal("0")
    grand_daily: Decimal = Decimal("0")
    # أرقام التعادل
    cm_ratio: Decimal = Decimal("0")  # نسبة هامش المساهمة من المبيعات الأخيرة
    cm_lookback_days: int = 30
    cm_lookback_revenue: Decimal = Decimal("0")
    cm_lookback_cogs: Decimal = Decimal("0")
    break_even_daily_revenue: Decimal = Decimal("0")
    # موقف اليوم
    today_revenue: Decimal = Decimal("0")
    today_cogs: Decimal = Decimal("0")
    today_gross_profit: Decimal = Decimal("0")
    today_net_position: Decimal = Decimal("0")  # gross − daily burden
    is_above_break_even: bool = False
    coverage_pct: float = 0.0  # نسبة تغطية إيراد اليوم لعتبة التعادل


_CATEGORY_LABELS = {
    RecurringCostCategory.SALARY: "رواتب وأجور",
    RecurringCostCategory.RENT: "إيجار",
    RecurringCostCategory.UTILITY: "كهرباء/ماء/إنترنت",
    RecurringCostCategory.SUBSCRIPTION: "اشتراكات",
    RecurringCostCategory.INSURANCE: "تأمين",
    RecurringCostCategory.LICENSE: "تراخيص ورسوم",
    RecurringCostCategory.OTHER: "أخرى",
}


def category_label(cat: RecurringCostCategory | str) -> str:
    if isinstance(cat, str):
        try:
            cat = RecurringCostCategory(cat)
        except ValueError:
            return "أخرى"
    return _CATEGORY_LABELS.get(cat, "أخرى")


def list_recurring_costs(
    db: Session, only_active: bool = False
) -> list[RecurringCost]:
    stmt = select(RecurringCost).order_by(
        RecurringCost.category, RecurringCost.name_ar
    )
    if only_active:
        stmt = stmt.where(RecurringCost.is_active.is_(True))
    return list(db.scalars(stmt).all())


def _hr_active_salaries(db: Session) -> Decimal:
    """مجموع الرواتب الأساسية للموظفين النشطين من وحدة الموارد البشرية.

    هذا هو المصدر الموحّد للرواتب. إن وُجد إدخال SALARY يدوي في
    RecurringCost فهو يعتبر «إضافي» (مثلاً: مكافآت دورية أو رواتب موسمية).
    """
    try:
        from modules.hr.service import total_active_monthly_salaries

        return total_active_monthly_salaries(db)
    except Exception:
        return Decimal("0")


def recurring_costs_monthly_total(db: Session) -> Decimal:
    """مجموع التكاليف الشهرية الثابتة النشطة (رواتب الموظفين + إيجار + اشتراكات...).

    = رواتب الموظفين النشطين من وحدة HR + كل بنود RecurringCost النشطة.
    يُستخدم في طرحه من صافي الربح بشكل متناسب مع طول الفترة.
    """
    total = db.execute(
        select(func.coalesce(func.sum(RecurringCost.monthly_amount), 0)).where(
            RecurringCost.is_active.is_(True)
        )
    ).scalar_one()
    rec_total = Decimal(str(total or 0))
    return (rec_total + _hr_active_salaries(db)).quantize(Decimal("0.001"))


def recurring_costs_in_period(
    db: Session,
    start: datetime,
    end: datetime,
    operating_days_per_month: Decimal | int = DEFAULT_OPERATING_DAYS,
) -> tuple[Decimal, Decimal, Decimal]:
    """يحسب حصة التكاليف الشهرية الثابتة المنسوبة للفترة المحددة.

    منطقياً: لو الراتب الشهري 1000 د.ل، فحصته اليومية 1000/30 ≈ 33.3،
    وحصة 7 أيام = 233، وحصة شهر كامل = 1000.

    عدد أيام الفترة يُقاس بفروق التواريخ.

    Returns: (monthly_total, period_share, period_days)
    """
    monthly = recurring_costs_monthly_total(db)
    days = Decimal(str(max(1.0, (end - start).total_seconds() / 86400.0))).quantize(
        Decimal("0.0001")
    )
    days_per_month = Decimal(str(operating_days_per_month or DEFAULT_OPERATING_DAYS))
    if days_per_month <= 0:
        days_per_month = DEFAULT_OPERATING_DAYS
    if monthly <= 0:
        return monthly, Decimal("0"), days
    share = (monthly * days / days_per_month).quantize(Decimal("0.001"))
    return monthly, share, days


def recurring_costs_breakdown_in_period(
    db: Session,
    start: datetime,
    end: datetime,
    operating_days_per_month: Decimal | int = DEFAULT_OPERATING_DAYS,
) -> list[tuple[str, str, Decimal, Decimal]]:
    """تفصيل التكاليف الشهرية حسب التصنيف، مع حصة الفترة.

    يضم:
    - رواتب الموظفين النشطين من وحدة HR (تُجمع مع SALARY).
    - بقية بنود RecurringCost (إيجار/كهرباء/اشتراكات...).

    Returns: list of (category_code, label_ar, monthly_total, period_share)
    """
    days = Decimal(str(max(1.0, (end - start).total_seconds() / 86400.0)))
    days_per_month = Decimal(str(operating_days_per_month or DEFAULT_OPERATING_DAYS))
    if days_per_month <= 0:
        days_per_month = DEFAULT_OPERATING_DAYS
    by_cat: dict[RecurringCostCategory, Decimal] = {}
    for rc in list_recurring_costs(db, only_active=True):
        amt = Decimal(str(rc.monthly_amount or 0))
        by_cat[rc.category] = by_cat.get(rc.category, Decimal("0")) + amt
    hr_salaries = _hr_active_salaries(db)
    if hr_salaries > 0:
        by_cat[RecurringCostCategory.SALARY] = (
            by_cat.get(RecurringCostCategory.SALARY, Decimal("0")) + hr_salaries
        )
    out: list[tuple[str, str, Decimal, Decimal]] = []
    for cat in RecurringCostCategory:
        monthly = by_cat.get(cat, Decimal("0")).quantize(Decimal("0.001"))
        if monthly <= 0:
            continue
        share = (monthly * days / days_per_month).quantize(Decimal("0.001"))
        out.append((cat.value, category_label(cat), monthly, share))
    return out


def _avg_cm_ratio(
    db: Session, lookback_days: int = 30
) -> tuple[Decimal, Decimal, Decimal]:
    """يحسب متوسط نسبة هامش المساهمة خلال آخر N يوم.
    return: (cm_ratio, total_revenue, total_cogs)
    """
    # نستورد هنا لتجنّب الدورات
    from modules.reporting.queries import cogs_summary

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    revenue = db.execute(
        select(func.coalesce(func.sum(Sale.total), 0)).where(
            Sale.status == SaleStatus.COMPLETED,
            Sale.created_at >= start,
            Sale.created_at < end,
        )
    ).scalar_one()
    revenue = Decimal(str(revenue or 0))
    cogs = cogs_summary(db, start, end)
    if revenue <= 0:
        return Decimal("0"), revenue, cogs
    cm = ((revenue - cogs) / revenue).quantize(Decimal("0.0001"))
    if cm < 0:
        cm = Decimal("0")
    if cm > 1:
        cm = Decimal("1")
    return cm, revenue, cogs


def _today_position(db: Session) -> tuple[Decimal, Decimal]:
    """إيراد وتكلفة المباع لليوم الحالي."""
    from modules.reporting.queries import cogs_summary

    now = datetime.now(timezone.utc)
    today_s = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_e = today_s + timedelta(days=1)
    revenue = db.execute(
        select(func.coalesce(func.sum(Sale.total), 0)).where(
            Sale.status == SaleStatus.COMPLETED,
            Sale.created_at >= today_s,
            Sale.created_at < today_e,
        )
    ).scalar_one()
    revenue = Decimal(str(revenue or 0))
    cogs = cogs_summary(db, today_s, today_e)
    return revenue, cogs


def compute_daily_burden(
    db: Session,
    operating_days_per_month: Decimal | int = DEFAULT_OPERATING_DAYS,
    cm_lookback_days: int = 30,
) -> DailyBurden:
    """يحسب العبء اليومي وتحليل التعادل."""
    days = Decimal(str(operating_days_per_month or DEFAULT_OPERATING_DAYS))
    if days <= 0:
        days = DEFAULT_OPERATING_DAYS
    burden = DailyBurden(operating_days_per_month=days, cm_lookback_days=cm_lookback_days)

    # تجميع التكاليف المتكرّرة حسب الفئة
    by_cat: dict[RecurringCostCategory, Decimal] = {}
    for rc in list_recurring_costs(db, only_active=True):
        amt = Decimal(str(rc.monthly_amount or 0))
        by_cat[rc.category] = by_cat.get(rc.category, Decimal("0")) + amt

    # ضمّ رواتب الموظفين النشطين من وحدة HR إلى صف SALARY
    hr_salaries = _hr_active_salaries(db)
    if hr_salaries > 0:
        by_cat[RecurringCostCategory.SALARY] = (
            by_cat.get(RecurringCostCategory.SALARY, Decimal("0")) + hr_salaries
        )

    for cat in RecurringCostCategory:
        monthly = by_cat.get(cat, Decimal("0")).quantize(Decimal("0.001"))
        if monthly <= 0:
            continue
        daily = (monthly / days).quantize(Decimal("0.001"))
        burden.rows.append(
            CategoryRow(
                category=cat.value,
                label_ar=_CATEGORY_LABELS[cat],
                monthly=monthly,
                daily=daily,
            )
        )
        burden.fixed_monthly_total += monthly
        burden.fixed_daily_total += daily

    burden.fixed_monthly_total = burden.fixed_monthly_total.quantize(Decimal("0.001"))
    burden.fixed_daily_total = burden.fixed_daily_total.quantize(Decimal("0.001"))

    # الإهلاك من سجل الأصول الثابتة
    fa = fixed_assets_summary(db)
    burden.depreciation_monthly = fa.total_monthly_depreciation
    burden.depreciation_daily = (
        fa.total_monthly_depreciation / days
    ).quantize(Decimal("0.001"))

    burden.grand_monthly = (
        burden.fixed_monthly_total + burden.depreciation_monthly
    ).quantize(Decimal("0.001"))
    burden.grand_daily = (burden.fixed_daily_total + burden.depreciation_daily).quantize(
        Decimal("0.001")
    )

    # هامش المساهمة من المبيعات الأخيرة
    cm, rev_back, cogs_back = _avg_cm_ratio(db, lookback_days=cm_lookback_days)
    burden.cm_ratio = cm
    burden.cm_lookback_revenue = rev_back
    burden.cm_lookback_cogs = cogs_back

    if cm > 0:
        burden.break_even_daily_revenue = (burden.grand_daily / cm).quantize(
            Decimal("0.001")
        )
    else:
        burden.break_even_daily_revenue = Decimal("0")

    # موقف اليوم
    today_rev, today_cogs = _today_position(db)
    burden.today_revenue = today_rev
    burden.today_cogs = today_cogs
    burden.today_gross_profit = (today_rev - today_cogs).quantize(Decimal("0.001"))
    burden.today_net_position = (
        burden.today_gross_profit - burden.grand_daily
    ).quantize(Decimal("0.001"))
    burden.is_above_break_even = burden.today_net_position >= 0

    if burden.break_even_daily_revenue > 0:
        burden.coverage_pct = float(
            (today_rev / burden.break_even_daily_revenue * 100).quantize(Decimal("0.1"))
        )
    else:
        burden.coverage_pct = 0.0

    return burden


def upsert_recurring_cost(
    db: Session,
    *,
    rc_id: int | None,
    name_ar: str,
    category: RecurringCostCategory | str,
    monthly_amount: Decimal,
    is_active: bool = True,
    notes: str | None = None,
) -> RecurringCost:
    name = (name_ar or "").strip()
    if not name:
        raise ValueError("الاسم مطلوب.")
    if monthly_amount < 0:
        raise ValueError("المبلغ لا يمكن أن يكون سالباً.")
    if isinstance(category, str):
        try:
            cat = RecurringCostCategory(category)
        except ValueError:
            cat = RecurringCostCategory.OTHER
    else:
        cat = category
    if rc_id:
        rc = db.get(RecurringCost, rc_id)
        if rc is None:
            raise ValueError("التكلفة الشهرية غير موجودة.")
        rc.name_ar = name
        rc.category = cat
        rc.monthly_amount = monthly_amount.quantize(Decimal("0.001"))
        rc.is_active = is_active
        rc.notes = (notes or "").strip() or None
        rc.updated_at = datetime.now(timezone.utc)
    else:
        rc = RecurringCost(
            name_ar=name,
            category=cat,
            monthly_amount=monthly_amount.quantize(Decimal("0.001")),
            is_active=is_active,
            notes=(notes or "").strip() or None,
        )
        db.add(rc)
        db.flush()
    return rc


def delete_recurring_cost(db: Session, rc_id: int) -> None:
    rc = db.get(RecurringCost, rc_id)
    if rc is not None:
        db.delete(rc)
        db.flush()
