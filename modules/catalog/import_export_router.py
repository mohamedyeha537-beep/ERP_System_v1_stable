"""تصدير/استيراد قوالب CSV للكتالوج."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import CATALOG_WRITE
from modules.catalog.import_export_service import (
    bom_csv_export,
    bom_csv_template,
    categories_csv_export,
    categories_csv_template,
    import_bom_csv,
    import_categories_csv,
    import_products_csv,
    products_csv_export,
    products_csv_template,
)

router = APIRouter(prefix="/import-export", tags=["catalog-import"])
_perm = require_permission(CATALOG_WRITE)


def _csv_response(content: str, filename: str) -> Response:
    return Response(
        content=content.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


async def _read_upload(file: UploadFile | None) -> str:
    if file is None or not file.filename:
        raise ValueError("لم يُرفع ملف.")
    raw = await file.read()
    for enc in ("utf-8-sig", "utf-8", "cp1256", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


@router.get("", response_class=HTMLResponse)
def import_export_hub(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
):
    msg = request.query_params.get("msg")
    err = request.query_params.get("err")
    return templates.TemplateResponse(
        "catalog_import_export.html",
        {
            "request": request,
            "msg": msg,
            "err": err,
            "result": None,
        },
    )


@router.get("/template/{kind}")
def download_template(kind: str, _: User = Depends(_perm)):
    if kind == "categories":
        return _csv_response(categories_csv_template(), "categories_template.csv")
    if kind == "products":
        return _csv_response(products_csv_template(), "products_template.csv")
    if kind == "bom":
        return _csv_response(bom_csv_template(), "bom_template.csv")
    return RedirectResponse("/catalog/import-export?err=نوع غير معروف", status_code=302)


@router.get("/export/{kind}")
def download_export(kind: str, db: DBSession, _: User = Depends(_perm)):
    if kind == "categories":
        return _csv_response(categories_csv_export(db), "categories_export.csv")
    if kind == "products":
        return _csv_response(products_csv_export(db), "products_export.csv")
    if kind == "bom":
        return _csv_response(bom_csv_export(db), "bom_export.csv")
    return RedirectResponse("/catalog/import-export?err=نوع غير معروف", status_code=302)


@router.post("/import/{kind}", response_class=HTMLResponse)
async def import_csv(
    request: Request,
    kind: str,
    db: DBSession,
    _: User = Depends(_perm),
    file: UploadFile = File(...),
    update_existing: str = Form("on"),
):
    update = update_existing == "on"
    try:
        text = await _read_upload(file)
    except ValueError as e:
        return RedirectResponse(
            f"/catalog/import-export?err={quote(str(e))}", status_code=302
        )
    if kind == "categories":
        result = import_categories_csv(db, text, update_existing=update)
    elif kind == "products":
        result = import_products_csv(db, text, update_existing=update)
    elif kind == "bom":
        result = import_bom_csv(db, text, update_existing=update)
    else:
        return RedirectResponse(
            "/catalog/import-export?err=" + quote("نوع غير معروف"), status_code=302
        )
    db.commit()
    return templates.TemplateResponse(
        "catalog_import_export.html",
        {
            "request": request,
            "msg": None,
            "err": None,
            "result": result,
            "import_kind": kind,
        },
    )
