from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.deps import DBSession, require_permission, LoggedInUser
from app.jinja_env import templates
from modules.authz.models import Permission, Role, User
from modules.authz.permissions import ADMIN_ROLES, ADMIN_USERS
from modules.authz.service import get_user_by_username, hash_password, is_admin_role, sync_admin_role_permissions, verify_password
from modules.authz.ui_blocks import (
    UI_BLOCK_GROUPS,
    compute_ui_hidden_from_shown,
    ui_blocks_by_group,
    user_ui_hidden_set,
)
from modules.platform.business_domain import UserViewScope, parse_user_view_scope, user_view_scope_choices

router = APIRouter(prefix="/auth", tags=["auth"])
_MIN_PASSWORD_LENGTH = 8
KDS_SCOPE_OPTIONS = (
    ("ALL", "كل أقسام شاشة المطبخ"),
    ("CAFE", "المقهى / المشروبات فقط"),
    ("RESTAURANT", "المطعم / الوجبات فقط"),
)


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
    from modules.authz.kiosk import is_cashier_kiosk_user
    from modules.platform.business_domain import (
        is_hotel_scope_user,
        is_restaurant_scope_user,
    )

    if is_cashier_kiosk_user(user):
        return RedirectResponse("/pos", status_code=status.HTTP_302_FOUND)
    if is_hotel_scope_user(user):
        from modules.authz.kiosk import requires_hotel_shift_pin

        if requires_hotel_shift_pin(user):
            return RedirectResponse("/admin/hotel/pin", status_code=status.HTTP_302_FOUND)
        return RedirectResponse("/admin/hotel/dashboard", status_code=status.HTTP_302_FOUND)
    if is_restaurant_scope_user(user):
        return RedirectResponse("/pos", status_code=status.HTTP_302_FOUND)
    return RedirectResponse("/", status_code=status.HTTP_302_FOUND)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=status.HTTP_302_FOUND)


# --- Admin: roles / users (mounted with prefix /admin in main) ---

admin_router = APIRouter(prefix="/admin", tags=["admin"])


@admin_router.post("/view-mode")
def admin_set_view_mode(
    request: Request,
    user: LoggedInUser,
    mode: str = Form("general"),
):
    from modules.platform.business_domain import (
        SESSION_VIEW_MODE_KEY,
        ViewMode,
        is_system_admin,
    )

    if not is_system_admin(user):
        return RedirectResponse("/", status_code=302)
    try:
        request.session[SESSION_VIEW_MODE_KEY] = ViewMode(mode.strip().lower()).value
    except ValueError:
        request.session[SESSION_VIEW_MODE_KEY] = ViewMode.GENERAL.value
    ref = (request.headers.get("referer") or "/").strip()
    if not ref.startswith("/"):
        ref = "/"
    return RedirectResponse(ref, status_code=302)


@admin_router.get("/finance-hub", response_class=HTMLResponse)
def admin_finance_hub(
    request: Request,
    db: DBSession,
    user: LoggedInUser,
):
    from modules.authz.service import user_has_permission
    from modules.platform.business_domain import is_system_admin
    from modules.platform.finance_hub import build_finance_hub_context

    if not (
        is_system_admin(user)
        or user_has_permission(user, "reports:view")
        or user_has_permission(user, "gl:manage")
    ):
        return RedirectResponse("/", status_code=302)

    ctx = build_finance_hub_context(db, user, request.session)
    ctx["request"] = request
    return templates.TemplateResponse("admin_finance_hub.html", ctx)


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
            "is_admin_role": is_admin_role(role),
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
    if is_admin_role(role):
        sync_admin_role_permissions(db)
        db.commit()
        return RedirectResponse(f"/admin/roles/{role_id}?admin_locked=1", status_code=302)
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
        {
            "request": request,
            "users": users,
            "roles": roles,
            "kds_scope_options": KDS_SCOPE_OPTIONS,
            "view_scope_options": user_view_scope_choices(),
            "ui_block_groups": UI_BLOCK_GROUPS,
            "ui_blocks_by_group": ui_blocks_by_group(),
        },
    )


@admin_router.post("/users", response_class=HTMLResponse)
def admin_user_create(
    request: Request,
    db: DBSession,
    _: User = Depends(require_permission(ADMIN_USERS)),
    username: str = Form(...),
    password: str = Form(...),
    kds_scope: str = Form("ALL"),
    view_scope: str = Form("both"),
    ui_show: list[str] | None = Form(None),
    role_ids: list[int] | None = Form(None),
):
    users = list(db.scalars(select(User).order_by(User.id)).all())
    roles = list(db.scalars(select(Role).order_by(Role.id)).all())
    if len((password or "").strip()) < _MIN_PASSWORD_LENGTH:
        return templates.TemplateResponse(
            "admin_users.html",
            {
                "request": request,
                "users": users,
                "roles": roles,
                "kds_scope_options": KDS_SCOPE_OPTIONS,
                "view_scope_options": user_view_scope_choices(),
                "ui_block_groups": UI_BLOCK_GROUPS,
                "ui_blocks_by_group": ui_blocks_by_group(),
                "error": f"كلمة المرور يجب أن لا تقل عن {_MIN_PASSWORD_LENGTH} أحرف.",
            },
            status_code=400,
        )
    if db.execute(select(User).where(User.username == username.strip())).scalar_one_or_none():
        return templates.TemplateResponse(
            "admin_users.html",
            {
                "request": request,
                "users": users,
                "roles": roles,
                "kds_scope_options": KDS_SCOPE_OPTIONS,
                "view_scope_options": user_view_scope_choices(),
                "ui_block_groups": UI_BLOCK_GROUPS,
                "ui_blocks_by_group": ui_blocks_by_group(),
                "error": "اسم المستخدم موجود مسبقاً.",
            },
            status_code=400,
        )
    scope = (kds_scope or "ALL").strip().upper()
    if scope not in {x[0] for x in KDS_SCOPE_OPTIONS}:
        scope = "ALL"
    parsed_view = parse_user_view_scope(view_scope)
    if parsed_view == UserViewScope.HOTEL:
        scope = "ALL"
    u = User(
        username=username.strip(),
        password_hash=hash_password(password),
        is_active=True,
        kds_scope=scope,
        view_scope=parsed_view.value,
        ui_hidden=compute_ui_hidden_from_shown(ui_show or []),
    )
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
    kds_scope: str = Form("ALL"),
    view_scope: str = Form("both"),
    ui_show: list[str] | None = Form(None),
    role_ids: list[int] | None = Form(None),
):
    u = db.get(User, user_id)
    if u is None:
        return RedirectResponse("/admin/users", status_code=302)
    rids = role_ids or []
    u.roles = list(db.scalars(select(Role).where(Role.id.in_(rids))).all()) if rids else []
    parsed_view = parse_user_view_scope(view_scope)
    u.view_scope = parsed_view.value
    u.ui_hidden = compute_ui_hidden_from_shown(ui_show or [])
    if parsed_view == UserViewScope.HOTEL:
        u.kds_scope = "ALL"
    else:
        scope = (kds_scope or "ALL").strip().upper()
        u.kds_scope = scope if scope in {x[0] for x in KDS_SCOPE_OPTIONS} else "ALL"
    db.commit()
    return RedirectResponse("/admin/users", status_code=302)


@admin_router.post("/users/{user_id}/password", response_class=HTMLResponse)
def admin_user_password_save(
    user_id: int,
    db: DBSession,
    _: User = Depends(require_permission(ADMIN_USERS)),
    password: str = Form(...),
):
    u = db.get(User, user_id)
    if u is None:
        return RedirectResponse("/admin/users", status_code=302)
    new_password = (password or "").strip()
    if len(new_password) < _MIN_PASSWORD_LENGTH:
        return RedirectResponse(
            f"/admin/users?error=كلمة المرور يجب أن لا تقل عن {_MIN_PASSWORD_LENGTH} أحرف.",
            status_code=302,
        )
    u.password_hash = hash_password(new_password)
    db.commit()
    return RedirectResponse("/admin/users", status_code=302)
