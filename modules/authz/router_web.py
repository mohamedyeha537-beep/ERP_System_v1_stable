from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import Permission, Role, User
from modules.authz.permissions import ADMIN_ROLES, ADMIN_USERS
from modules.authz.service import get_user_by_username, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": None},
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    db: DBSession,
    username: str = Form(...),
    password: str = Form(...),
):
    user = get_user_by_username(db, username.strip())
    if user is None or not verify_password(password, user.password_hash):
        # استخدام 200 بدل 401/403 لتفادي ERR_INVALID_HTTP_RESPONSE في بعض المتصفحات مع نماذج HTML
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "اسم المستخدم أو كلمة المرور غير صحيحة."},
            status_code=status.HTTP_200_OK,
        )
    if not user.is_active:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "الحساب معطّل."},
            status_code=status.HTTP_200_OK,
        )
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=status.HTTP_302_FOUND)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=status.HTTP_302_FOUND)


# --- Admin: roles / users (mounted with prefix /admin in main) ---

admin_router = APIRouter(prefix="/admin", tags=["admin"])


@admin_router.get("/roles", response_class=HTMLResponse)
def admin_roles(
    request: Request,
    db: DBSession,
    _: User = Depends(require_permission(ADMIN_ROLES)),
):
    roles = list(db.scalars(select(Role).order_by(Role.id)).all())
    return templates.TemplateResponse("admin_roles.html", {"request": request, "roles": roles})


@admin_router.get("/roles/{role_id}", response_class=HTMLResponse)
def admin_role_edit(
    request: Request,
    role_id: int,
    db: DBSession,
    _: User = Depends(require_permission(ADMIN_ROLES)),
):
    role = db.get(Role, role_id)
    if role is None:
        return RedirectResponse("/admin/roles", status_code=302)
    perms = list(db.scalars(select(Permission).order_by(Permission.code)).all())
    role_perm_ids = {p.id for p in role.permissions}
    return templates.TemplateResponse(
        "admin_role_edit.html",
        {
            "request": request,
            "role": role,
            "perms": perms,
            "role_perm_ids": role_perm_ids,
        },
    )


@admin_router.post("/roles", response_class=HTMLResponse)
def admin_role_create(
    request: Request,
    db: DBSession,
    _: User = Depends(require_permission(ADMIN_ROLES)),
    name_ar: str = Form(...),
):
    r = Role(name_ar=name_ar.strip())
    db.add(r)
    db.commit()
    return RedirectResponse(f"/admin/roles/{r.id}", status_code=302)


@admin_router.post("/roles/{role_id}", response_class=HTMLResponse)
def admin_role_save(
    request: Request,
    role_id: int,
    db: DBSession,
    _: User = Depends(require_permission(ADMIN_ROLES)),
    name_ar: str = Form(...),
    perm_ids: list[int] | None = Form(None),
):
    role = db.get(Role, role_id)
    if role is None:
        return RedirectResponse("/admin/roles", status_code=302)
    role.name_ar = name_ar.strip()
    ids = perm_ids or []
    if ids:
        perms = db.execute(select(Permission).where(Permission.id.in_(ids))).scalars().all()
        role.permissions = list(perms)
    else:
        role.permissions = []
    db.commit()
    return RedirectResponse(f"/admin/roles/{role_id}", status_code=302)


@admin_router.get("/users", response_class=HTMLResponse)
def admin_users(
    request: Request,
    db: DBSession,
    _: User = Depends(require_permission(ADMIN_USERS)),
):
    users = list(db.scalars(select(User).order_by(User.id)).all())
    roles = list(db.scalars(select(Role).order_by(Role.id)).all())
    return templates.TemplateResponse(
        "admin_users.html",
        {"request": request, "users": users, "roles": roles},
    )


@admin_router.post("/users", response_class=HTMLResponse)
def admin_user_create(
    request: Request,
    db: DBSession,
    _: User = Depends(require_permission(ADMIN_USERS)),
    username: str = Form(...),
    password: str = Form(...),
    role_ids: list[int] | None = Form(None),
):
    if db.execute(select(User).where(User.username == username.strip())).scalar_one_or_none():
        users = list(db.scalars(select(User).order_by(User.id)).all())
        roles = list(db.scalars(select(Role).order_by(Role.id)).all())
        return templates.TemplateResponse(
            "admin_users.html",
            {
                "request": request,
                "users": users,
                "roles": roles,
                "error": "اسم المستخدم موجود مسبقاً.",
            },
            status_code=400,
        )
    u = User(username=username.strip(), password_hash=hash_password(password), is_active=True)
    rids = role_ids or []
    if rids:
        u.roles = list(db.scalars(select(Role).where(Role.id.in_(rids))).all())
    db.add(u)
    db.commit()
    return RedirectResponse("/admin/users", status_code=302)


@admin_router.post("/users/{user_id}/roles", response_class=HTMLResponse)
def admin_user_roles_save(
    user_id: int,
    db: DBSession,
    _: User = Depends(require_permission(ADMIN_USERS)),
    role_ids: list[int] | None = Form(None),
):
    u = db.get(User, user_id)
    if u is None:
        return RedirectResponse("/admin/users", status_code=302)
    rids = role_ids or []
    u.roles = list(db.scalars(select(Role).where(Role.id.in_(rids))).all()) if rids else []
    db.commit()
    return RedirectResponse("/admin/users", status_code=302)
