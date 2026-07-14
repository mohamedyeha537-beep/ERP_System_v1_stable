"""تكلفة التركيبة (BOM) وتسعير المنتجات المرتبطة بالنسبة."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from modules.catalog.models import BillOfMaterialsLine, Product
from modules.catalog.service import CatalogError
from modules.reporting.queries import avg_unit_cost_per_product
from modules.settings.service import get_setting


@dataclass(frozen=True)
class BomLineCostRow:
    line_id: int
    component_id: int
    name_ar: str
    unit: str
    qty_per_parent: Decimal
    unit_cost: Decimal
    line_cost: Decimal
    reference_unit_cost: Decimal | None = None
    cost_from_purchase: bool = False
    packaging_only: bool = False


def component_unit_cost(product: Product, avg_costs: dict[int, Decimal]) -> tuple[Decimal, Decimal | None, bool]:
    """(التكلفة المستخدمة، المرجع اليدوي إن وُجد، هل من متوسط شراء)."""
    ref = product.reference_unit_cost
    if ref is not None and ref > 0:
        return Decimal(str(ref)).quantize(Decimal("0.001")), ref, False
    avg = avg_costs.get(int(product.id), Decimal("0"))
    return avg, None, avg > 0


def save_bom_line_edits(
    db: Session,
    parent: Product,
    *,
    line_qty: dict[int, Decimal],
    line_unit_cost: dict[int, Decimal | None],
) -> None:
    """حفظ كميات التركيبة وتكلفة المرجع للمكوّنات — بدون فواتير."""
    if not parent.bom_lines_as_parent:
        raise CatalogError("لا توجد بنود للحفظ.")
    lines_by_id = {int(ln.id): ln for ln in parent.bom_lines_as_parent}
    for line_id, qty in line_qty.items():
        if int(line_id) not in lines_by_id:
            raise CatalogError(f"بند تركيبة #{line_id} غير موجود.")
        if qty <= 0:
            raise CatalogError("الكمية يجب أن تكون أكبر من صفر.")
    for line_id, cost in line_unit_cost.items():
        if int(line_id) not in lines_by_id:
            continue
        if cost is not None and cost < 0:
            raise CatalogError("تكلفة الوحدة لا يمكن أن تكون سالبة.")

    for line_id, qty in line_qty.items():
        line = lines_by_id[int(line_id)]
        line.qty_per_parent = qty.quantize(Decimal("0.0001"))
        comp = line.component_product
        if comp is None:
            comp = db.get(Product, line.component_product_id)
        if comp is None:
            continue
        if int(line_id) in line_unit_cost:
            raw = line_unit_cost[int(line_id)]
            if raw is None or raw <= 0:
                comp.reference_unit_cost = None
            else:
                comp.reference_unit_cost = raw.quantize(Decimal("0.001"))
    db.flush()


def global_bom_adjust_pct(db: Session) -> Decimal:
    raw = (get_setting(db, "catalog_bom_global_adjust_pct", "0") or "0").strip()
    try:
        return Decimal(raw.replace(",", "."))
    except InvalidOperation:
        return Decimal("0")


def parse_markup_pct(raw: str | None) -> Decimal | None:
    s = (raw or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation as exc:
        raise CatalogError("نسبة الربح يجب أن تكون رقماً.") from exc


def effective_markup_pct(product_markup: Decimal | None, global_adjust: Decimal) -> Decimal:
    base = product_markup if product_markup is not None else Decimal("0")
    return (base + global_adjust).quantize(Decimal("0.001"))


def suggested_sell_price(
    bom_cost: Decimal,
    *,
    product_markup_pct: Decimal | None,
    global_adjust_pct: Decimal,
) -> Decimal:
    if bom_cost <= 0:
        return Decimal("0")
    markup = effective_markup_pct(product_markup_pct, global_adjust_pct)
    return (bom_cost * (1 + markup / Decimal("100"))).quantize(Decimal("0.001"))


def bom_line_costs(db: Session, parent_product_id: int, *, _stack: frozenset[int] | None = None) -> list[BomLineCostRow]:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from modules.catalog.bom_explosion import product_has_recipe

    stack = _stack or frozenset()
    if parent_product_id in stack:
        return []
    stack = stack | {parent_product_id}

    parent = db.execute(
        select(Product)
        .where(Product.id == parent_product_id)
        .options(
            selectinload(Product.bom_lines_as_parent).selectinload(
                BillOfMaterialsLine.component_product
            )
        )
    ).scalar_one_or_none()
    if parent is None:
        return []
    avg_costs = avg_unit_cost_per_product(db)
    rows: list[BomLineCostRow] = []
    for line in parent.bom_lines_as_parent:
        comp = line.component_product
        if comp is None:
            continue
        if product_has_recipe(db, int(comp.id)):
            sub_cost = bom_total_cost(db, int(comp.id), _stack=stack)
            unit_cost = sub_cost
            ref_cost = None
            from_purchase = sub_cost > 0
        else:
            unit_cost, ref_cost, from_purchase = component_unit_cost(comp, avg_costs)
        qty = Decimal(str(line.qty_per_parent or 0))
        line_cost = (qty * unit_cost).quantize(Decimal("0.001"))
        rows.append(
            BomLineCostRow(
                line_id=int(line.id),
                component_id=int(comp.id),
                name_ar=comp.name_ar,
                unit=comp.unit or "—",
                qty_per_parent=qty,
                unit_cost=unit_cost,
                line_cost=line_cost,
                reference_unit_cost=ref_cost,
                cost_from_purchase=from_purchase and ref_cost is None,
                packaging_only=bool(line.packaging_only),
            )
        )
    return rows


def bom_total_cost(db: Session, parent_product_id: int, *, _stack: frozenset[int] | None = None) -> Decimal:
    total = sum(
        (r.line_cost for r in bom_line_costs(db, parent_product_id, _stack=_stack) if not r.packaging_only),
        Decimal("0"),
    )
    return total.quantize(Decimal("0.001"))


def apply_bom_pricing_to_product(
    db: Session,
    product: Product,
    *,
    price_linked_to_bom: bool,
    bom_markup_pct: Decimal | None,
    sell_price: Decimal | None,
    manual_price: bool,
) -> None:
    """حفظ تسعير التركيبة على المنتج."""
    global_adj = global_bom_adjust_pct(db)
    product.price_linked_to_bom = price_linked_to_bom
    product.bom_markup_pct = bom_markup_pct

    if manual_price and sell_price is not None:
        product.sell_price = sell_price
        product.price_linked_to_bom = False
        product.bom_markup_pct = bom_markup_pct
        return

    if price_linked_to_bom:
        cost = bom_total_cost(db, product.id)
        if cost <= 0:
            raise CatalogError("لا يمكن ربط السعر بالتركيبة — أضف مكوّنات أو أدخل تكلفة المرجع.")
        if bom_markup_pct is None:
            raise CatalogError("أدخل نسبة الربح عند تفعيل الربط بالتركيبة.")
        product.sell_price = suggested_sell_price(
            cost,
            product_markup_pct=bom_markup_pct,
            global_adjust_pct=global_adj,
        )
    elif sell_price is not None:
        product.sell_price = sell_price


def recalculate_all_bom_linked_prices(db: Session) -> int:
    """إعادة حساب أسعار كل المنتجات المرتبطة بالتركيبة (بعد تغيير النسبة العامة)."""
    from sqlalchemy import select

    global_adj = global_bom_adjust_pct(db)
    products = list(
        db.scalars(
            select(Product).where(Product.price_linked_to_bom.is_(True))
        ).all()
    )
    updated = 0
    for p in products:
        old_cost = bom_total_cost(db, p.id)
        cost = bom_total_cost(db, p.id)
        if cost <= 0:
            continue
        new_price = suggested_sell_price(
            cost,
            product_markup_pct=p.bom_markup_pct,
            global_adjust_pct=global_adj,
        )
        if p.sell_price != new_price:
            p.sell_price = new_price
            updated += 1
            try:
                from modules.notifications.inventory_hooks import emit_recipe_cost_changed

                emit_recipe_cost_changed(
                    db,
                    product_id=p.id,
                    product_name=p.name_ar or str(p.id),
                    old_cost=old_cost,
                    new_cost=cost,
                )
            except Exception:  # noqa: BLE001
                pass
    db.flush()
    return updated


@dataclass(frozen=True)
class CatalogProductPricing:
    unit_cost: Decimal | None
    margin: Decimal | None
    pricing_error: bool


def catalog_product_unit_cost(
    db: Session,
    product_id: int,
    avg_costs: dict[int, Decimal],
) -> Decimal:
    """تكلفة العرض في قائمة الأصناف — مرجع يدوي أو متوسط شراء أو تركيبة."""
    from modules.catalog.bom_explosion import product_has_recipe

    pid = int(product_id)
    if product_has_recipe(db, pid):
        return bom_total_cost(db, pid)
    product = db.get(Product, pid)
    if product is None:
        return Decimal("0")
    cost, _, _ = component_unit_cost(product, avg_costs)
    return cost.quantize(Decimal("0.001"))


def parse_reference_unit_cost(raw: str | None) -> Decimal | None:
    s = (raw or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        v = Decimal(s)
    except InvalidOperation as exc:
        raise CatalogError("سعر التكلفة المرجعي يجب أن يكون رقماً.") from exc
    if v < 0:
        raise CatalogError("سعر التكلفة المرجعي لا يمكن أن يكون سالباً.")
    if v <= 0:
        return None
    return v.quantize(Decimal("0.001"))


def build_catalog_product_pricing_map(
    db: Session,
    products: list[Product],
    *,
    avg_costs: dict[int, Decimal] | None = None,
) -> dict[int, CatalogProductPricing]:
    """سعر التكلفة والفارق (بيع − تكلفة) لكل صنف في القائمة."""
    if avg_costs is None:
        avg_costs = avg_unit_cost_per_product(db)
    out: dict[int, CatalogProductPricing] = {}
    for p in products:
        cost = catalog_product_unit_cost(db, p.id, avg_costs)
        sell = p.sell_price
        margin: Decimal | None = None
        pricing_error = False
        if sell is not None and cost > 0:
            margin = (Decimal(str(sell)) - cost).quantize(Decimal("0.001"))
            pricing_error = margin < 0
        out[int(p.id)] = CatalogProductPricing(
            unit_cost=cost if cost > 0 else None,
            margin=margin,
            pricing_error=pricing_error,
        )
    return out


def sort_products_pricing_errors_first(
    products: list[Product],
    pricing: dict[int, CatalogProductPricing],
) -> list[Product]:
    def _key(p: Product) -> tuple:
        pr = pricing.get(int(p.id))
        if pr and pr.pricing_error and pr.margin is not None:
            return (0, pr.margin, -int(p.id))
        return (1, 0, -int(p.id))

    return sorted(products, key=_key)
