"""صلاحية المنتج — تتبع بالدفعات (Lot) المرتبطة بفاتورة الشراء."""

from __future__ import annotations



from dataclasses import dataclass

from datetime import date



from sqlalchemy import select

from sqlalchemy.orm import Session



from modules.catalog.models import Product

from modules.catalog.service import CatalogError

from modules.inventory.models import InventoryLot





def parse_optional_date(raw: str | None, *, field_label: str) -> date | None:

    s = (raw or "").strip()

    if not s:

        return None

    try:

        return date.fromisoformat(s)

    except ValueError as exc:

        raise CatalogError(f"{field_label} غير صالح — استخدم YYYY-MM-DD.") from exc





def days_until_expiry(expiry: date | None, *, today: date | None = None) -> int | None:

    if expiry is None:

        return None

    ref = today or date.today()

    return (expiry - ref).days





def expiry_remaining_label(expiry: date | None, *, today: date | None = None) -> str:

    days = days_until_expiry(expiry, today=today)

    if days is None:

        return "—"

    if days < 0:

        return f"منتهي منذ {abs(days)} يوم"

    if days == 0:

        return "ينتهي اليوم"

    return f"{days} يوم"





def should_warn_lot(

    lot: InventoryLot,

    product: Product,

    *,

    today: date | None = None,

) -> bool:

    if not product.expiry_tracked or lot.expiry_date is None:

        return False

    if lot.qty_remaining <= 0:

        return False

    days = days_until_expiry(lot.expiry_date, today=today)

    if days is None:

        return False

    warn = max(1, int(product.expiry_warn_days or 7))

    return days <= warn





def apply_product_expiry_form(

    product: Product,

    *,

    expiry_tracked: str,

    expiry_warn_days: str,

) -> None:

    """كرت الصنف: تفعيل التتبع + أيام التنبيه فقط — التاريخ يُدخل عند الشراء."""

    tracked = (expiry_tracked or "").strip().lower() in ("on", "1", "true", "yes")

    product.expiry_tracked = tracked

    product.expiry_production_date = None

    product.expiry_date = None

    if not tracked:

        product.expiry_warn_days = 7

        return



    warn_raw = (expiry_warn_days or "").strip()

    try:

        warn = int(warn_raw) if warn_raw else 7

    except ValueError as exc:

        raise CatalogError("أيام التنبيه يجب أن تكون رقماً صحيحاً.") from exc

    if warn < 1:

        raise CatalogError("أيام التنبيه يجب أن تكون 1 على الأقل.")

    product.expiry_warn_days = warn





@dataclass(frozen=True)

class ExpiryWarningRow:

    product: Product

    lot: InventoryLot





def list_expiry_warning_lots(db: Session) -> list[ExpiryWarningRow]:

    today = date.today()

    lots = list(

        db.scalars(

            select(InventoryLot)

            .where(

                InventoryLot.qty_remaining > 0,

                InventoryLot.expiry_date.isnot(None),

            )

            .order_by(InventoryLot.expiry_date.asc(), InventoryLot.id.asc())

        ).all()

    )

    if not lots:

        return []

    product_ids = {lot.product_id for lot in lots}

    products = {

        p.id: p

        for p in db.scalars(

            select(Product).where(

                Product.id.in_(product_ids),

                Product.is_active.is_(True),

                Product.expiry_tracked.is_(True),

            )

        ).all()

    }

    out: list[ExpiryWarningRow] = []

    for lot in lots:

        product = products.get(lot.product_id)

        if product is None:

            continue

        if should_warn_lot(lot, product, today=today):

            out.append(ExpiryWarningRow(product=product, lot=lot))

    return out





def list_expiry_warning_products(db: Session) -> list[Product]:

    """للتوافق مع التنبيهات — منتجات لها دفعات قريبة من الانتهاء."""

    seen: set[int] = set()

    out: list[Product] = []

    for row in list_expiry_warning_lots(db):

        if row.product.id not in seen:

            seen.add(row.product.id)

            out.append(row.product)

    return out





def format_expiry_alert_message(rows: list[ExpiryWarningRow], *, store_name: str) -> str:

    if not rows:

        return ""

    lines = [f"⚠️ [{store_name}] تنبيه صلاحية دفعات ({len(rows)}):"]

    for row in rows:

        label = expiry_remaining_label(row.lot.expiry_date)

        exp_s = row.lot.expiry_date.isoformat() if row.lot.expiry_date else "—"

        qty = row.lot.qty_remaining

        lines.append(

            f"• {row.product.name_ar} — دفعة {row.lot.lot_code} — "

            f"ينتهي {exp_s} ({label}) — متبقي {qty}"

        )

    return "\n".join(lines)





def format_expiry_alert_message_from_lots(db: Session, *, store_name: str) -> str:

    return format_expiry_alert_message(list_expiry_warning_lots(db), store_name=store_name)

