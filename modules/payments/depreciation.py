"""حسابات الإهلاك للأصول الثابتة (طريقة القسط الثابت — Straight-Line).

المرجع: المعيار الدولي IAS 16 (Property, Plant and Equipment) و IAS 38 (Intangibles).
الأساس: تكلفة الأصل القابل للإهلاك = الكلفة الإجمالية − قيمة الخردة (Salvage).
الإهلاك الشهري = الكلفة القابلة للإهلاك ÷ العمر الإنتاجي بالأشهر.
الإهلاك في الفترة = الإهلاك الشهري × عدد الأشهر التي تقع داخل الفترة من بدء الإهلاك حتى التخلّص (إن وُجد).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.payments.models import Purchase, PurchaseKind, PurchaseLine


@dataclass
class FixedAssetSnapshot:
    """صورة أصل ثابت في نقطة زمنية معيّنة (نهاية فترة)."""

    line: PurchaseLine
    purchase: Purchase
    cost: Decimal  # الكلفة الإجمالية
    salvage_value: Decimal  # قيمة الخردة المتوقعة
    depreciable_base: Decimal  # cost - salvage
    useful_life_months: int
    monthly_depreciation: Decimal
    months_in_service_to_date: int  # عدد الأشهر منذ بدء الإهلاك حتى التاريخ المحدد
    accumulated_depreciation: Decimal  # الإهلاك المتراكم حتى التاريخ
    book_value: Decimal  # القيمة الدفترية = cost - accumulated_depreciation
    is_fully_depreciated: bool
    is_disposed: bool


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _months_between(start: datetime, end: datetime) -> int:
    """عدد الأشهر الكاملة من start حتى end (بحدّ أدنى صفر)."""
    if end <= start:
        return 0
    s = _to_utc(start)
    e = _to_utc(end)
    months = (e.year - s.year) * 12 + (e.month - s.month)
    # تعديل للأيام: إذا لم يبلغ يوم النهاية يوم البداية بعد، انقص شهراً
    if e.day < s.day:
        months -= 1
    return max(0, months)


def _depreciation_start(line: PurchaseLine) -> datetime:
    """تاريخ بدء الإهلاك = تاريخ شراء الأصل."""
    pur = line.purchase
    if pur is not None and pur.created_at is not None:
        return _to_utc(pur.created_at)
    return datetime.now(timezone.utc)


def _effective_end(line: PurchaseLine, as_of: datetime) -> datetime:
    """نهاية فترة الإهلاك الفعلية: أيهما أبكر — تاريخ التخلّص أم انتهاء العمر الإنتاجي أم as_of."""
    start = _depreciation_start(line)
    end_of_life = start
    months = int(line.useful_life_months or 0)
    if months > 0:
        # نضيف أشهر بطريقة آمنة
        y = start.year + (start.month - 1 + months) // 12
        m = (start.month - 1 + months) % 12 + 1
        try:
            end_of_life = start.replace(year=y, month=m)
        except ValueError:
            # احتمال يوم 31 في شهر 30 — انقل لآخر يوم صالح
            from calendar import monthrange

            last_day = monthrange(y, m)[1]
            end_of_life = start.replace(year=y, month=m, day=min(start.day, last_day))
    candidates = [_to_utc(as_of), _to_utc(end_of_life)]
    if line.disposal_date is not None:
        candidates.append(_to_utc(line.disposal_date))
    return min(candidates)


def snapshot_at(line: PurchaseLine, as_of: datetime) -> FixedAssetSnapshot:
    """يحسب صورة الأصل الثابت في تاريخ معيّن."""
    cost = Decimal(str(line.line_total or 0))
    salvage = Decimal(str(line.salvage_value or 0))
    base = cost - salvage
    if base < 0:
        base = Decimal("0")
    months = int(line.useful_life_months or 0)
    monthly = (base / Decimal(months)).quantize(Decimal("0.001")) if months > 0 else Decimal("0")

    start = _depreciation_start(line)
    end = _effective_end(line, as_of)
    months_in_service = _months_between(start, end)
    if months > 0:
        months_in_service = min(months_in_service, months)
    accumulated = (Decimal(months_in_service) * monthly).quantize(Decimal("0.001"))
    if accumulated > base:
        accumulated = base
    book_value = (cost - accumulated).quantize(Decimal("0.001"))
    return FixedAssetSnapshot(
        line=line,
        purchase=line.purchase,
        cost=cost.quantize(Decimal("0.001")),
        salvage_value=salvage.quantize(Decimal("0.001")),
        depreciable_base=base.quantize(Decimal("0.001")),
        useful_life_months=months,
        monthly_depreciation=monthly,
        months_in_service_to_date=months_in_service,
        accumulated_depreciation=accumulated,
        book_value=book_value,
        is_fully_depreciated=(months > 0 and months_in_service >= months),
        is_disposed=(line.disposal_date is not None and line.disposal_date <= as_of),
    )


def depreciation_in_period(
    line: PurchaseLine, period_start: datetime, period_end: datetime
) -> Decimal:
    """يحسب مقدار الإهلاك المعترف به ضمن فترة [period_start, period_end).

    الفكرة: snapshot في period_end ناقص snapshot في period_start = الإهلاك المعترف به في الفترة.
    """
    if not (line.useful_life_months and int(line.useful_life_months) > 0):
        return Decimal("0")
    snap_end = snapshot_at(line, period_end)
    snap_start = snapshot_at(line, period_start)
    diff = snap_end.accumulated_depreciation - snap_start.accumulated_depreciation
    return diff.quantize(Decimal("0.001")) if diff > 0 else Decimal("0")


def list_fixed_asset_lines(db: Session) -> list[PurchaseLine]:
    """جميع بنود الأصول الثابتة (ASSET مع useful_life_months>0)، أحدثاً أولاً."""
    stmt = (
        select(PurchaseLine)
        .join(Purchase, Purchase.id == PurchaseLine.purchase_id)
        .where(Purchase.kind == PurchaseKind.ASSET)
        .where(PurchaseLine.useful_life_months > 0)
        .order_by(Purchase.created_at.desc(), PurchaseLine.id.desc())
    )
    return list(db.scalars(stmt).all())


def list_consumable_asset_lines_in_period(
    db: Session, start: datetime, end: datetime
) -> list[PurchaseLine]:
    """بنود مستلزمات استهلاكية (ASSET مع useful_life_months=0) خلال الفترة."""
    s, e = _to_utc(start), _to_utc(end)
    stmt = (
        select(PurchaseLine)
        .join(Purchase, Purchase.id == PurchaseLine.purchase_id)
        .where(Purchase.kind == PurchaseKind.ASSET)
        .where((PurchaseLine.useful_life_months == 0) | (PurchaseLine.useful_life_months.is_(None)))
        .where(Purchase.created_at >= s, Purchase.created_at < e)
        .order_by(Purchase.created_at.desc(), PurchaseLine.id.desc())
    )
    return list(db.scalars(stmt).all())


def consumable_assets_total_in_period(
    db: Session, start: datetime, end: datetime
) -> Decimal:
    """إجمالي الأصول الاستهلاكية المسجَّلة في الفترة (مصروف فوري)."""
    total = Decimal("0")
    for ln in list_consumable_asset_lines_in_period(db, start, end):
        total += Decimal(str(ln.line_total or 0))
    return total.quantize(Decimal("0.001"))


def total_depreciation_in_period(
    db: Session, start: datetime, end: datetime
) -> Decimal:
    """إجمالي مصروف الإهلاك للفترة لجميع الأصول الثابتة."""
    total = Decimal("0")
    for ln in list_fixed_asset_lines(db):
        total += depreciation_in_period(ln, start, end)
    return total.quantize(Decimal("0.001"))


@dataclass
class FixedAssetsSummary:
    asset_count: int
    total_cost: Decimal
    total_salvage: Decimal
    total_accumulated: Decimal
    total_book_value: Decimal
    total_monthly_depreciation: Decimal
    fully_depreciated_count: int


def fixed_assets_summary(db: Session, as_of: datetime | None = None) -> FixedAssetsSummary:
    """ملخّص دفتر الأصول الثابتة عند تاريخ as_of (افتراضياً الآن)."""
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    cost = Decimal("0")
    salvage = Decimal("0")
    accum = Decimal("0")
    book = Decimal("0")
    monthly = Decimal("0")
    fully = 0
    cnt = 0
    for ln in list_fixed_asset_lines(db):
        if ln.disposal_date is not None and ln.disposal_date <= as_of:
            continue  # نتجاهل المتخلَّص منها
        snap = snapshot_at(ln, as_of)
        cnt += 1
        cost += snap.cost
        salvage += snap.salvage_value
        accum += snap.accumulated_depreciation
        book += snap.book_value
        monthly += snap.monthly_depreciation
        if snap.is_fully_depreciated:
            fully += 1
    return FixedAssetsSummary(
        asset_count=cnt,
        total_cost=cost.quantize(Decimal("0.001")),
        total_salvage=salvage.quantize(Decimal("0.001")),
        total_accumulated=accum.quantize(Decimal("0.001")),
        total_book_value=book.quantize(Decimal("0.001")),
        total_monthly_depreciation=monthly.quantize(Decimal("0.001")),
        fully_depreciated_count=fully,
    )
