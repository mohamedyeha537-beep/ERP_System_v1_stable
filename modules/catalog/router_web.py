from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from infra.schema_bootstrap import ensure_schema_patched, reset_schema_patch_flag
from modules.authz.models import User
from modules.authz.permissions import ADMIN_ROLES, CATALOG_WRITE
from modules.catalog.models import BillOfMaterialsLine, Product, ProductCategory, ProductKind, ProductUnit
from modules.catalog.service import (
    CatalogError,
    assert_barcode_available,
    assert_unit_can_delete,
    bulk_apply_products,
    bulk_fill_reference_costs_from_purchase,
    bulk_product_deletable,
    delete_product,
    validate_bom_line,
)
from modules.catalog.expiry import apply_product_expiry_form
from modules.catalog.bom_pricing import (
    apply_bom_pricing_to_product,
    bom_line_costs,
    bom_total_cost,
    build_catalog_product_pricing_map,
    global_bom_adjust_pct,
    parse_markup_pct,
    parse_reference_unit_cost,
    save_bom_line_edits,
    sort_products_pricing_errors_first,
    suggested_sell_price,
)
from modules.catalog.uploads import save_product_image
from modules.sales.line_notes import (
    get_modifier_presets,
    get_product_modifier_presets,
    parse_preset_form_values,
    save_product_modifier_selection,
    sync_product_modifier_presets,
)

router = APIRouter(prefix="/catalog", tags=["catalog"])
_perm = require_permission(CATALOG_WRITE)
_admin_perm = require_permission(ADMIN_ROLES)

CATALOG_PRODUCTS_PAGE_SIZE_DEFAULT = 50
CATALOG_PRODUCTS_PAGE_SIZE_OPTIONS = (25, 50, 100, 200)

STATIC_ROOT = Path(__file__).resolve().parents[2] / "app" / "static"


def _is_missing_column_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "no such column" in msg or "unknown column" in msg


def _category_rows(db):
    """قائمة الفئات للنموذج — SQL مباشر (لا يعتمد على أعمدة ORM الجديدة)."""
    from sqlalchemy import text

    sql = text(
        """
        SELECT id, name_ar, parent_id, sort_order
        FROM product_categories
        ORDER BY CASE WHEN parent_id IS NULL THEN 0 ELSE 1 END,
                 sort_order, id
        """
    )
    raw = db.execute(sql).mappings().all()
    by_parent: dict[int | None, list] = {}
    for row in raw:
        by_parent.setdefault(row["parent_id"], []).append(row)
    out: list[tuple[int, str]] = []
    for root in by_parent.get(None, []):
        out.append((int(root["id"]), str(root["name_ar"])))
        for ch in by_parent.get(int(root["id"]), []):
            out.append((int(ch["id"]), f"— {ch['name_ar']}"))
    return out


def _form_ctx(db, product=None, error=None, modifier_saved=False):
    ensure_schema_patched()
    global_presets = get_modifier_presets(db)
    selected: list[str] = (
        get_product_modifier_presets(product, pool=global_presets)
        if product
        else []
    )
    return {
        "request": None,
        "product": product,
        "error": error,
        "category_rows": _category_rows(db),
        "unit_rows": _unit_rows(db),
        "global_modifier_presets": global_presets,
        "product_modifier_presets": selected,
        "modifier_saved": modifier_saved,
    }


def _unit_rows(db):
    return list(
        db.scalars(select(ProductUnit).order_by(ProductUnit.sort_order, ProductUnit.id)).all()
    )


def _apply_products_list_filters(
    stmt,
    *,
    kind: str | None,
    category: str | None,
    pos: str | None,
    q: str | None,
) -> tuple[object, str, str, str, str]:
    kind_filter = "all"
    if kind in (ProductKind.FINAL_SELLABLE.value, ProductKind.STOCK_ONLY.value):
        stmt = stmt.where(Product.kind == ProductKind(kind))
        kind_filter = kind
    category_filter = "all"
    if category == "none":
        stmt = stmt.where(Product.category_id.is_(None))
        category_filter = "none"
    pos_filter = "all"
    if pos == "in_pos":
        stmt = stmt.where(
            Product.kind == ProductKind.FINAL_SELLABLE,
            Product.show_in_pos.is_(True),
            Product.is_active.is_(True),
        )
        pos_filter = "in_pos"
    elif pos == "hidden":
        stmt = stmt.where(
            Product.kind == ProductKind.FINAL_SELLABLE,
            Product.show_in_pos.is_(False),
        )
        pos_filter = "hidden"
    search_q = (q or "").strip()
    if search_q:
        term = f"%{search_q}%"
        stmt = stmt.where(
            (Product.name_ar.like(term))
            | (Product.sku.like(term))
            | (Product.barcode.like(term))
        )
    return stmt, kind_filter, category_filter, pos_filter, search_q


@router.get("/products", response_class=HTMLResponse)
def list_products(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    kind: str | None = None,
    category: str | None = None,
    pos: str | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(CATALOG_PRODUCTS_PAGE_SIZE_DEFAULT, ge=10, le=200),
    msg: str | None = None,
    err: str | None = None,
):
    ensure_schema_patched()
    if page_size not in CATALOG_PRODUCTS_PAGE_SIZE_OPTIONS:
        page_size = CATALOG_PRODUCTS_PAGE_SIZE_DEFAULT

    count_stmt = select(func.count()).select_from(Product)
    count_stmt, kind_filter, category_filter, pos_filter, search_q = _apply_products_list_filters(
        count_stmt, kind=kind, category=category, pos=pos, q=q
    )
    total_count = int(db.scalar(count_stmt) or 0)
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages

    stmt = select(Product).options(selectinload(Product.category))
    stmt, _, _, _, _ = _apply_products_list_filters(
        stmt, kind=kind, category=category, pos=pos, q=q
    )
    all_rows = list(db.scalars(stmt).all())
    pricing_map = build_catalog_product_pricing_map(db, all_rows)
    all_rows = sort_products_pricing_errors_first(all_rows, pricing_map)
    pricing_error_count = sum(1 for pr in pricing_map.values() if pr.pricing_error)
    offset = (page - 1) * page_size
    rows = all_rows[offset : offset + page_size]
    deletable = bulk_product_deletable(db, [p.id for p in rows])
    products_query = _products_list_query(
        kind=kind_filter,
        category=category_filter,
        pos=pos_filter,
        search=search_q,
        page=page,
        page_size=page_size,
    )
    filters_query = _products_list_query(
        kind=kind_filter,
        category=category_filter,
        pos=pos_filter,
        search="",
    )
    list_base_query = _products_list_query(
        kind=kind_filter,
        category=category_filter,
        pos=pos_filter,
        search=search_q,
        page=None,
        page_size=page_size,
    )
    return templates.TemplateResponse(
        "catalog_products.html",
        {
            "request": request,
            "products": rows,
            "product_pricing": pricing_map,
            "pricing_error_count": pricing_error_count,
            "kind_filter": kind_filter,
            "category_filter": category_filter,
            "pos_filter": pos_filter,
            "search_q": search_q,
            "products_query": products_query,
            "filters_query": filters_query,
            "list_base_query": list_base_query,
            "deletable": deletable,
            "flash_msg": msg,
            "flash_err": err,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total_count": total_count,
            "page_size_options": CATALOG_PRODUCTS_PAGE_SIZE_OPTIONS,
            "page_from": offset + 1 if rows else 0,
            "page_to": offset + len(rows),
        },
    )


def _products_list_query(
    *,
    kind: str = "all",
    category: str = "all",
    pos: str = "all",
    search: str = "",
    page: int | None = None,
    page_size: int | None = None,
) -> str:
    params: list[str] = []
    if kind != "all":
        params.append(f"kind={quote(kind)}")
    if category != "all":
        params.append(f"category={quote(category)}")
    if pos != "all":
        params.append(f"pos={quote(pos)}")
    if search.strip():
        params.append(f"q={quote(search.strip())}")
    if page is not None and page > 1:
        params.append(f"page={page}")
    if page_size is not None and page_size != CATALOG_PRODUCTS_PAGE_SIZE_DEFAULT:
        params.append(f"page_size={page_size}")
    return "&".join(params)


def _products_list_url(
    *,
    kind: str = "all",
    category: str = "all",
    pos: str = "all",
    search: str = "",
    page: int | None = None,
    page_size: int | None = None,
) -> str:
    qs = _products_list_query(
        kind=kind,
        category=category,
        pos=pos,
        search=search,
        page=page,
        page_size=page_size,
    )
    return f"/catalog/products{'?' + qs if qs else ''}"


def _parse_products_paging(page: str = "1", page_size: str = "") -> tuple[int, int]:
    try:
        p = max(1, int((page or "1").strip()))
    except ValueError:
        p = 1
    try:
        ps = int((page_size or str(CATALOG_PRODUCTS_PAGE_SIZE_DEFAULT)).strip())
    except ValueError:
        ps = CATALOG_PRODUCTS_PAGE_SIZE_DEFAULT
    if ps not in CATALOG_PRODUCTS_PAGE_SIZE_OPTIONS:
        ps = CATALOG_PRODUCTS_PAGE_SIZE_DEFAULT
    return p, ps


@router.post("/products/bulk", response_class=HTMLResponse)
async def products_bulk_action(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    action: str = Form(...),
    kind: str = Form("all"),
    category: str = Form("all"),
    pos: str = Form("all"),
    q: str = Form(""),
    page: str = Form("1"),
    page_size: str = Form(""),
):
    form = await request.form()
    raw_ids = form.getlist("product_ids") if hasattr(form, "getlist") else []
    ids: list[int] = []
    for raw in raw_ids:
        if str(raw).isdigit():
            ids.append(int(raw))
    result = bulk_apply_products(db, ids, action.strip())
    db.commit()
    pg, ps = _parse_products_paging(page, page_size)
    back = _products_list_url(
        kind=kind, category=category, pos=pos, search=q, page=pg, page_size=ps
    )
    if result.errors:
        return RedirectResponse(
            back + ("&" if "?" in back else "?") + "err=" + quote(result.errors[0]),
            status_code=302,
        )
    parts: list[str] = []
    if result.updated:
        parts.append(f"تم تحديث {result.updated} صنف")
    if result.deleted:
        parts.append(f"حُذف {result.deleted} صنف")
    if result.skipped:
        parts.append(f"تُرك {result.skipped} بدون تغيير")
    msg = " · ".join(parts) if parts else "لم يُنفَّذ أي تغيير."
    return RedirectResponse(
        back + ("&" if "?" in back else "?") + "msg=" + quote(msg),
        status_code=302,
    )


@router.post("/products/fill-reference-costs", response_class=HTMLResponse)
def products_fill_reference_costs(
    db: DBSession,
    _: User = Depends(_perm),
    overwrite: str = Form(""),
):
    """نسخ متوسط شراء المخزون إلى التكلفة المرجعية لكل الأصناف."""
    result = bulk_fill_reference_costs_from_purchase(db, overwrite=overwrite == "on")
    db.commit()
    parts: list[str] = []
    if result.updated:
        parts.append(f"عُيّنت تكلفة مرجعية لـ {result.updated} صنف")
    if result.skipped:
        parts.append(f"تُرك {result.skipped} بدون تغيير (لا شراء سابق أو لديه مرجع)")
    msg = " · ".join(parts) if parts else "لم يُحدَّث أي صنف — لا توجد فواتير شراء بأسعار."
    return RedirectResponse("/catalog/products?msg=" + quote(msg), status_code=302)


@router.post("/products/{product_id}/delete", response_class=HTMLResponse)
def product_delete(
    product_id: int,
    db: DBSession,
    _: User = Depends(_perm),
    kind: str = Form("all"),
    category: str = Form("all"),
    pos: str = Form("all"),
    q: str = Form(""),
    page: str = Form("1"),
    page_size: str = Form(""),
):
    p = db.get(Product, product_id)
    pg, ps = _parse_products_paging(page, page_size)
    back = _products_list_url(
        kind=kind, category=category, pos=pos, search=q, page=pg, page_size=ps
    )
    if p is None:
        return RedirectResponse(back + ("&" if "?" in back else "?") + "err=" + quote("الصنف غير موجود."), status_code=302)
    name = p.name_ar
    try:
        delete_product(db, p)
        db.commit()
        return RedirectResponse(
            back + ("&" if "?" in back else "?") + "msg=" + quote(f"حُذف «{name}»."),
            status_code=302,
        )
    except CatalogError as e:
        db.rollback()
        return RedirectResponse(back + ("&" if "?" in back else "?") + "err=" + quote(str(e)), status_code=302)


@router.post("/products/{product_id}/reorder-level")
def product_reorder_level_inline(
    request: Request,
    product_id: int,
    db: DBSession,
    _: User = Depends(_perm),
    reorder_level: str = Form("0"),
    kind: str = Form("all"),
    category: str = Form("all"),
    pos: str = Form("all"),
    q: str = Form(""),
    page: str = Form("1"),
    page_size: str = Form(""),
):
    from decimal import InvalidOperation
    from fastapi.responses import JSONResponse, Response

    p = db.get(Product, product_id)
    pg, ps = _parse_products_paging(page, page_size)
    back = _products_list_url(
        kind=kind, category=category, pos=pos, search=q, page=pg, page_size=ps
    )
    if p is None:
        if request.headers.get("HX-Request") or request.headers.get("X-Inline-Save"):
            return Response(status_code=404)
        return RedirectResponse(
            back + ("&" if "?" in back else "?") + "err=" + quote("الصنف غير موجود."),
            status_code=302,
        )
    try:
        val = Decimal((reorder_level or "0").strip().replace(",", "") or "0")
        if val < 0:
            raise InvalidOperation
        p.reorder_level = val.quantize(Decimal("0.001"))
        db.commit()
    except (InvalidOperation, ValueError):
        db.rollback()
        if request.headers.get("HX-Request") or request.headers.get("X-Inline-Save"):
            return JSONResponse({"ok": False, "error": "قيمة غير صالحة"}, status_code=400)
        return RedirectResponse(
            back + ("&" if "?" in back else "?") + "err=" + quote("قيمة الحد الأدنى غير صالحة."),
            status_code=302,
        )
    saved = format(val.normalize(), "f").rstrip("0").rstrip(".") or "0"
    if request.headers.get("HX-Request"):
        return Response(status_code=204)
    if request.headers.get("X-Inline-Save"):
        return JSONResponse({"ok": True, "value": saved})
    return RedirectResponse(
        back + ("&" if "?" in back else "?") + "msg=" + quote(f"تم تحديث حد طلب الشراء لـ «{p.name_ar}»."),
        status_code=302,
    )


@router.post("/products/hide-uncategorized-from-pos", response_class=HTMLResponse)
def hide_uncategorized_from_pos(
    db: DBSession,
    _: User = Depends(_perm),
):
    """إخفاء كل المنتجات النهائية بدون فئة من جلسة البيع (زر سريع)."""
    rows = list(
        db.scalars(
            select(Product).where(
                Product.category_id.is_(None),
                Product.kind == ProductKind.FINAL_SELLABLE,
            )
        ).all()
    )
    n = 0
    for p in rows:
        p.show_in_pos = False
        n += 1
    db.commit()
    return RedirectResponse(
        "/catalog/products?category=none&msg=" + quote(f"أُخفيت {n} صنفاً بدون فئة من جلسة البيع."),
        status_code=302,
    )


@router.get("/products/new", response_class=HTMLResponse)
def new_product_form(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
):
    try:
        ctx = _form_ctx(db)
        ctx["request"] = request
        return templates.TemplateResponse("catalog_product_form.html", ctx)
    except Exception as exc:
        if _is_missing_column_error(exc):
            reset_schema_patch_flag()
            ensure_schema_patched(force=True)
            try:
                ctx = _form_ctx(db)
                ctx["request"] = request
                return templates.TemplateResponse("catalog_product_form.html", ctx)
            except Exception:
                return HTMLResponse(
                    "<h1>خطأ في قاعدة البيانات</h1>"
                    "<p>أعمدة الكatalog ناقصة على السيرفر. شغّل على VPS:</p>"
                    "<pre>python tools/fix_mysql_catalog_schema.py</pre>"
                    "<p>ثم أعد تشغيل الخدمة: <code>sudo systemctl restart pos</code></p>",
                    status_code=503,
                )
        raise


@router.post("/products/new", response_class=HTMLResponse)
async def new_product_submit(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    name_ar: str = Form(...),
    sku: str = Form(""),
    barcode: str = Form(""),
    unit: str = Form("قطعة"),
    kind: str = Form(...),
    sell_price: str = Form(""),
    reference_unit_cost: str = Form(""),
    reorder_level: str = Form("0"),
    notes: str = Form(""),
    category_id: str = Form(""),
    expiry_tracked: str = Form(""),
    expiry_warn_days: str = Form("7"),
    show_in_pos: str = Form("on"),
    show_in_shop: str = Form("on"),
    direct_purchase_enabled: str = Form("on"),
    is_hotel_breakfast: str = Form(""),
    image: UploadFile | None = File(None),
):
    ensure_schema_patched()
    form = await request.form()
    preset = parse_preset_form_values(form)
    try:
        k = ProductKind(kind)
    except ValueError:
        ctx = _form_ctx(db, error="نوع الصنف غير صالح.")
        ctx["request"] = request
        return templates.TemplateResponse("catalog_product_form.html", ctx, status_code=400)
    sp = None
    if sell_price.strip():
        sp = Decimal(sell_price.strip())
    if k == ProductKind.FINAL_SELLABLE and sp is None:
        ctx = _form_ctx(
            db,
            error="سعر البيع مطلوب عند اختيار «منتج نهائي». للمكوّنات (بقدونس، توابل…) اختر «مخزني فقط».",
        )
        ctx["request"] = request
        return templates.TemplateResponse("catalog_product_form.html", ctx, status_code=400)
    if k == ProductKind.STOCK_ONLY:
        sp = None
    try:
        ref_cost = parse_reference_unit_cost(reference_unit_cost)
    except CatalogError as e:
        ctx = _form_ctx(db, error=str(e))
        ctx["request"] = request
        return templates.TemplateResponse("catalog_product_form.html", ctx, status_code=400)
    unit_name = unit.strip()
    valid_units = {u.name_ar for u in _unit_rows(db)}
    if not unit_name or unit_name not in valid_units:
        ctx = _form_ctx(db, error="اختر وحدة صالحة من قائمة الوحدات.")
        ctx["request"] = request
        return templates.TemplateResponse("catalog_product_form.html", ctx, status_code=400)
    cat_id = int(category_id) if category_id.strip().isdigit() else None
    bc = barcode.strip() or None
    try:
        assert_barcode_available(db, bc, None)
    except CatalogError as e:
        ctx = _form_ctx(db, error=str(e))
        ctx["request"] = request
        return templates.TemplateResponse("catalog_product_form.html", ctx, status_code=400)
    p = Product(
        name_ar=name_ar.strip(),
        sku=sku.strip() or None,
        barcode=bc,
        unit=unit_name,
        kind=k,
        sell_price=sp,
        reference_unit_cost=ref_cost,
        reorder_level=Decimal(reorder_level.strip() or "0"),
        notes=notes.strip() or None,
        category_id=cat_id,
        show_in_pos=k == ProductKind.FINAL_SELLABLE and show_in_pos == "on",
        show_in_shop=k == ProductKind.FINAL_SELLABLE and show_in_shop == "on",
        direct_purchase_enabled=(
            k == ProductKind.FINAL_SELLABLE and direct_purchase_enabled == "on"
        ),
        is_hotel_breakfast=is_hotel_breakfast == "on",
    )
    db.add(p)
    db.flush()
    save_product_modifier_selection(p, preset, pool=get_modifier_presets(db))
    from modules.kds.section_rules import assign_product_kitchen_section_from_category

    assign_product_kitchen_section_from_category(db, p, force=False)
    if image and image.filename:
        try:
            p.image_filename = save_product_image(image, STATIC_ROOT)
        except ValueError as e:
            db.rollback()
            ctx = _form_ctx(db, error=str(e))
            ctx["request"] = request
            return templates.TemplateResponse("catalog_product_form.html", ctx, status_code=400)
    try:
        apply_product_expiry_form(
            p,
            expiry_tracked=expiry_tracked,
            expiry_warn_days=expiry_warn_days,
        )
    except CatalogError as e:
        db.rollback()
        ctx = _form_ctx(db, error=str(e))
        ctx["request"] = request
        return templates.TemplateResponse("catalog_product_form.html", ctx, status_code=400)
    db.commit()
    if k == ProductKind.FINAL_SELLABLE:
        return RedirectResponse(f"/catalog/products/{p.id}/bom", status_code=302)
    return RedirectResponse("/catalog/products", status_code=302)


@router.get("/products/{product_id}/edit", response_class=HTMLResponse)
def edit_product_form(
    request: Request,
    product_id: int,
    db: DBSession,
    _: User = Depends(_perm),
):
    p = db.get(Product, product_id)
    if p is None:
        return RedirectResponse("/catalog/products", status_code=302)
    sync_product_modifier_presets(db, p)
    db.commit()
    saved = request.query_params.get("modifier_saved") == "1"
    ctx = _form_ctx(db, product=p, modifier_saved=saved)
    ctx["request"] = request
    return templates.TemplateResponse("catalog_product_form.html", ctx)


@router.post("/products/{product_id}/edit", response_class=HTMLResponse)
async def edit_product_submit(
    request: Request,
    product_id: int,
    db: DBSession,
    _: User = Depends(_perm),
    name_ar: str = Form(...),
    sku: str = Form(""),
    barcode: str = Form(""),
    unit: str = Form("قطعة"),
    kind: str = Form(...),
    sell_price: str = Form(""),
    reference_unit_cost: str = Form(""),
    reorder_level: str = Form("0"),
    notes: str = Form(""),
    category_id: str = Form(""),
    is_active: str = Form(""),
    show_in_pos: str = Form(""),
    show_in_shop: str = Form(""),
    direct_purchase_enabled: str = Form(""),
    is_hotel_breakfast: str = Form(""),
    expiry_tracked: str = Form(""),
    expiry_warn_days: str = Form("7"),
    image: UploadFile | None = File(None),
):
    p = db.get(Product, product_id)
    if p is None:
        return RedirectResponse("/catalog/products", status_code=302)
    try:
        k = ProductKind(kind)
    except ValueError:
        ctx = _form_ctx(db, product=p, error="نوع الصنف غير صالح.")
        ctx["request"] = request
        return templates.TemplateResponse("catalog_product_form.html", ctx, status_code=400)
    sp = None
    if sell_price.strip():
        sp = Decimal(sell_price.strip())
    if k == ProductKind.FINAL_SELLABLE and sp is None:
        ctx = _form_ctx(
            db,
            product=p,
            error="سعر البيع مطلوب عند اختيار «منتج نهائي». للمكوّنات اختر «مخزني فقط».",
        )
        ctx["request"] = request
        return templates.TemplateResponse("catalog_product_form.html", ctx, status_code=400)
    if k == ProductKind.STOCK_ONLY:
        sp = None
    try:
        ref_cost = parse_reference_unit_cost(reference_unit_cost)
    except CatalogError as e:
        ctx = _form_ctx(db, product=p, error=str(e))
        ctx["request"] = request
        return templates.TemplateResponse("catalog_product_form.html", ctx, status_code=400)
    unit_name = unit.strip()
    valid_units = {u.name_ar for u in _unit_rows(db)}
    if not unit_name or unit_name not in valid_units:
        ctx = _form_ctx(db, product=p, error="اختر وحدة صالحة من قائمة الوحدات.")
        ctx["request"] = request
        return templates.TemplateResponse("catalog_product_form.html", ctx, status_code=400)
    cat_id = int(category_id) if category_id.strip().isdigit() else None
    bc = barcode.strip() or None
    try:
        assert_barcode_available(db, bc, p.id)
    except CatalogError as e:
        ctx = _form_ctx(db, product=p, error=str(e))
        ctx["request"] = request
        return templates.TemplateResponse("catalog_product_form.html", ctx, status_code=400)
    p.name_ar = name_ar.strip()
    p.sku = sku.strip() or None
    p.barcode = bc
    p.unit = unit_name
    p.kind = k
    p.sell_price = sp
    p.reference_unit_cost = ref_cost
    p.reorder_level = Decimal(reorder_level.strip() or "0")
    p.notes = notes.strip() or None
    p.category_id = cat_id
    from modules.kds.section_rules import assign_product_kitchen_section_from_category

    assign_product_kitchen_section_from_category(db, p, force=False)
    p.is_active = is_active == "on"
    p.is_hotel_breakfast = is_hotel_breakfast == "on"
    if k == ProductKind.STOCK_ONLY:
        p.show_in_pos = False
        p.show_in_shop = False
        p.direct_purchase_enabled = False
    else:
        p.show_in_pos = show_in_pos == "on"
        p.show_in_shop = show_in_shop == "on"
        p.direct_purchase_enabled = direct_purchase_enabled == "on"
    if image and image.filename:
        try:
            p.image_filename = save_product_image(image, STATIC_ROOT)
        except ValueError as e:
            ctx = _form_ctx(db, product=p, error=str(e))
            ctx["request"] = request
            return templates.TemplateResponse("catalog_product_form.html", ctx, status_code=400)
    try:
        apply_product_expiry_form(
            p,
            expiry_tracked=expiry_tracked,
            expiry_warn_days=expiry_warn_days,
        )
    except CatalogError as e:
        ctx = _form_ctx(db, product=p, error=str(e))
        ctx["request"] = request
        return templates.TemplateResponse("catalog_product_form.html", ctx, status_code=400)
    db.commit()
    return RedirectResponse("/catalog/products", status_code=302)


@router.post("/products/{product_id}/line-modifiers/save", response_class=HTMLResponse)
async def product_line_modifiers_save(
    request: Request,
    product_id: int,
    db: DBSession,
    _: User = Depends(_perm),
):
    form = await request.form()
    preset = parse_preset_form_values(form)
    p = db.get(Product, product_id)
    if p is None:
        return RedirectResponse("/catalog/products", status_code=302)
    save_product_modifier_selection(p, preset, pool=get_modifier_presets(db))
    db.commit()
    return RedirectResponse(
        f"/catalog/products/{product_id}/edit?modifier_saved=1",
        status_code=302,
    )


@router.get("/products/{product_id}/bom", response_class=HTMLResponse)
def bom_page(
    request: Request,
    product_id: int,
    db: DBSession,
    _: User = Depends(_perm),
):
    stmt = (
        select(Product)
        .where(Product.id == product_id)
        .options(
            selectinload(Product.bom_lines_as_parent).selectinload(BillOfMaterialsLine.component_product),
        )
    )
    parent = db.execute(stmt).scalar_one_or_none()
    if parent is None:
        return RedirectResponse("/catalog/products", status_code=302)
    ctx = _bom_page_context(
        db,
        parent,
        error=request.query_params.get("err"),
        pricing_saved=request.query_params.get("pricing_saved") == "1",
        cost_saved=request.query_params.get("cost_saved"),
        lines_saved=request.query_params.get("lines_saved") == "1",
    )
    ctx["request"] = request
    return templates.TemplateResponse("catalog_bom.html", ctx)


@router.post("/products/{product_id}/bom/pricing", response_class=HTMLResponse)
def bom_pricing_save(
    product_id: int,
    db: DBSession,
    _: User = Depends(_perm),
    price_linked_to_bom: str = Form(""),
    bom_markup_pct: str = Form(""),
    sell_price: str = Form(""),
    pricing_mode: str = Form("linked"),
):
    from urllib.parse import quote

    parent = db.get(Product, product_id)
    if parent is None or parent.kind != ProductKind.FINAL_SELLABLE:
        return RedirectResponse(f"/catalog/products/{product_id}/bom", status_code=302)
    linked = (price_linked_to_bom or "").strip().lower() in ("on", "1", "true", "yes")
    manual = (pricing_mode or "").strip() == "manual"
    try:
        markup = parse_markup_pct(bom_markup_pct) if (bom_markup_pct or "").strip() else None
        sp: Decimal | None = None
        if (sell_price or "").strip():
            sp = Decimal((sell_price or "").strip().replace(",", "."))
        apply_bom_pricing_to_product(
            db,
            parent,
            price_linked_to_bom=linked and not manual,
            bom_markup_pct=markup,
            sell_price=sp,
            manual_price=manual,
        )
        db.commit()
    except (CatalogError, Exception) as exc:
        db.rollback()
        if not isinstance(exc, CatalogError):
            msg = "بيانات التسعير غير صالحة."
        else:
            msg = str(exc)
        return RedirectResponse(
            f"/catalog/products/{product_id}/bom?err={quote(msg)}",
            status_code=302,
        )
    return RedirectResponse(
        f"/catalog/products/{product_id}/bom?pricing_saved=1",
        status_code=302,
    )


def _bom_page_context(db: DBSession, parent: Product, *, error: str | None = None, pricing_saved: bool = False, cost_saved: str | None = None, lines_saved: bool = False) -> dict:
    cost_rows = bom_line_costs(db, parent.id)
    total_cost = bom_total_cost(db, parent.id)
    global_adj = global_bom_adjust_pct(db)
    suggested = suggested_sell_price(
        total_cost,
        product_markup_pct=parent.bom_markup_pct,
        global_adjust_pct=global_adj,
    )
    component_parent_ids = select(BillOfMaterialsLine.parent_product_id)
    components = list(
        db.scalars(
            select(Product)
            .where(Product.is_active.is_(True), Product.id != parent.id)
            .where(
                (Product.kind == ProductKind.STOCK_ONLY)
                | (
                    (Product.kind == ProductKind.FINAL_SELLABLE)
                    & Product.id.not_in(component_parent_ids)
                )
            )
            .order_by(Product.name_ar)
        ).all()
    )
    return {
        "parent": parent,
        "is_final_sellable": parent.kind == ProductKind.FINAL_SELLABLE,
        "is_semi_finished": parent.kind == ProductKind.STOCK_ONLY,
        "lines": parent.bom_lines_as_parent,
        "cost_rows": cost_rows,
        "cost_by_line": {r.line_id: r for r in cost_rows},
        "total_cost": total_cost,
        "global_adjust_pct": global_adj,
        "suggested_price": suggested,
        "components": components,
        "error": error,
        "pricing_saved": pricing_saved,
        "cost_saved": cost_saved,
        "lines_saved": lines_saved,
    }


@router.post("/products/{product_id}/bom/add", response_class=HTMLResponse)
def bom_add(
    request: Request,
    product_id: int,
    db: DBSession,
    _: User = Depends(_perm),
    component_product_id: int = Form(...),
    qty_per_parent: str = Form(...),
    packaging_only: str = Form(""),
):
    parent = db.get(Product, product_id)
    if parent is None:
        return RedirectResponse("/catalog/products", status_code=302)
    try:
        qty = Decimal(qty_per_parent.strip())
        validate_bom_line(db, parent, component_product_id, qty)
    except CatalogError as e:
        stmt = (
            select(Product)
            .where(Product.id == product_id)
            .options(
                selectinload(Product.bom_lines_as_parent).selectinload(
                    BillOfMaterialsLine.component_product
                ),
            )
        )
        parent = db.execute(stmt).scalar_one_or_none()
        ctx = _bom_page_context(db, parent, error=str(e))
        ctx["request"] = request
        return templates.TemplateResponse("catalog_bom.html", ctx, status_code=400)
    line = BillOfMaterialsLine(
        parent_product_id=parent.id,
        component_product_id=component_product_id,
        qty_per_parent=qty,
        packaging_only=packaging_only == "on",
    )
    db.add(line)
    db.flush()
    if parent.price_linked_to_bom and parent.bom_markup_pct is not None:
        parent.sell_price = suggested_sell_price(
            bom_total_cost(db, parent.id),
            product_markup_pct=parent.bom_markup_pct,
            global_adjust_pct=global_bom_adjust_pct(db),
        )
    db.commit()
    return RedirectResponse(f"/catalog/products/{product_id}/bom", status_code=302)


@router.post("/products/{product_id}/bom/{line_id}/delete", response_class=HTMLResponse)
def bom_delete(
    product_id: int,
    line_id: int,
    db: DBSession,
    _: User = Depends(_perm),
):
    line = db.get(BillOfMaterialsLine, line_id)
    if line and line.parent_product_id == product_id:
        parent = db.get(Product, product_id)
        db.delete(line)
        db.flush()
        if parent and parent.price_linked_to_bom and parent.bom_markup_pct is not None:
            parent.sell_price = suggested_sell_price(
                bom_total_cost(db, parent.id),
                product_markup_pct=parent.bom_markup_pct,
                global_adjust_pct=global_bom_adjust_pct(db),
            )
        db.commit()
    return RedirectResponse(f"/catalog/products/{product_id}/bom", status_code=302)


@router.post("/products/{product_id}/bom/save-lines", response_class=HTMLResponse)
async def bom_save_lines(
    request: Request,
    product_id: int,
    db: DBSession,
    _: User = Depends(_perm),
):
    from decimal import InvalidOperation

    parent = db.execute(
        select(Product)
        .where(Product.id == product_id)
        .options(
            selectinload(Product.bom_lines_as_parent).selectinload(
                BillOfMaterialsLine.component_product
            )
        )
    ).scalar_one_or_none()
    if parent is None:
        return RedirectResponse("/catalog/products", status_code=302)
    form = await request.form()
    line_qty: dict[int, Decimal] = {}
    line_unit_cost: dict[int, Decimal | None] = {}
    line_packaging: dict[int, bool] = {}
    for key, val in form.multi_items():
        if key.startswith("qty_line_"):
            try:
                line_id = int(key.removeprefix("qty_line_"))
                line_qty[line_id] = Decimal((val or "").strip().replace(",", "."))
            except (ValueError, InvalidOperation):
                return RedirectResponse(
                    f"/catalog/products/{product_id}/bom?err={quote('كمية غير صالحة.')}",
                    status_code=302,
                )
        elif key.startswith("unit_cost_line_"):
            try:
                line_id = int(key.removeprefix("unit_cost_line_"))
                s = (val or "").strip().replace(",", ".")
                if not s:
                    line_unit_cost[line_id] = None
                else:
                    line_unit_cost[line_id] = Decimal(s)
            except (ValueError, InvalidOperation):
                return RedirectResponse(
                    f"/catalog/products/{product_id}/bom?err={quote('تكلفة غير صالحة.')}",
                    status_code=302,
                )
        elif key.startswith("packaging_line_"):
            try:
                line_id = int(key.removeprefix("packaging_line_"))
                line_packaging[line_id] = val == "on"
            except ValueError:
                pass
    try:
        old_cost = bom_total_cost(db, parent.id)
        save_bom_line_edits(db, parent, line_qty=line_qty, line_unit_cost=line_unit_cost)
        for line_id, is_pkg in line_packaging.items():
            ln = next((x for x in parent.bom_lines_as_parent if int(x.id) == int(line_id)), None)
            if ln is not None:
                ln.packaging_only = bool(is_pkg)
        db.flush()
        new_cost = bom_total_cost(db, parent.id)
        if parent.price_linked_to_bom and parent.bom_markup_pct is not None:
            parent.sell_price = suggested_sell_price(
                bom_total_cost(db, parent.id),
                product_markup_pct=parent.bom_markup_pct,
                global_adjust_pct=global_bom_adjust_pct(db),
            )
        db.commit()
        try:
            from modules.notifications.inventory_hooks import (
                emit_bom_missing_cost_if_needed,
                emit_recipe_cost_changed,
            )

            emit_recipe_cost_changed(
                db,
                product_id=parent.id,
                product_name=parent.name_ar or str(parent.id),
                old_cost=old_cost,
                new_cost=new_cost,
            )
            emit_bom_missing_cost_if_needed(db, parent.id)
            db.commit()
        except Exception:  # noqa: BLE001
            pass
    except CatalogError as exc:
        db.rollback()
        return RedirectResponse(
            f"/catalog/products/{product_id}/bom?err={quote(str(exc))}",
            status_code=302,
        )
    except Exception as exc:
        db.rollback()
        return RedirectResponse(
            f"/catalog/products/{product_id}/bom?err={quote('فشل حفظ التركيبة: ' + str(exc)[:240])}",
            status_code=302,
        )
    return RedirectResponse(
        f"/catalog/products/{product_id}/bom?lines_saved=1",
        status_code=302,
    )


@router.get("/units", response_class=HTMLResponse)
def units_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_admin_perm),
):
    return templates.TemplateResponse(
        "catalog_units.html",
        {"request": request, "units": _unit_rows(db), "error_message": None},
    )


@router.post("/units/add", response_class=HTMLResponse)
def units_add(
    request: Request,
    db: DBSession,
    _: User = Depends(_admin_perm),
    name_ar: str = Form(...),
    sort_order: str = Form("0"),
):
    name = name_ar.strip()
    if not name:
        return templates.TemplateResponse(
            "catalog_units.html",
            {"request": request, "units": _unit_rows(db), "error_message": "اسم الوحدة مطلوب."},
            status_code=400,
        )
    exists = db.execute(select(ProductUnit).where(ProductUnit.name_ar == name)).scalar_one_or_none()
    if exists is not None:
        return templates.TemplateResponse(
            "catalog_units.html",
            {"request": request, "units": _unit_rows(db), "error_message": "الوحدة موجودة مسبقاً."},
            status_code=400,
        )
    db.add(ProductUnit(name_ar=name, sort_order=int(sort_order.strip() or "0")))
    db.commit()
    return RedirectResponse("/catalog/units", status_code=302)


@router.post("/units/{unit_id}/delete", response_class=HTMLResponse)
def units_delete(
    request: Request,
    unit_id: int,
    db: DBSession,
    _: User = Depends(_admin_perm),
):
    unit = db.get(ProductUnit, unit_id)
    if unit is None:
        return RedirectResponse("/catalog/units", status_code=302)
    try:
        assert_unit_can_delete(db, unit.name_ar)
    except CatalogError as e:
        return templates.TemplateResponse(
            "catalog_units.html",
            {"request": request, "units": _unit_rows(db), "error_message": str(e)},
            status_code=400,
        )
    db.delete(unit)
    db.commit()
    return RedirectResponse("/catalog/units", status_code=302)
