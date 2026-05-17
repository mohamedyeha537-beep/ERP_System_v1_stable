from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import ADMIN_ROLES, CATALOG_WRITE
from modules.catalog.models import BillOfMaterialsLine, Product, ProductCategory, ProductKind, ProductUnit
from modules.kds.models import KitchenDepartment
from modules.catalog.service import (
    CatalogError,
    assert_barcode_available,
    assert_unit_can_delete,
    validate_bom_line,
)
from modules.catalog.uploads import save_product_image

router = APIRouter(prefix="/catalog", tags=["catalog"])
_perm = require_permission(CATALOG_WRITE)
_admin_perm = require_permission(ADMIN_ROLES)

STATIC_ROOT = Path(__file__).resolve().parents[2] / "app" / "static"


def _category_rows(db):
    roots = list(
        db.scalars(
            select(ProductCategory)
            .where(ProductCategory.parent_id.is_(None))
            .order_by(ProductCategory.sort_order, ProductCategory.id)
        ).all()
    )
    rows: list[tuple[int, str]] = []
    for r in roots:
        rows.append((r.id, r.name_ar))
        subs = list(
            db.scalars(
                select(ProductCategory)
                .where(ProductCategory.parent_id == r.id)
                .order_by(ProductCategory.sort_order, ProductCategory.id)
            ).all()
        )
        for ch in subs:
            rows.append((ch.id, f"— {ch.name_ar}"))
    return rows


def _department_rows(db):
    return list(
        db.scalars(
            select(KitchenDepartment)
            .where(KitchenDepartment.is_active.is_(True))
            .order_by(KitchenDepartment.venue, KitchenDepartment.sort_order, KitchenDepartment.id)
        ).all()
    )


def _form_ctx(db, product=None, error=None):
    return {
        "request": None,
        "product": product,
        "error": error,
        "category_rows": _category_rows(db),
        "department_rows": _department_rows(db),
        "unit_rows": _unit_rows(db),
    }


def _unit_rows(db):
    return list(
        db.scalars(select(ProductUnit).order_by(ProductUnit.sort_order, ProductUnit.id)).all()
    )


@router.get("/products", response_class=HTMLResponse)
def list_products(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
):
    rows = list(
        db.scalars(
            select(Product).options(selectinload(Product.category)).order_by(Product.id.desc())
        ).all()
    )
    return templates.TemplateResponse("catalog_products.html", {"request": request, "products": rows})


@router.get("/products/new", response_class=HTMLResponse)
def new_product_form(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
):
    ctx = _form_ctx(db)
    ctx["request"] = request
    return templates.TemplateResponse("catalog_product_form.html", ctx)


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
    reorder_level: str = Form("0"),
    notes: str = Form(""),
    category_id: str = Form(""),
    kitchen_department_id: str = Form(""),
    image: UploadFile | None = File(None),
):
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
        ctx = _form_ctx(db, error="سعر البيع مطلوب للمنتج النهائي.")
        ctx["request"] = request
        return templates.TemplateResponse("catalog_product_form.html", ctx, status_code=400)
    if k == ProductKind.STOCK_ONLY:
        sp = None
    unit_name = unit.strip()
    valid_units = {u.name_ar for u in _unit_rows(db)}
    if not unit_name or unit_name not in valid_units:
        ctx = _form_ctx(db, error="اختر وحدة صالحة من قائمة الوحدات.")
        ctx["request"] = request
        return templates.TemplateResponse("catalog_product_form.html", ctx, status_code=400)
    cat_id = int(category_id) if category_id.strip().isdigit() else None
    dept_id = int(kitchen_department_id) if kitchen_department_id.strip().isdigit() else None
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
        reorder_level=Decimal(reorder_level.strip() or "0"),
        notes=notes.strip() or None,
        category_id=cat_id,
        kitchen_department_id=dept_id,
    )
    db.add(p)
    db.flush()
    if image and image.filename:
        try:
            p.image_filename = save_product_image(image, STATIC_ROOT)
        except ValueError as e:
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
    ctx = _form_ctx(db, product=p)
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
    reorder_level: str = Form("0"),
    notes: str = Form(""),
    category_id: str = Form(""),
    kitchen_department_id: str = Form(""),
    is_active: str = Form(""),
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
        ctx = _form_ctx(db, product=p, error="سعر البيع مطلوب للمنتج النهائي.")
        ctx["request"] = request
        return templates.TemplateResponse("catalog_product_form.html", ctx, status_code=400)
    if k == ProductKind.STOCK_ONLY:
        sp = None
    unit_name = unit.strip()
    valid_units = {u.name_ar for u in _unit_rows(db)}
    if not unit_name or unit_name not in valid_units:
        ctx = _form_ctx(db, product=p, error="اختر وحدة صالحة من قائمة الوحدات.")
        ctx["request"] = request
        return templates.TemplateResponse("catalog_product_form.html", ctx, status_code=400)
    cat_id = int(category_id) if category_id.strip().isdigit() else None
    dept_id = int(kitchen_department_id) if kitchen_department_id.strip().isdigit() else None
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
    p.reorder_level = Decimal(reorder_level.strip() or "0")
    p.notes = notes.strip() or None
    p.category_id = cat_id
    p.kitchen_department_id = dept_id
    p.is_active = is_active == "on"
    if image and image.filename:
        try:
            p.image_filename = save_product_image(image, STATIC_ROOT)
        except ValueError as e:
            ctx = _form_ctx(db, product=p, error=str(e))
            ctx["request"] = request
            return templates.TemplateResponse("catalog_product_form.html", ctx, status_code=400)
    db.commit()
    return RedirectResponse("/catalog/products", status_code=302)


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
    components = list(
        db.scalars(
            select(Product)
            .where(Product.kind == ProductKind.STOCK_ONLY, Product.is_active.is_(True))
            .order_by(Product.name_ar)
        ).all()
    )
    return templates.TemplateResponse(
        "catalog_bom.html",
        {"request": request, "parent": parent, "lines": parent.bom_lines_as_parent, "components": components, "error": None},
    )


@router.post("/products/{product_id}/bom/add", response_class=HTMLResponse)
def bom_add(
    request: Request,
    product_id: int,
    db: DBSession,
    _: User = Depends(_perm),
    component_product_id: int = Form(...),
    qty_per_parent: str = Form(...),
):
    parent = db.get(Product, product_id)
    if parent is None:
        return RedirectResponse("/catalog/products", status_code=302)
    try:
        qty = Decimal(qty_per_parent.strip())
        validate_bom_line(db, parent, component_product_id, qty)
    except CatalogError as e:
        components = list(
            db.scalars(
                select(Product)
                .where(Product.kind == ProductKind.STOCK_ONLY, Product.is_active.is_(True))
                .order_by(Product.name_ar)
            ).all()
        )
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
        return templates.TemplateResponse(
            "catalog_bom.html",
            {
                "request": request,
                "parent": parent,
                "lines": parent.bom_lines_as_parent,
                "components": components,
                "error": str(e),
            },
            status_code=400,
        )
    line = BillOfMaterialsLine(
        parent_product_id=parent.id,
        component_product_id=component_product_id,
        qty_per_parent=qty,
    )
    db.add(line)
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
        db.delete(line)
        db.commit()
    return RedirectResponse(f"/catalog/products/{product_id}/bom", status_code=302)


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
