"""صفحة أدمن لإصلاح أعمدة قاعدة البيانات من المتصفح (VPS)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from infra.catalog_schema import catalog_missing_columns, repair_catalog_schema
from infra.db import get_engine
from infra.schema_bootstrap import reset_schema_patch_flag
from modules.authz.models import User
from modules.authz.permissions import ADMIN_SETTINGS

router = APIRouter(prefix="/admin/schema-fix", tags=["admin-schema"])
_admin = require_permission(ADMIN_SETTINGS)


@router.get("", response_class=HTMLResponse)
def schema_fix_page(request: Request, db: DBSession, _: User = Depends(_admin)):
    engine = get_engine()
    missing = catalog_missing_columns(engine)
    ok = request.query_params.get("ok") == "1"
    err = request.query_params.get("err")
    return templates.TemplateResponse(
        "admin_schema_fix.html",
        {
            "request": request,
            "missing": missing,
            "db_label": str(engine.url).split("@")[-1] if "@" in str(engine.url) else str(engine.url),
            "ok": ok,
            "error": err,
        },
    )


@router.post("/run", response_class=HTMLResponse)
def schema_fix_run(_: Request, __: DBSession, ___: User = Depends(_admin)):
    try:
        reset_schema_patch_flag()
        added = repair_catalog_schema(get_engine())
        if catalog_missing_columns(get_engine()):
            return RedirectResponse(
                "/admin/schema-fix?err=still_missing",
                status_code=302,
            )
        msg = "1" if added else "already_ok"
        return RedirectResponse(f"/admin/schema-fix?ok={msg}", status_code=302)
    except Exception as exc:
        return RedirectResponse(
            f"/admin/schema-fix?err={str(exc)[:120]}",
            status_code=302,
        )
